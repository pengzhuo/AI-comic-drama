from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from novelvideo.project_context import ProjectContext


def _project_ctx(tmp_path: Path) -> ProjectContext:
    return ProjectContext(
        project_id="proj_123",
        project_name="demo",
        owner_type="user",
        owner_id="user_owner",
        owner_username="admin",
        requester_user_id="user_editor",
        requester_username="admin",
        requester_principals=(("user", "user_editor"),),
        effective_role="editor",
        home_node_id="node_a",
        output_dir=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        is_home_node=True,
    )


@pytest.mark.asyncio
async def test_global_optimize_video_closes_cognee_store_on_success(monkeypatch, tmp_path):
    from novelvideo import cognee
    from novelvideo.agents import global_video_optimizer
    from novelvideo.task_backend.runners import video
    from novelvideo.utils.path_resolver import PathResolver

    sketch_path = PathResolver(str(tmp_path), 1).sketch(1)
    sketch_path.parent.mkdir(parents=True, exist_ok=True)
    sketch_path.write_bytes(b"fake-png")

    calls: list[str] = []

    class FakeTaskManager:
        def update_progress_for_project(self, *args, **kwargs):
            return None

    class FakeCogneeStore:
        def __init__(self, *args, **kwargs):
            calls.append("init")

        async def initialize(self):
            calls.append("initialize")

        async def load_graph_state(self):
            calls.append("load_graph_state")

        async def update_beat_asset(self, **kwargs):
            calls.append("update_beat_asset")
            return True

        async def close(self):
            calls.append("close")

    class FakeOptimizer:
        async def optimize_single_beat(self, **kwargs):
            return {"prompt": "optimized prompt"}

    monkeypatch.setattr(video, "get_task_manager", lambda: FakeTaskManager())
    monkeypatch.setattr(cognee, "CogneeStore", FakeCogneeStore)
    monkeypatch.setattr(
        global_video_optimizer,
        "prepare_global_optimizer_input",
        lambda **kwargs: ([str(sketch_path)], {}, 1),
    )
    monkeypatch.setattr(
        global_video_optimizer,
        "get_global_video_optimizer",
        lambda: FakeOptimizer(),
    )

    result = await video._run_global_optimize_video_async(
        {
            "episode": 1,
            "payload": {
                "episode": 1,
                "beats": [{"beat_number": 1, "visual_description": "frame"}],
                "characters": [],
                "output_dir": str(tmp_path),
            },
        },
        _project_ctx(tmp_path),
    )

    assert result["optimized"] == 1
    assert calls == [
        "init",
        "initialize",
        "load_graph_state",
        "update_beat_asset",
        "close",
    ]


@pytest.mark.asyncio
async def test_global_optimize_video_closes_cognee_store_on_failure(monkeypatch, tmp_path):
    from novelvideo import cognee
    from novelvideo.agents import global_video_optimizer
    from novelvideo.task_backend.runners import video
    from novelvideo.utils.path_resolver import PathResolver

    sketch_path = PathResolver(str(tmp_path), 1).sketch(1)
    sketch_path.parent.mkdir(parents=True, exist_ok=True)
    sketch_path.write_bytes(b"fake-png")

    calls: list[str] = []

    class FakeTaskManager:
        def update_progress_for_project(self, *args, **kwargs):
            return None

    class FakeCogneeStore:
        def __init__(self, *args, **kwargs):
            calls.append("init")

        async def initialize(self):
            calls.append("initialize")

        async def load_graph_state(self):
            calls.append("load_graph_state")

        async def close(self):
            calls.append("close")

    class FakeOptimizer:
        async def optimize_single_beat(self, **kwargs):
            calls.append("optimize_single_beat")
            raise RuntimeError("model unavailable")

    monkeypatch.setattr(video, "get_task_manager", lambda: FakeTaskManager())
    monkeypatch.setattr(cognee, "CogneeStore", FakeCogneeStore)
    monkeypatch.setattr(
        global_video_optimizer,
        "prepare_global_optimizer_input",
        lambda **kwargs: ([str(sketch_path)], {}, 1),
    )
    monkeypatch.setattr(
        global_video_optimizer,
        "get_global_video_optimizer",
        lambda: FakeOptimizer(),
    )

    with pytest.raises(RuntimeError, match="model unavailable"):
        await video._run_global_optimize_video_async(
            {
                "episode": 1,
                "payload": {
                    "episode": 1,
                    "beats": [{"beat_number": 1, "visual_description": "frame"}],
                    "characters": [],
                    "output_dir": str(tmp_path),
                },
            },
            _project_ctx(tmp_path),
        )

    assert calls == [
        "init",
        "initialize",
        "load_graph_state",
        "optimize_single_beat",
        "close",
    ]


@pytest.mark.asyncio
async def test_global_optimize_video_continues_after_consecutive_model_failures(
    monkeypatch, tmp_path
):
    from novelvideo import cognee
    from novelvideo.agents import global_video_optimizer
    from novelvideo.task_backend.runners import video
    from novelvideo.utils.path_resolver import PathResolver

    beats = [{"beat_number": beat, "visual_description": "frame"} for beat in range(1, 6)]
    for beat in range(1, 6):
        sketch_path = PathResolver(str(tmp_path), 1).sketch(beat)
        sketch_path.parent.mkdir(parents=True, exist_ok=True)
        sketch_path.write_bytes(b"fake-png")

    attempts: list[int] = []
    logs: list[str] = []

    class FakeTaskManager:
        def update_progress_for_project(self, *args, **kwargs):
            logs.extend(kwargs.get("logs") or [])

    class FakeCogneeStore:
        def __init__(self, *args, **kwargs):
            pass

        async def initialize(self):
            pass

        async def load_graph_state(self):
            pass

        async def close(self):
            pass

    class FakeOptimizer:
        async def optimize_single_beat(self, **kwargs):
            attempts.append(kwargs["beat"]["beat_number"])
            raise RuntimeError("invalid structured output")

    monkeypatch.setattr(video, "get_task_manager", lambda: FakeTaskManager())
    monkeypatch.setattr(cognee, "CogneeStore", FakeCogneeStore)
    monkeypatch.setattr(
        global_video_optimizer,
        "prepare_global_optimizer_input",
        lambda **kwargs: (["grid.png"], {}, len(beats)),
    )
    monkeypatch.setattr(
        global_video_optimizer,
        "get_global_video_optimizer",
        lambda: FakeOptimizer(),
    )

    with pytest.raises(RuntimeError, match="Beat 5: invalid structured output"):
        await video._run_global_optimize_video_async(
            {
                "episode": 1,
                "payload": {
                    "episode": 1,
                    "beats": beats,
                    "characters": [],
                    "output_dir": str(tmp_path),
                },
            },
            _project_ctx(tmp_path),
        )

    assert attempts == [1, 2, 3, 4, 5]
    assert not any("提前停止全局优化" in message for message in logs)


def _envelope(tmp_path: Path, beats: list[dict]) -> dict:
    return {
        "episode": 1,
        "payload": {
            "episode": 1,
            "beats": beats,
            "characters": [],
            "output_dir": str(tmp_path),
        },
    }


def _install_optimizer_fakes(
    monkeypatch,
    tmp_path: Path,
    beats: list[dict],
    *,
    fail_beats: set[int] | None = None,
    delay: float = 0.0,
) -> tuple[object, dict]:
    """装一套假的 CogneeStore / 优化器，返回 (video 模块, 调用记录)。

    记录内容：``attempts``(beat_number, prev_prompt) / ``store``(落库顺序) /
    ``max_in_flight``(同时在飞的最大调用数，用来断言确实并发了)。
    """

    from novelvideo import cognee
    from novelvideo.agents import global_video_optimizer
    from novelvideo.task_backend.runners import video
    from novelvideo.utils.path_resolver import PathResolver

    failing = set(fail_beats or ())
    for beat in beats:
        num = int(beat["beat_number"])
        sketch = PathResolver(str(tmp_path), 1).sketch(num)
        sketch.parent.mkdir(parents=True, exist_ok=True)
        sketch.write_bytes(b"fake-png")

    record: dict = {
        "attempts": [],
        "store": [],
        "logs": [],
        "max_in_flight": 0,
        "in_flight": 0,
    }

    class FakeTaskManager:
        def update_progress_for_project(self, *args, **kwargs):
            record["logs"].extend(kwargs.get("logs") or [])

    class FakeCogneeStore:
        def __init__(self, *args, **kwargs):
            pass

        async def initialize(self):
            pass

        async def load_graph_state(self):
            pass

        async def update_beat_asset(self, **kwargs):
            record["store"].append(int(kwargs["beat_number"]))
            return True

        async def close(self):
            pass

    class FakeOptimizer:
        async def optimize_single_beat(self, **kwargs):
            record["in_flight"] += 1
            record["max_in_flight"] = max(
                record["max_in_flight"], record["in_flight"]
            )
            try:
                num = int(kwargs["beat"]["beat_number"])
                record["attempts"].append((num, kwargs["prev_prompt"]))
                if delay:
                    await asyncio.sleep(delay)
                if num in failing:
                    raise RuntimeError(f"beat {num} exploded")
                return {"prompt": f"P{num}"}
            finally:
                record["in_flight"] -= 1

    monkeypatch.setattr(video, "get_task_manager", lambda: FakeTaskManager())
    monkeypatch.setattr(cognee, "CogneeStore", FakeCogneeStore)
    monkeypatch.setattr(
        global_video_optimizer,
        "prepare_global_optimizer_input",
        lambda **kwargs: (["grid.png"], {}, len(beats)),
    )
    monkeypatch.setattr(
        global_video_optimizer,
        "get_global_video_optimizer",
        lambda: FakeOptimizer(),
    )
    return video, record


@pytest.mark.asyncio
async def test_global_optimize_video_serial_chain_matches_legacy(monkeypatch, tmp_path):
    """并发=1（旧默认）必须与旧的严格串行一致：prev_prompt 就是上一 beat 的输出。"""

    monkeypatch.setenv("GLOBAL_OPTIMIZE_CONCURRENCY", "1")
    monkeypatch.setenv("GLOBAL_OPTIMIZE_WAVE_SIZE", "1")
    beats = [{"beat_number": n, "visual_description": "frame"} for n in (1, 2, 3)]
    video, record = _install_optimizer_fakes(monkeypatch, tmp_path, beats)

    result = await video._run_global_optimize_video_async(
        _envelope(tmp_path, beats), _project_ctx(tmp_path)
    )

    assert result["optimized"] == 3
    assert record["attempts"] == [(1, None), (2, "P1"), (3, "P2")]
    assert record["store"] == [1, 2, 3]
    assert record["max_in_flight"] == 1


@pytest.mark.asyncio
async def test_global_optimize_video_waves_anchor_on_previous_wave(monkeypatch, tmp_path):
    """波内并发：每个 beat 以上一波最后一个 beat 的提示词为连贯锚点。"""

    monkeypatch.setenv("GLOBAL_OPTIMIZE_CONCURRENCY", "3")
    monkeypatch.setenv("GLOBAL_OPTIMIZE_WAVE_SIZE", "3")
    beats = [{"beat_number": n, "visual_description": "frame"} for n in range(1, 7)]
    video, record = _install_optimizer_fakes(monkeypatch, tmp_path, beats, delay=0.01)

    result = await video._run_global_optimize_video_async(
        _envelope(tmp_path, beats), _project_ctx(tmp_path)
    )

    assert result["optimized"] == 6
    anchors = dict(record["attempts"])
    assert [anchors[n] for n in (1, 2, 3)] == [None, None, None]
    assert [anchors[n] for n in (4, 5, 6)] == ["P3", "P3", "P3"]
    assert record["store"] == [1, 2, 3, 4, 5, 6]
    assert record["max_in_flight"] >= 2


@pytest.mark.asyncio
async def test_global_optimize_video_wave_failure_keeps_other_beats(monkeypatch, tmp_path):
    """波内某个 beat 失败不影响同波其它 beat，锚点取上一波最后一个成功结果。"""

    monkeypatch.setenv("GLOBAL_OPTIMIZE_CONCURRENCY", "3")
    monkeypatch.setenv("GLOBAL_OPTIMIZE_WAVE_SIZE", "3")
    beats = [{"beat_number": n, "visual_description": "frame"} for n in range(1, 7)]
    video, record = _install_optimizer_fakes(
        monkeypatch, tmp_path, beats, fail_beats={3}
    )

    result = await video._run_global_optimize_video_async(
        _envelope(tmp_path, beats), _project_ctx(tmp_path)
    )

    assert result["optimized"] == 5
    assert record["store"] == [1, 2, 4, 5, 6]
    assert dict(record["attempts"])[4] == "P2"
    assert any("Beat 3: 生成失败" in message for message in record["logs"])


@pytest.mark.asyncio
async def test_global_optimize_video_wave_can_exceed_in_flight_limit(monkeypatch, tmp_path):
    """波宽 > 在飞上限：吞吐受上限约束，锚点仍取上一波最后一个成功提示词。

    波宽放大只是为了摊薄「每波等最慢那一路」的栅栏成本（实测 8 路波宽=8 只拿到
    4.9x），并不改变并发上限。
    """

    monkeypatch.setenv("GLOBAL_OPTIMIZE_CONCURRENCY", "2")
    monkeypatch.setenv("GLOBAL_OPTIMIZE_WAVE_SIZE", "6")
    beats = [{"beat_number": n, "visual_description": "frame"} for n in range(1, 13)]
    video, record = _install_optimizer_fakes(monkeypatch, tmp_path, beats, delay=0.01)

    result = await video._run_global_optimize_video_async(
        _envelope(tmp_path, beats), _project_ctx(tmp_path)
    )

    assert result["optimized"] == 12
    anchors = dict(record["attempts"])
    assert [anchors[n] for n in range(1, 7)] == [None] * 6
    assert [anchors[n] for n in range(7, 13)] == ["P6"] * 6
    assert record["max_in_flight"] == 2


@pytest.mark.asyncio
async def test_global_optimize_video_prev_prompt_none_omits_anchor(monkeypatch, tmp_path):
    """prev_prompt=none 时整段不发连贯锚点（用于 A/B 隔离该字段的作用）。"""

    monkeypatch.setenv("GLOBAL_OPTIMIZE_CONCURRENCY", "2")
    monkeypatch.setenv("GLOBAL_OPTIMIZE_PREV_PROMPT", "none")
    beats = [{"beat_number": n, "visual_description": "frame"} for n in range(1, 5)]
    video, record = _install_optimizer_fakes(monkeypatch, tmp_path, beats, delay=0.01)

    await video._run_global_optimize_video_async(
        _envelope(tmp_path, beats), _project_ctx(tmp_path)
    )

    assert [anchor for _num, anchor in record["attempts"]] == [None, None, None, None]
