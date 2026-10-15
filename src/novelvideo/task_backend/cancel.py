"""Cancellation flags and cooperative cancellation helpers for project tasks."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

from novelvideo.ports import get_cancellation_store
from novelvideo.ports.tasks import cancel_key
from novelvideo.task_backend.task_clock import TaskClock, current_task_clock

logger = logging.getLogger(__name__)


class TaskCancelled(Exception):
    """Raised when a runner's watcher observes a Redis cancel flag mid-flight.

    Belongs here(not in any specific runner module)so the generic celery
    entry can catch it without importing concrete runners — the entry point
    in `celery_tasks.py:run_project_task` handles this exception to mark the
    task as `cancelled` instead of `failed`.
    """


class TaskTimedOut(Exception):
    """Raised when a project task exceeds its cooperative execution deadline.

    ``reason`` distinguishes the configured hard runtime cap (``"max_runtime"``)
    from the legacy frozen-deadline path (``"idle"``) so the failure message can
    describe what actually happened.
    """

    def __init__(
        self,
        *,
        timeout_seconds: int | None = None,
        reason: str = "idle",
    ) -> None:
        self.timeout_seconds = timeout_seconds or 30 * 60
        self.reason = reason
        super().__init__(self.timeout_seconds)


async def request_cancel(
    *,
    project_id: str,
    task_type: str,
    episode: int,
    task_id: str,
    beat_num: int | None = None,
    scope: str | None = None,
    ttl_seconds: int = 86_400,
) -> None:
    await get_cancellation_store().request_cancel(
        project_id=project_id,
        task_type=task_type,
        episode=episode,
        task_id=task_id,
        beat_num=beat_num,
        scope=scope,
        ttl_seconds=ttl_seconds,
    )


async def is_cancel_requested(
    *,
    project_id: str,
    task_type: str,
    episode: int,
    task_id: str,
    beat_num: int | None = None,
    scope: str | None = None,
) -> bool:
    return await get_cancellation_store().is_cancel_requested(
        project_id=project_id,
        task_type=task_type,
        episode=episode,
        task_id=task_id,
        beat_num=beat_num,
        scope=scope,
    )


async def await_with_cancel_watch(
    coro,
    *,
    project_id: str,
    task_type: str,
    episode: int,
    task_id: str,
    beat_num: int | None = None,
    scope: str | None = None,
    deadline_monotonic: float | None = None,
    timeout_seconds: int | None = None,
    clock: TaskClock | None = None,
    poll_seconds: float = 0.5,
):
    """Await a coroutine while polling the project task cancel flag.

    Celery revoke/terminate is process-level and may not interrupt an active
    async HTTP await quickly. Async runners should wrap their long-running
    coroutine with this helper so user cancellation raises TaskCancelled.

    When ``clock`` is supplied its *rolling* deadline is consulted on every
    poll, so a task that keeps reporting progress is never marked timed out;
    only an idle (no-progress) window trips the timeout.
    """
    if (
        not task_id
        and deadline_monotonic is None
        and (clock is None or not clock.enabled)
    ):
        return await coro

    main_task = asyncio.create_task(coro)
    stop_reason: str | None = None

    async def _watch() -> None:
        nonlocal stop_reason
        try:
            while not main_task.done():
                if clock is not None:
                    if clock.hard_cap_reached():
                        stop_reason = "timeout"
                        main_task.cancel()
                        return
                    if clock.is_stalled():
                        # An idle gap is not fatal: fire a one-time notice and
                        # keep the task running, so a genuinely long step is
                        # never abandoned by mistake. The user decides.
                        clock.note_progress()
                        clock.stall_notices += 1
                        handler = clock.on_stall
                        if handler is not None:
                            try:
                                await handler(clock)
                            except Exception:  # noqa: BLE001
                                logger.exception("project task stall handler failed")
                        continue
                elif (
                    deadline_monotonic is not None
                    and time.monotonic() >= deadline_monotonic
                ):
                    stop_reason = "timeout"
                    main_task.cancel()
                    return
                try:
                    cancelled = (
                        await is_cancel_requested(
                            project_id=project_id,
                            task_type=task_type,
                            episode=episode,
                            task_id=task_id,
                            beat_num=beat_num,
                            scope=scope,
                        )
                        if task_id
                        else False
                    )
                except Exception:
                    cancelled = False
                if cancelled:
                    stop_reason = "cancelled"
                    main_task.cancel()
                    return
                sleep_seconds = poll_seconds
                if clock is None and deadline_monotonic is not None:
                    sleep_seconds = min(
                        sleep_seconds,
                        max(deadline_monotonic - time.monotonic(), 0.0),
                    )
                await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            pass

    watcher = asyncio.create_task(_watch())
    try:
        return await main_task
    except asyncio.CancelledError:
        if stop_reason == "timeout":
            if clock is not None:
                capped = clock.max_runtime_seconds > 0
                raise TaskTimedOut(
                    timeout_seconds=clock.max_runtime_seconds or timeout_seconds,
                    reason="max_runtime" if capped else "idle",
                )
            raise TaskTimedOut(timeout_seconds=timeout_seconds, reason="idle")
        raise TaskCancelled()
    finally:
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def remaining_timeout_seconds(
    envelope: dict[str, Any],
    *,
    default_seconds: int | None = None,
) -> int | None:
    """Return seconds remaining before this task's cooperative deadline."""

    clock = current_task_clock()
    if clock is not None:
        # Idle gaps are non-fatal and never cap a subprocess; only an explicitly
        # configured hard runtime cap can bound a child process.
        remaining = clock.remaining_seconds()
        if remaining is None:
            return default_seconds
        if remaining <= 0:
            raise TaskTimedOut(
                timeout_seconds=clock.max_runtime_seconds,
                reason="max_runtime",
            )
        deadline_seconds = max(math.ceil(remaining), 1)
        if default_seconds is None:
            return deadline_seconds
        return max(min(int(default_seconds), deadline_seconds), 1)

    deadline_monotonic = _optional_float(envelope.get("__deadline_monotonic"))
    timeout_seconds = _optional_int(envelope.get("__timeout_seconds"))
    if deadline_monotonic is None:
        return default_seconds
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise TaskTimedOut(timeout_seconds=timeout_seconds)
    deadline_seconds = max(math.ceil(remaining), 1)
    if default_seconds is None:
        return deadline_seconds
    return max(min(int(default_seconds), deadline_seconds), 1)


def _envelope_cancel_fields(
    envelope: dict[str, Any],
    *,
    task_type: str | None = None,
    episode: int | None = None,
    beat_num: int | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    payload = envelope.get("payload") or {}
    resolved_task_type = str(task_type or envelope.get("task_type") or "")
    resolved_episode = int(
        episode
        if episode is not None
        else envelope.get("episode") or payload.get("episode") or 0
    )
    resolved_beat_num = (
        beat_num
        if beat_num is not None
        else _optional_int(envelope.get("beat_num"))
        if envelope.get("beat_num") is not None
        else _optional_int(payload.get("beat_num"))
    )
    resolved_scope = str(
        scope
        if scope is not None
        else envelope.get("scope") or payload.get("scope") or payload.get("job_id") or ""
    )
    return {
        "project_id": str(envelope.get("project_id") or ""),
        "task_type": resolved_task_type,
        "episode": resolved_episode,
        "task_id": str(envelope.get("__run_task_id") or ""),
        "beat_num": resolved_beat_num,
        "scope": resolved_scope or None,
    }


async def await_envelope_with_cancel_watch(
    coro,
    envelope: dict[str, Any],
    *,
    task_type: str | None = None,
    episode: int | None = None,
    beat_num: int | None = None,
    scope: str | None = None,
):
    """Await a runner coroutine using the Celery envelope cancel identity."""
    fields = _envelope_cancel_fields(
        envelope,
        task_type=task_type,
        episode=episode,
        beat_num=beat_num,
        scope=scope,
    )
    return await await_with_cancel_watch(
        coro,
        **fields,
        deadline_monotonic=_optional_float(envelope.get("__deadline_monotonic")),
        timeout_seconds=_optional_int(envelope.get("__timeout_seconds")),
        clock=current_task_clock(),
    )


def raise_if_envelope_cancel_requested(
    envelope: dict[str, Any],
    *,
    task_type: str | None = None,
    episode: int | None = None,
    beat_num: int | None = None,
    scope: str | None = None,
) -> None:
    """Synchronous cancellation checkpoint for non-async runner boundaries."""
    clock = current_task_clock()
    if clock is not None:
        # Idle gaps are non-fatal at synchronous checkpoints too: only the
        # explicitly configured hard runtime cap can terminate the task here.
        if clock.hard_cap_reached():
            raise TaskTimedOut(
                timeout_seconds=clock.max_runtime_seconds,
                reason="max_runtime",
            )
    else:
        deadline_monotonic = _optional_float(envelope.get("__deadline_monotonic"))
        if (
            deadline_monotonic is not None
            and time.monotonic() >= deadline_monotonic
        ):
            raise TaskTimedOut(
                timeout_seconds=_optional_int(envelope.get("__timeout_seconds"))
            )

    fields = _envelope_cancel_fields(
        envelope,
        task_type=task_type,
        episode=episode,
        beat_num=beat_num,
        scope=scope,
    )
    if not fields["task_id"]:
        return
    if asyncio.run(is_cancel_requested(**fields)):
        raise TaskCancelled()
