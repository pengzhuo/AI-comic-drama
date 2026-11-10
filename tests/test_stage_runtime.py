"""共享阶段运行库（Phase 0）的回归测试。

覆盖：热调参优先级与坏文件兜底、:class:`StageLimiter` 冷却/温和降级、
:func:`run_bounded` 的保序与取消兄弟、进程级全局闸门上限、``pipeline_timing``
轮转与 :func:`summarize_stage`。
"""

from __future__ import annotations

import asyncio

import pytest

from novelvideo import pipeline_timing
from novelvideo.shared import global_llm_gate
from novelvideo.shared.global_llm_gate import global_llm_limit, global_llm_slot
from novelvideo.shared.stage_runtime import (
    StageLimiter,
    coerce_positive_int,
    env_flag,
    env_positive_int,
    load_runtime_tuning,
    run_bounded,
    runtime_tuning_path,
    stage_bool,
    stage_choice,
    stage_int,
    tuning_bool,
)


@pytest.fixture(autouse=True)
def _clean_global_gate():
    """每个用例前后清空全局闸门，避免 loop/semaphore 缓存串味。"""

    global_llm_gate._gates.clear()
    yield
    global_llm_gate._gates.clear()


# ── 值收正 ────────────────────────────────────────────────────────────────


def test_env_positive_int_and_flag(monkeypatch):
    monkeypatch.delenv("DEMO_INT", raising=False)
    assert env_positive_int("DEMO_INT", 3) == 3
    monkeypatch.setenv("DEMO_INT", "7")
    assert env_positive_int("DEMO_INT", 3) == 7
    monkeypatch.setenv("DEMO_INT", "0")
    assert env_positive_int("DEMO_INT", 3) == 3
    monkeypatch.setenv("DEMO_INT", "nope")
    assert env_positive_int("DEMO_INT", 3) == 3

    monkeypatch.delenv("DEMO_FLAG", raising=False)
    assert env_flag("DEMO_FLAG") is None
    monkeypatch.setenv("DEMO_FLAG", "off")
    assert env_flag("DEMO_FLAG") is False
    monkeypatch.setenv("DEMO_FLAG", "ON")
    assert env_flag("DEMO_FLAG") is True


def test_coerce_and_tuning_bool_reject_garbage():
    assert coerce_positive_int(True) is None
    assert coerce_positive_int(0) is None
    assert coerce_positive_int(-2) is None
    assert coerce_positive_int(" 5 ") == 5
    assert coerce_positive_int("5.0") is None
    assert tuning_bool("false", True) is False
    assert tuning_bool("yes", False) is True
    assert tuning_bool(object(), True) is True


# ── 热调参文件 ────────────────────────────────────────────────────────────


def test_runtime_tuning_path_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv("NOVELVIDEO_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("NOVELVIDEO_STATE_DIR", raising=False)
    monkeypatch.delenv("DEMO_TUNING_FILE", raising=False)

    assert runtime_tuning_path("demo") == runtime_tuning_path("demo")
    assert str(runtime_tuning_path("demo")).endswith("demo.json")

    monkeypatch.setenv("DEMO_TUNING_FILE", str(tmp_path / "custom.json"))
    assert (
        runtime_tuning_path("demo", explicit_env="DEMO_TUNING_FILE")
        == tmp_path / "custom.json"
    )

    monkeypatch.delenv("DEMO_TUNING_FILE")
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(tmp_path / "rt"))
    assert runtime_tuning_path("demo") == tmp_path / "rt" / "demo.json"


def test_load_runtime_tuning_tolerates_bad_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(tmp_path))
    assert load_runtime_tuning("demo") == {}

    (tmp_path / "demo.json").write_text("{not json", encoding="utf-8")
    assert load_runtime_tuning("demo") == {}

    (tmp_path / "demo.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert load_runtime_tuning("demo") == {}

    (tmp_path / "demo.json").write_text('{"x": 1}', encoding="utf-8")
    assert load_runtime_tuning("demo") == {"x": 1}


def test_stage_int_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("DEMO_X", raising=False)

    # 常量
    assert stage_int("demo", "DEMO_X", "x", 5) == 5

    # 热调参文件
    (tmp_path / "demo.json").write_text('{"x": 7}', encoding="utf-8")
    assert stage_int("demo", "DEMO_X", "x", 5) == 7

    # env 优先
    monkeypatch.setenv("DEMO_X", "9")
    assert stage_int("demo", "DEMO_X", "x", 5) == 9

    # env 非法时回落到文件
    monkeypatch.setenv("DEMO_X", "abc")
    assert stage_int("demo", "DEMO_X", "x", 5) == 7

    # 文件值非法时回落到常量
    (tmp_path / "demo.json").write_text('{"x": -3}', encoding="utf-8")
    monkeypatch.delenv("DEMO_X")
    assert stage_int("demo", "DEMO_X", "x", 5) == 5


def test_stage_bool_and_choice_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("DEMO_B", raising=False)
    monkeypatch.delenv("DEMO_C", raising=False)

    assert stage_bool("demo", "DEMO_B", "b", True) is True
    (tmp_path / "demo.json").write_text('{"b": false, "c": "beta"}', encoding="utf-8")
    assert stage_bool("demo", "DEMO_B", "b", True) is False
    assert stage_choice("demo", "DEMO_C", "c", {"alpha", "beta"}, "alpha") == "beta"

    monkeypatch.setenv("DEMO_B", "on")
    assert stage_bool("demo", "DEMO_B", "b", True) is True
    monkeypatch.setenv("DEMO_C", "alpha")
    assert stage_choice("demo", "DEMO_C", "c", {"alpha", "beta"}, "beta") == "alpha"


# ── 并发闸门 ──────────────────────────────────────────────────────────────


async def test_stage_limiter_cooldown_trips_and_resets():
    limiter = StageLimiter(3, streak=2, cooldown_seconds=0.05)
    assert limiter.note_transient_failure() is False
    limiter.note_success()
    assert limiter.note_transient_failure() is False  # 成功已清零连续计数
    assert limiter.note_transient_failure() is True
    assert limiter.cooling_down is True

    serial = await limiter.acquire()
    assert serial is True
    limiter.release(serial)

    await asyncio.sleep(0.06)
    assert limiter.cooling_down is False


async def test_stage_limiter_cooldown_limit_graceful_decay():
    limiter = StageLimiter(4, streak=1, cooldown_seconds=60.0, cooldown_limit=2)
    assert limiter.note_transient_failure() is True

    first = await limiter.acquire()
    second = await limiter.acquire()
    assert first is True and second is True

    third = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.02)
    assert third.done() is False  # 被 cooldown_limit=2 挡住

    limiter.release(first)
    resumed = await asyncio.wait_for(third, timeout=1.0)
    assert resumed is True
    limiter.release(second)
    limiter.release(resumed)


async def test_run_bounded_preserves_input_order():
    async def worker(item: int) -> int:
        await asyncio.sleep(0.01 * (5 - item))
        return item

    limiter = StageLimiter(3, streak=5, cooldown_seconds=1.0)
    result = await run_bounded(
        [1, 2, 3, 4, 5], worker, limiter=limiter, gate=False
    )
    assert result == [1, 2, 3, 4, 5]


async def test_run_bounded_serial_without_limiter():
    async def worker(item: int) -> int:
        return item * 2

    assert await run_bounded([1, 2, 3], worker, limiter=None) == [2, 4, 6]


async def test_run_bounded_cancels_siblings_on_fatal():
    started: set[int] = set()
    cancelled: set[int] = set()
    all_asleep = asyncio.Event()
    sleeping = 0

    async def worker(item: int) -> int:
        nonlocal sleeping
        started.add(item)
        if item == 0:
            await all_asleep.wait()
            raise RuntimeError("fatal")
        sleeping += 1
        if sleeping >= 3:
            all_asleep.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.add(item)
            raise
        return item

    limiter = StageLimiter(4, streak=5, cooldown_seconds=1.0)
    with pytest.raises(RuntimeError, match="fatal"):
        await run_bounded([0, 1, 2, 3], worker, limiter=limiter, gate=False)

    assert started == {0, 1, 2, 3}
    assert cancelled == {1, 2, 3}


async def test_global_llm_gate_caps_concurrency(monkeypatch):
    monkeypatch.setenv("LLM_GLOBAL_CONCURRENCY", "2")
    assert global_llm_limit() == 2

    current = 0
    peak = 0
    lock = asyncio.Lock()

    async def one() -> None:
        nonlocal current, peak
        async with global_llm_slot():
            async with lock:
                current += 1
                peak = max(peak, current)
            await asyncio.sleep(0.02)
            async with lock:
                current -= 1

    await asyncio.gather(*(one() for _ in range(10)))
    assert peak <= 2


async def test_global_llm_gate_disabled(monkeypatch):
    monkeypatch.setenv("LLM_GLOBAL_CONCURRENCY", "0")
    assert global_llm_limit() == 0

    current = 0
    peak = 0
    lock = asyncio.Lock()

    async def one() -> None:
        nonlocal current, peak
        async with global_llm_slot():
            async with lock:
                current += 1
                peak = max(peak, current)
            await asyncio.sleep(0.02)
            async with lock:
                current -= 1

    await asyncio.gather(*(one() for _ in range(6)))
    assert peak >= 3  # 闸门关闭，不再被压到 1


# ── pipeline_timing 轮转与阶段汇总 ────────────────────────────────────────


def test_timing_rotation_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.setenv(pipeline_timing.TIMING_MAX_BYTES_ENV, "800")
    for i in range(100):
        pipeline_timing.record_event(
            tmp_path, "demo_call", phase="p", ok=True, duration_seconds=1.0, seq=i
        )

    events = pipeline_timing.load_events(tmp_path)
    assert events, "轮转后仍应保留事件"
    assert events[-1]["seq"] == 99
    assert len(events) < 100
    assert pipeline_timing.timing_path(tmp_path).stat().st_size <= 800


def test_summarize_stage(tmp_path):
    out = tmp_path
    pipeline_timing.record_event(out, "demo_call", phase="segment", ok=True, duration_seconds=2.0)
    pipeline_timing.record_event(out, "demo_call", phase="segment", ok=True, duration_seconds=4.0)
    pipeline_timing.record_event(out, "demo_call", phase="digest", ok=False, duration_seconds=1.0)
    pipeline_timing.record_event(out, "other_call", phase="x", ok=True, duration_seconds=99.0)

    summary = pipeline_timing.summarize_stage(out, "demo")
    assert summary["calls"] == 3
    assert summary["failures"] == 1
    assert summary["phases"] == {"segment": 2, "digest": 1}
    assert summary["duration_median_seconds"] == 2.0

    assert pipeline_timing.summarize_stage(out, "missing") == {
        "stage": "missing",
        "calls": 0,
    }


def test_record_span_and_summarize_spans(tmp_path):
    out = tmp_path
    # 两项同阶段（片段重叠），一项另一阶段。
    pipeline_timing.record_span(out, "sketch", started=100.0, ended=130.0, ok=True)
    pipeline_timing.record_span(out, "sketch", started=120.0, ended=150.0, ok=True)
    pipeline_timing.record_span(out, "render", started=160.0, ended=200.0, ok=False, episode=3)

    summary = pipeline_timing.summarize_spans(out)
    assert summary["count"] == 3
    sketch = summary["stages"]["sketch"]
    assert sketch["calls"] == 2
    assert sketch["failures"] == 0
    assert sketch["duration_sum_seconds"] == 60.0
    # 两次 span 的并集：100 → 150。
    assert sketch["span_seconds"] == 50.0
    render = summary["stages"]["render"]
    assert render["calls"] == 1
    assert render["failures"] == 1
    assert render["span_seconds"] == 40.0

    # since 只看起点在窗口内的事件。
    only_late = pipeline_timing.summarize_spans(out, since=155.0)
    assert set(only_late["stages"]) == {"render"}

    # 空目录不抛错。
    assert pipeline_timing.summarize_spans(tmp_path / "empty")["stages"] == {}


# ── map_bounded 按阶段热调参 ──────────────────────────────────────────────


async def _peak_concurrency(worker_count: int, **kwargs) -> int:
    from novelvideo.utils.bounded_concurrency import map_bounded

    current = 0
    peak = 0
    lock = asyncio.Lock()

    async def worker(item: int) -> int:
        nonlocal current, peak
        async with lock:
            current += 1
            peak = max(peak, current)
        await asyncio.sleep(0.02)
        async with lock:
            current -= 1
        return item

    out = await map_bounded(list(range(worker_count)), worker, gate=False, **kwargs)
    assert out == list(range(worker_count))
    return peak


async def test_map_bounded_stage_resolves_hot_concurrency(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("STRUCTURED_LLM_CONCURRENCY", raising=False)
    (tmp_path / "structured.json").write_text('{"concurrency": 3}', encoding="utf-8")

    assert await _peak_concurrency(9, stage="structured") == 3
    # 显式 limit 优先于 stage
    assert await _peak_concurrency(9, stage="structured", limit=2) == 2
    # env 优先于文件
    monkeypatch.setenv("STRUCTURED_LLM_CONCURRENCY", "4")
    assert await _peak_concurrency(9, stage="structured") == 4

