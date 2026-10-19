"""选区再生成的并发度：1x1 模式下每个 beat 一张图，不该被排成串行往返。

首帧阶段用的是 ``1x1_16-9``：capacity=1 → 9 个 beat = 9 个 job，每个 job 都是一次
独立的网关往返。原实现逐个 ``await``，于是 9 张首帧排成 9 个完整往返的墙钟；这里
锁住「同一批 job 会并发发出」「上限受控」「返回顺序与串行时一致」三件事。
"""

from __future__ import annotations

import asyncio

import pytest

from novelvideo.generators import nanobanana_grid


class _StubGenerator:
    """记录同时在飞的调用数，并按给定延迟返回一个成功结果。"""

    in_flight = 0
    max_in_flight = 0

    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def generate_grid(self, **kwargs) -> nanobanana_grid.GridGenerationResult:
        type(self).in_flight += 1
        type(self).max_in_flight = max(type(self).max_in_flight, type(self).in_flight)
        try:
            await asyncio.sleep(self._delay)
            return nanobanana_grid.GridGenerationResult(
                success=True, grid_image_path=kwargs["output_path"]
            )
        finally:
            type(self).in_flight -= 1


@pytest.fixture(autouse=True)
def _reset_in_flight():
    _StubGenerator.in_flight = 0
    _StubGenerator.max_in_flight = 0
    yield


def _install_stub(monkeypatch, delay: float = 0.02) -> None:
    monkeypatch.setattr(
        nanobanana_grid,
        "create_grid_generator",
        lambda api_key=None, config=None: _StubGenerator(delay),
    )


def _beats(count: int) -> list[dict]:
    return [
        {"beat_number": i + 1, "visual_description": f"beat {i + 1}"}
        for i in range(count)
    ]


def test_regen_runs_grids_concurrently_when_the_switch_allows_it(
    monkeypatch, tmp_path
):
    _install_stub(monkeypatch)
    monkeypatch.setenv(nanobanana_grid.REGEN_CONCURRENCY_ENV, "3")

    results = asyncio.run(
        nanobanana_grid.regenerate_selected_beats(
            selected_beats=_beats(9),
            mode_key="1x1_16-9",
            character_map={},
            style="realistic",
            output_dir=str(tmp_path),
            progress_callback=None,
        )
    )

    assert len(results) == 9
    assert _StubGenerator.max_in_flight == 3


def test_regen_keeps_grid_order_and_reports_progress(
    monkeypatch, tmp_path
):
    _install_stub(monkeypatch)
    monkeypatch.setenv(nanobanana_grid.REGEN_CONCURRENCY_ENV, "3")
    seen: list[tuple[int, int]] = []

    results = asyncio.run(
        nanobanana_grid.regenerate_selected_beats(
            selected_beats=_beats(9),
            mode_key="1x1_16-9",
            character_map={},
            style="realistic",
            output_dir=str(tmp_path),
            progress_callback=lambda done, total: seen.append((done, total)),
        )
    )

    # 并发完成顺序不定，但返回值必须仍按 grid 顺序（对应 beat 1..9）。
    assert [result.beat_start_index for result in results] == list(range(9))
    assert [(done, total) for done, total in seen] == [
        (i + 1, 9) for i in range(9)
    ]


def test_regen_concurrency_one_falls_back_to_serial(monkeypatch, tmp_path):
    _install_stub(monkeypatch)
    monkeypatch.setenv(nanobanana_grid.REGEN_CONCURRENCY_ENV, "1")

    asyncio.run(
        nanobanana_grid.regenerate_selected_beats(
            selected_beats=_beats(4),
            mode_key="1x1_16-9",
            character_map={},
            style="realistic",
            output_dir=str(tmp_path),
            progress_callback=None,
        )
    )

    assert _StubGenerator.max_in_flight == 1


def test_regen_concurrency_reads_env_with_sane_bounds(monkeypatch):
    monkeypatch.delenv(nanobanana_grid.REGEN_CONCURRENCY_ENV, raising=False)
    assert nanobanana_grid._regen_concurrency(9) == (
        nanobanana_grid.DEFAULT_REGEN_CONCURRENCY
    )
    # job 数比上限小时不必多开。
    assert nanobanana_grid._regen_concurrency(2) == 2

    monkeypatch.setenv(nanobanana_grid.REGEN_CONCURRENCY_ENV, "0")
    assert nanobanana_grid._regen_concurrency(9) == 1

    monkeypatch.setenv(nanobanana_grid.REGEN_CONCURRENCY_ENV, "-4")
    assert nanobanana_grid._regen_concurrency(9) == 1

    monkeypatch.setenv(nanobanana_grid.REGEN_CONCURRENCY_ENV, "99")
    assert nanobanana_grid._regen_concurrency(5) == 5

    monkeypatch.setenv(nanobanana_grid.REGEN_CONCURRENCY_ENV, "not-a-number")
    assert nanobanana_grid._regen_concurrency(9) == (
        nanobanana_grid.DEFAULT_REGEN_CONCURRENCY
    )

    assert nanobanana_grid._regen_concurrency(0) == 1
