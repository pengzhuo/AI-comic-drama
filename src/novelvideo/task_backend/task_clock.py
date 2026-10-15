"""Progress-aware execution watchdog for project tasks.

A project task used to be killed once its **total** wall-clock runtime exceeded
a fixed budget (default 30 minutes).  That punished legitimately long work — a
full episode "one-click" pipeline easily runs past half an hour even while it is
making steady progress, and the task was abandoned exactly when it was healthy.

This module replaces the wall-clock budget with two separate concepts:

* **Idle monitoring** — a rolling clock that is reset on every progress report.
  When it goes silent for the idle window it fires a one-time *stall notice* and
  keeps the task running.  It never kills the task: an idle gap may just be a
  single long provider call that does not emit progress callbacks.
* **Hard cap** — an optional, explicitly configured absolute runtime
  (``ST_PROJECT_TASK_MAX_RUNTIME_S``, ``0`` = off) that is the only automatic
  termination left.  It exists purely as an operator safety net.

The clock is carried through the execution context so that:

* the async cancel watcher (``cancel.py``) fires stall notices and honours the
  hard cap,
* ``update_progress_for_project`` refreshes the clock on every progress report,
* subprocess timeouts (``subprocesses.py``) honour only the hard cap, and
* ``run_core`` seeds the clock and installs the stall-notice handler.

Set ``ST_PROJECT_TASK_IDLE_TIMEOUT_S`` (seconds) to change the idle window
(``0`` disables idle monitoring).  ``ST_PROJECT_TASK_TIMEOUT_S`` is still
honoured as a backward-compatible alias for the idle window.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Default idle window before a silent task triggers a stall notice.
DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60
#: Default absolute runtime cap; ``0`` means "no hard cap".
DEFAULT_MAX_RUNTIME_SECONDS = 0

StallHandler = Callable[["TaskClock"], Awaitable[None]]


def _resolve_seconds(name: str) -> int | None:
    raw_value = os.environ.get(name)
    if raw_value is None or raw_value == "":
        return None
    try:
        return max(int(raw_value), 0)
    except ValueError:
        logger.warning("Invalid %s=%r; ignoring", name, raw_value)
        return None


def resolve_idle_timeout_seconds() -> int:
    """Return the idle window (seconds) before a silent task is flagged.

    ``0`` disables idle monitoring.  Invalid values fall back to the default and
    are logged, matching the previous timeout-configuration behaviour.
    """

    for name in (
        "ST_PROJECT_TASK_IDLE_TIMEOUT_S",
        "ST_PROJECT_TASK_TIMEOUT_S",
    ):
        value = _resolve_seconds(name)
        if value is not None:
            return value
    return DEFAULT_IDLE_TIMEOUT_SECONDS


def resolve_max_runtime_seconds() -> int:
    """Return the absolute runtime cap (seconds); ``0`` disables it.

    This is the only automatic termination left.  It is deliberately off by
    default because a task that keeps reporting progress is expected to be
    allowed to run for as long as it needs.
    """

    value = _resolve_seconds("ST_PROJECT_TASK_MAX_RUNTIME_S")
    return DEFAULT_MAX_RUNTIME_SECONDS if value is None else value


@dataclass
class TaskClock:
    """A rolling idle clock plus an optional hard runtime cap."""

    idle_seconds: int
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS
    last_progress_monotonic: float = field(default_factory=time.monotonic)
    started_monotonic: float = field(default_factory=time.monotonic)
    #: Async callback fired once per idle window; the task keeps running.
    on_stall: StallHandler | None = None
    #: Number of stall notices fired so far (diagnostics/tests).
    stall_notices: int = 0

    @property
    def idle_enabled(self) -> bool:
        return self.idle_seconds > 0

    @property
    def enabled(self) -> bool:
        """Whether the clock enforces anything (idle or hard cap)."""

        return self.idle_enabled or self.hard_deadline_monotonic is not None

    def note_progress(self) -> None:
        """Reset the idle timer; called on every task progress report."""

        self.last_progress_monotonic = time.monotonic()

    def idle_elapsed_seconds(self) -> float:
        return time.monotonic() - self.last_progress_monotonic

    @property
    def deadline_monotonic(self) -> float | None:
        """Current rolling idle deadline, or ``None`` when idle is disabled."""

        if not self.idle_enabled:
            return None
        return self.last_progress_monotonic + self.idle_seconds

    @property
    def hard_deadline_monotonic(self) -> float | None:
        """Absolute runtime deadline, or ``None`` when no hard cap is set."""

        if self.max_runtime_seconds <= 0:
            return None
        return self.started_monotonic + self.max_runtime_seconds

    def total_elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic

    def is_stalled(self) -> bool:
        return self.idle_enabled and self.idle_elapsed_seconds() >= self.idle_seconds

    def hard_cap_reached(self) -> bool:
        deadline = self.hard_deadline_monotonic
        return deadline is not None and time.monotonic() >= deadline

    def remaining_seconds(self) -> float | None:
        """Seconds left before the hard cap, or ``None`` when uncapped."""

        deadline = self.hard_deadline_monotonic
        if deadline is None:
            return None
        return max(deadline - time.monotonic(), 0.0)


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
def project_task_clock(
    idle_seconds: int,
    *,
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
) -> Iterator[TaskClock]:
    """Install a fresh :class:`TaskClock` for the current task execution."""

    clock = TaskClock(
        idle_seconds=max(int(idle_seconds), 0),
        max_runtime_seconds=max(int(max_runtime_seconds), 0),
    )
    token = _CURRENT_TASK_CLOCK.set(clock)
    try:
        yield clock
    finally:
        _CURRENT_TASK_CLOCK.reset(token)
