"""Progress-aware execution clock for project tasks.

A project task used to be killed once its **total** wall-clock runtime exceeded
a fixed budget (default 30 minutes).  That punished legitimately long work — a
full episode "one-click" pipeline easily runs past half an hour even while it is
making steady progress, and the task was abandoned exactly when it was still
healthy.

This module replaces the wall-clock budget with an **idle** budget.  The clock is
reset every time the runner reports progress, so a task may run for hours as long
as it keeps moving; only a task that goes completely silent for the idle window
is treated as stalled.

The clock is carried through the execution context so that:

* the async cancel watcher (``cancel.py``) trips on idle stalls,
* ``update_progress_for_project`` refreshes the clock on every progress report,
* subprocess timeouts (``subprocesses.py``) use the remaining idle budget, and
* ``run_core`` seeds the clock once per task execution.

Set ``ST_PROJECT_TASK_IDLE_TIMEOUT_S`` (seconds) to change the idle window; ``0``
disables the watchdog entirely so tasks only stop on cancellation or completion.
``ST_PROJECT_TASK_TIMEOUT_S`` is still honoured as a backward-compatible alias.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

logger = logging.getLogger(__name__)

#: Default idle window before a silent task is considered stalled.
DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60


def resolve_idle_timeout_seconds() -> int:
    """Return the idle window (seconds) before a silent task is stalled.

    ``0`` disables the watchdog.  Invalid values fall back to the default and are
    logged, matching the previous timeout-configuration behaviour.
    """

    for name in (
        "ST_PROJECT_TASK_IDLE_TIMEOUT_S",
        "ST_PROJECT_TASK_TIMEOUT_S",
    ):
        raw_value = os.environ.get(name)
        if raw_value is None or raw_value == "":
            continue
        try:
            return max(int(raw_value), 0)
        except ValueError:
            logger.warning(
                "Invalid %s=%r; using default idle timeout", name, raw_value
            )
    return DEFAULT_IDLE_TIMEOUT_SECONDS


@dataclass
class TaskClock:
    """A rolling deadline that resets whenever the task reports progress."""

    idle_seconds: int
    last_progress_monotonic: float = field(default_factory=time.monotonic)
    started_monotonic: float = field(default_factory=time.monotonic)

    @property
    def enabled(self) -> bool:
        return self.idle_seconds > 0

    def note_progress(self) -> None:
        """Reset the idle timer; called on every task progress report."""

        self.last_progress_monotonic = time.monotonic()

    def idle_elapsed_seconds(self) -> float:
        return time.monotonic() - self.last_progress_monotonic

    @property
    def deadline_monotonic(self) -> float | None:
        """Current rolling deadline, or ``None`` when the watchdog is disabled."""

        if not self.enabled:
            return None
        return self.last_progress_monotonic + self.idle_seconds

    def remaining_seconds(self) -> float | None:
        deadline = self.deadline_monotonic
        if deadline is None:
            return None
        return max(deadline - time.monotonic(), 0.0)

    def is_stalled(self) -> bool:
        return self.enabled and self.idle_elapsed_seconds() >= self.idle_seconds


_CURRENT_TASK_CLOCK: ContextVar[TaskClock | None] = ContextVar(
    "novelvideo_project_task_clock", default=None
)


def current_task_clock() -> TaskClock | None:
    return _CURRENT_TASK_CLOCK.get()


def note_task_progress() -> None:
    """Refresh the active task clock (no-op outside a task execution context)."""

    clock = _CURRENT_TASK_CLOCK.get()
    if clock is not None:
        clock.note_progress()


@contextmanager
def project_task_clock(idle_seconds: int) -> Iterator[TaskClock]:
    """Install a fresh :class:`TaskClock` for the current task execution."""

    clock = TaskClock(idle_seconds=max(int(idle_seconds), 0))
    token = _CURRENT_TASK_CLOCK.set(clock)
    try:
        yield clock
    finally:
        _CURRENT_TASK_CLOCK.reset(token)
