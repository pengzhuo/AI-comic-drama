"""全局视频优化：``review_and_fix`` 的有界并发与就地更新语义。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from novelvideo.agents import global_video_optimizer as gvo


class _FakeReviewAgent:
    """记录在飞峰值；每条请求小睡以便观测并发。"""

    def __init__(self) -> None:
        self.max_in_flight = 0
        self._in_flight = 0
        self.prompts: list[str] = []

    async def run(self, payload):
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            task, _image = payload
            self.prompts.append(task)
            await asyncio.sleep(0.05)
            return SimpleNamespace(
                output=gvo.ReviewResult(needs_fix=True, reason="r", prompt="FIXED")
            )
        finally:
            self._in_flight -= 1


def _optimizer(fake: _FakeReviewAgent) -> gvo.GlobalVideoPromptOptimizer:
    optimizer = gvo.GlobalVideoPromptOptimizer()
    optimizer._review_agent = fake
    optimizer._compress_image = lambda _path, **_kw: b"jpeg"  # type: ignore[method-assign]
    return optimizer


async def test_review_and_fix_runs_concurrently_in_place(tmp_path: Path) -> None:
    for num in (1, 2, 3):
        (tmp_path / f"beat_{num:02d}.png").write_bytes(b"x")

    fake = _FakeReviewAgent()
    optimizer = _optimizer(fake)

    results = [
        {"beat_number": num, "video_mode": "first_frame", "prompt": f"p{num}"}
        for num in (1, 2, 3)
    ]
    beats = [
        {"beat_number": num, "visual_description": f"v{num}", "narration_segment": "n"}
        for num in (1, 2, 3)
    ]

    out = await optimizer.review_and_fix(results, beats, str(tmp_path), {}, log_fn=None)

    # 同一列表就地更新，prompt 被改写。
    assert out is results
    assert [item["prompt"] for item in results] == ["FIXED", "FIXED", "FIXED"]
    # 三个 beat 各审一次。
    assert len(fake.prompts) == 3
    # 默认并发 2 → 至少两个请求同时在飞（旧的串行循环恒为 1）。
    assert fake.max_in_flight >= 2


async def test_review_and_fix_skips_missing_sketch(tmp_path: Path) -> None:
    fake = _FakeReviewAgent()
    optimizer = _optimizer(fake)

    results = [{"beat_number": 1, "video_mode": "first_frame", "prompt": "p1"}]
    beats = [{"beat_number": 1, "visual_description": "v", "narration_segment": "n"}]

    out = await optimizer.review_and_fix(results, beats, str(tmp_path), {}, log_fn=None)

    assert out[0]["prompt"] == "p1"
    assert fake.prompts == []


def _frame_resolver(sketches: Path, pool: Path) -> SimpleNamespace:
    """PathResolver 替身：``_try_combine_frames_to_grid`` 只用这两个方法。"""
    return SimpleNamespace(sketches_dir=lambda: sketches, sketch_dir=lambda: pool)


def _write_frames(directory: Path, count: int) -> None:
    from PIL import Image

    directory.mkdir(parents=True, exist_ok=True)
    for num in range(1, count + 1):
        Image.new("RGB", (8, 8), "white").save(directory / f"beat_{num:02d}.png")


def test_combine_frames_to_grid_supports_two_and_three_beats(tmp_path: Path) -> None:
    """2-3 个 beat 的集也要能拼出网格。

    护花铃 39 个有 beats 的集里 32 个 <4 帧，旧实现「至少 4 帧才拼」会让这些集的
    ``global_optimize_video`` 永远拿不到网格、卡在「找不到草图网格」重试到硬暂停。
    """
    for episode, beats_count in ((2, 3), (3, 2)):
        sketches = tmp_path / f"ep{episode:03d}" / "sketches"
        pool = tmp_path / f"ep{episode:03d}" / "grids" / "sketch"
        _write_frames(sketches, beats_count)
        beats = [{"beat_number": num} for num in range(1, beats_count + 1)]

        out = gvo._try_combine_frames_to_grid(
            _frame_resolver(sketches, pool), beats, str(tmp_path), episode
        )

        assert [Path(p).name for p in out] == ["_global_opt_grid_2x2.png"]
        assert Path(out[0]).exists()


def test_combine_frames_to_grid_single_frame_and_no_frame(tmp_path: Path) -> None:
    sketches = tmp_path / "sketches"
    pool = tmp_path / "grids" / "sketch"
    _write_frames(sketches, 1)

    out = gvo._try_combine_frames_to_grid(
        _frame_resolver(sketches, pool), [{"beat_number": 1}], str(tmp_path), 1
    )
    assert [Path(p).name for p in out] == ["_global_opt_grid_1x1.png"]

    # 一帧都没有 → 仍返回空列表，runner 据此报「找不到草图网格」。
    empty = tmp_path / "empty_sketches"
    empty.mkdir()
    assert (
        gvo._try_combine_frames_to_grid(
            _frame_resolver(empty, tmp_path / "empty_pool"),
            [{"beat_number": 1}, {"beat_number": 2}],
            str(tmp_path),
            9,
        )
        == []
    )


def test_combine_frames_to_grid_keeps_tiers_for_four_plus(tmp_path: Path) -> None:
    """≥4 帧保持旧档位：4 帧 → 2x2 邻档，5 帧 → 3x3，17 帧 → 5x5。"""
    for beats_count, expected in ((5, "3x3"), (17, "5x5")):
        sketches = tmp_path / f"n{beats_count}" / "sketches"
        pool = tmp_path / f"n{beats_count}" / "grids" / "sketch"
        _write_frames(sketches, beats_count)
        beats = [{"beat_number": num} for num in range(1, beats_count + 1)]

        out = gvo._try_combine_frames_to_grid(
            _frame_resolver(sketches, pool), beats, str(tmp_path), 1
        )

        assert [Path(p).name for p in out] == [f"_global_opt_grid_{expected}.png"]
