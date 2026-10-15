"""Progress-aware task watchdog coverage.

These tests pin the behaviour that replaced the old fixed wall-clock budget:

* an idle gap only fires a stall notice and never kills the task, and
* only an explicitly configured hard runtime cap terminates a task.
"""

import asyncio
import time

import pytest

from novelvideo.task_backend import cancel as cancel_module
from novelvideo.task_backend import task_clock
from novelvideo.task_backend.cancel import TaskTimedOut
from novelvideo.task_backend.task_clock import TaskClock, project_task_clock


def test_task_clock_idle_resets_and_hard_cap_is_independent() -> None:
    clock = TaskClock(idle_seconds=10, max_runtime_seconds=3600)
    clock.last_progress_monotonic = time.monotonic() - 5
    before = clock.deadline_monotonic
    assert before is not None

    clock.note_progress()

    after = clock.deadline_monotonic
    assert after is not None
    assert after > before
    assert clock.hard_deadline_monotonic == pytest.approx(
        clock.started_monotonic + 3600
    )
    assert clock.hard_cap_reached() is False


def test_task_clock_disabled_idle_never_stalls() -> None:
    clock = TaskClock(idle_seconds=0)
    clock.last_progress_monotonic -= 10_000

    assert clock.deadline_monotonic is None
    assert clock.is_stalled() is False
    assert clock.hard_deadline_monotonic is None
    assert clock.hard_cap_reached() is False


def test_resolve_max_runtime_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ST_PROJECT_TASK_MAX_RUNTIME_S", raising=False)
    assert task_clock.resolve_max_runtime_seconds() == 0


def test_resolve_idle_honours_legacy_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ST_PROJECT_TASK_IDLE_TIMEOUT_S", raising=False)
    monkeypatch.setenv("ST_PROJECT_TASK_TIMEOUT_S", "42")
    assert task_clock.resolve_idle_timeout_seconds() == 42


def test_remaining_timeout_seconds_ignores_idle_and_uses_default() -> None:
    with project_task_clock(10):
        # No hard cap -> the idle window must not bound a subprocess.
        assert cancel_module.remaining_timeout_seconds({}, default_seconds=60) == 60


def test_remaining_timeout_seconds_caps_by_hard_runtime() -> None:
    with project_task_clock(0, max_runtime_seconds=120):
        remaining = cancel_module.remaining_timeout_seconds({}, default_seconds=600)

    assert remaining is not None
    assert 1 <= remaining <= 120


def test_raise_if_envelope_cancel_requested_ignores_idle_with_clock() -> None:
    with project_task_clock(30) as clock:
        clock.last_progress_monotonic = time.monotonic() - 31
        # The idle window is long past, but idle is non-fatal: no task id means
        # the cancel-flag lookup is also skipped, so this must simply return.
        cancel_module.raise_if_envelope_cancel_requested(
            {
                "project_id": "p",
                "task_type": "t",
                "episode": 0,
                "__deadline_monotonic": 0.0,
            }
        )


def test_raise_if_envelope_cancel_requested_trips_on_hard_cap() -> None:
    with project_task_clock(0, max_runtime_seconds=30) as clock:
        clock.started_monotonic = time.monotonic() - 31
        with pytest.raises(TaskTimedOut) as exc_info:
            cancel_module.raise_if_envelope_cancel_requested(
                {"project_id": "p", "task_type": "t", "episode": 0}
            )

    assert exc_info.value.reason == "max_runtime"


async def test_cancel_watch_fires_stall_notice_and_keeps_running() -> None:
    notices: list[int] = []

    async def _on_stall(clock: TaskClock) -> None:
        notices.append(clock.stall_notices)

    async def slow_work() -> str:
        await asyncio.sleep(0.25)
        return "done"

    with project_task_clock(1) as clock:
        clock.on_stall = _on_stall
        clock.last_progress_monotonic = time.monotonic() - 2
        result = await asyncio.wait_for(
            cancel_module.await_with_cancel_watch(
                slow_work(),
                project_id="p",
                task_type="t",
                episode=0,
                task_id="x",
                clock=clock,
                poll_seconds=0.01,
            ),
            timeout=5,
        )

    assert result == "done"
    assert notices == [1]


async def test_cancel_watch_hard_cap_aborts() -> None:
    async def slow_work() -> None:
        await asyncio.sleep(1)

    with project_task_clock(0, max_runtime_seconds=30) as clock:
        clock.started_monotonic = time.monotonic() - 31
        with pytest.raises(TaskTimedOut) as exc_info:
            await cancel_module.await_with_cancel_watch(
                slow_work(),
                project_id="p",
                task_type="t",
                episode=0,
                task_id="x",
                clock=clock,
                poll_seconds=0.005,
            )

    assert exc_info.value.reason == "max_runtime"
    assert exc_info.value.timeout_seconds == 30


async def test_cancel_watch_legacy_deadline_still_times_out() -> None:
    async def slow_work() -> None:
        await asyncio.sleep(1)

    with pytest.raises(TaskTimedOut):
        await cancel_module.await_with_cancel_watch(
            slow_work(),
            project_id="p",
            task_type="t",
            episode=0,
            task_id="x",
            deadline_monotonic=0.0,
            poll_seconds=0.005,
        )
