"""Task-scoped diagnostics used to explain *why* a project task failed.

A failed project task used to surface only ``str(exc)`` plus a small
``error_code``.  That tells an operator *that* something broke, never *what the
task was doing* or *which model was called*.  This module keeps a small,
task-scoped record of the recent operations and model calls so the failure
payload can answer those questions.

The record is a mutable object stored in a :class:`contextvars.ContextVar`.
Because the *object* (not the mapping) is shared, a record created in the sync
task frame stays visible to async code that runs in a copied context -- which is
exactly how the runners invoke the model gateway.

Nothing here raises: diagnostics must never be the reason a task fails.  All
public helpers swallow their own errors and degrade to no-ops outside a task
scope.
"""

from __future__ import annotations

import re
import time
import traceback as _traceback
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

_MAX_OPERATIONS = 8
_MAX_MODEL_CALLS = 8
_MAX_MESSAGE_CHARS = 800
_MAX_TRACEBACK_CHARS = 2000
_MAX_OPERATION_CHARS = 300
_MAX_FIELD_CHARS = 300

# Best-effort scrub of secrets that could otherwise leak into task metadata (and
# from there into the UI).  Diagnostics are shown to operators, not auditors.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{8,}"), "[redacted]"),
    (
        re.compile(
            r"(?i)(?<![\w-])(api[_-]?key|authorization|access[_-]?token|"
            r"refresh[_-]?token|secret|password|passwd|token)(?![\w-])"
            r"\s*[=:]\s*[^\s,;'\"]+"
        ),
        r"\1=[redacted]",
    ),
    (re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@"), r"\1[redacted]@"),
)


def redact(text: str) -> str:
    """Remove obvious credentials from a free-form string."""

    if not text:
        return ""
    cleaned = text
    for pattern, replacement in _REDACTIONS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _clean(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    return redact(value).strip()[:limit]


@dataclass
class ModelCallRecord:
    """One provider/model invocation observed inside a task."""

    model: str = ""
    provider: str = ""
    capability: str = ""
    request_id: str = ""
    source: str = ""
    status: str = "ok"
    error: str = ""
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": _clean(self.model, _MAX_FIELD_CHARS),
            "capability": _clean(self.capability, _MAX_FIELD_CHARS),
            "status": self.status,
            "at": self.at,
        }
        for key, value in (
            ("provider", self.provider),
            ("request_id", self.request_id),
            ("source", self.source),
            ("error", self.error),
        ):
            cleaned = _clean(value, _MAX_FIELD_CHARS)
            if cleaned:
                payload[key] = cleaned
        return payload


@dataclass
class TaskDiagnostics:
    """Mutable, task-scoped record of recent operations and model calls."""

    task_type: str = ""
    episode: int = 0
    beat_num: int | None = None
    scope: str | None = None
    project_id: str = ""
    task_id: str = ""
    operation: str = ""
    started_at: float = field(default_factory=time.time)
    started_monotonic: float = field(default_factory=time.monotonic)
    operations: deque[str] = field(default_factory=lambda: deque(maxlen=_MAX_OPERATIONS))
    model_calls: deque[ModelCallRecord] = field(
        default_factory=lambda: deque(maxlen=_MAX_MODEL_CALLS)
    )

    def note_operation(self, label: Any) -> None:
        text = _clean(label, _MAX_OPERATION_CHARS)
        if not text or text == self.operation:
            return
        self.operation = text
        if not self.operations or self.operations[-1] != text:
            self.operations.append(text)

    def note_model_call(self, record: ModelCallRecord) -> None:
        self.model_calls.append(record)

    def elapsed_seconds(self) -> float:
        return max(time.monotonic() - self.started_monotonic, 0.0)

    def snapshot(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "operation": self.operation,
            "recent_operations": list(self.operations),
            "model_calls": [record.to_dict() for record in self.model_calls],
            "elapsed_seconds": round(self.elapsed_seconds(), 1),
        }
        for key, value in (
            ("task_type", self.task_type),
            ("project_id", self.project_id),
            ("task_id", self.task_id),
            ("scope", self.scope),
        ):
            if value:
                payload[key] = value
        if self.episode:
            payload["episode"] = self.episode
        if self.beat_num is not None:
            payload["beat_num"] = self.beat_num
        return payload


_CTX: ContextVar[TaskDiagnostics | None] = ContextVar(
    "novelvideo_project_task_diagnostics", default=None
)


def current_task_diagnostics() -> TaskDiagnostics | None:
    """Return the active task diagnostics record, or ``None`` outside a task."""

    return _CTX.get()


def note_operation(label: Any) -> None:
    """Record the operation a task is currently performing (no-op off-task)."""

    record = _CTX.get()
    if record is None:
        return
    try:
        record.note_operation(label)
    except Exception:  # noqa: BLE001 - diagnostics never break a task
        pass


def note_model_call(
    *,
    model: Any = "",
    provider: Any = "",
    capability: Any = "",
    request_id: Any = "",
    source: Any = "",
    status: str = "ok",
    error: Any = "",
) -> None:
    """Record a provider/model invocation (no-op outside a task execution)."""

    record = _CTX.get()
    if record is None:
        return
    try:
        record.note_model_call(
            ModelCallRecord(
                model=str(model or ""),
                provider=str(provider or ""),
                capability=str(capability or ""),
                request_id=str(request_id or ""),
                source=str(source or ""),
                status=str(status or "ok"),
                error=str(error or ""),
            )
        )
    except Exception:  # noqa: BLE001 - diagnostics never break a task
        pass


@contextmanager
def task_diagnostics_scope(
    *,
    task_type: str = "",
    episode: int = 0,
    beat_num: int | None = None,
    scope: str | None = None,
    project_id: str = "",
    task_id: str = "",
) -> Iterator[TaskDiagnostics]:
    """Install a fresh diagnostics record for the current task execution."""

    record = TaskDiagnostics(
        task_type=str(task_type or ""),
        episode=int(episode or 0),
        beat_num=beat_num,
        scope=scope,
        project_id=str(project_id or ""),
        task_id=str(task_id or ""),
    )
    token = _CTX.set(record)
    try:
        yield record
    finally:
        _CTX.reset(token)


def summarize_failure(payload: dict[str, Any] | None) -> str:
    """One-line, human-readable summary of a failure payload.

    Used to enrich chat/toast text (for example the one-click director
    notification) with the operation that was running and the model that was
    last called.  Returns an empty string when the payload says nothing useful.
    """

    if not isinstance(payload, dict) or not payload:
        return ""
    parts: list[str] = []
    operation = str(payload.get("operation") or "").strip()
    if operation:
        parts.append(f"执行中：{operation}")
    calls = payload.get("model_calls")
    if isinstance(calls, list) and calls:
        last = calls[-1]
        if isinstance(last, dict):
            model = str(last.get("model") or "").strip()
            provider = str(last.get("provider") or "").strip()
            request_id = str(last.get("request_id") or "").strip()
            status = str(last.get("status") or "").strip()
            descriptor = model or provider
            if descriptor:
                if request_id:
                    descriptor = f"{descriptor}，请求 ID {request_id}"
                if status == "error":
                    descriptor = f"{descriptor}（调用失败）"
                parts.append(f"最近调用模型：{descriptor}")
    if not parts:
        message = str(payload.get("message") or "").strip()
        if message:
            parts.append(_clean(message, _MAX_FIELD_CHARS))
    return "；".join(parts)


def build_failure_diagnostics(
    exc: BaseException,
    *,
    task_id: str = "",
    message: str | None = None,
) -> dict[str, Any]:
    """Build the structured ``metadata['failure']`` payload for a task failure.

    Combines the exception identity/message/traceback with whatever the task
    recorded about its recent operations and model calls.
    """

    text = message if message is not None else str(exc)
    payload: dict[str, Any] = {
        "exception_type": type(exc).__name__,
        "message": _clean(text, _MAX_MESSAGE_CHARS),
    }
    try:
        payload["traceback"] = _clean(
            "".join(
                _traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
            _MAX_TRACEBACK_CHARS,
        )
    except Exception:  # noqa: BLE001
        payload["traceback"] = ""
    record = _CTX.get()
    if record is not None:
        payload.update(record.snapshot())
    if task_id and not payload.get("task_id"):
        payload["task_id"] = task_id
    return payload
