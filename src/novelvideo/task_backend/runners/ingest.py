"""Celery runner for fast novel ingest."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from novelvideo.i18n_message import MessageLike
from novelvideo.model_gateway_runtime import model_gateway_scope_for_runner
from novelvideo.project_context import ProjectContext
from novelvideo.task_backend.cancel import await_envelope_with_cancel_watch
from novelvideo.task_backend.registry import register_project_task_runner
from novelvideo.task_state import get_task_manager

logger = logging.getLogger(__name__)

#: Stage the import hands off to.  The bundled「通用AI影视制作流程包」script
#: processing (阶段 1—2) is what every later stage reads, and it is chained from
#: here instead of from the REST handler so the automatic hand-off survives a
#: closed browser tab.
SCRIPT_PROCESS_TASK_TYPE = "script_process"

#: A queued/running stage must not be duplicated by the hand-off below.
_ACTIVE_TASK_STATUSES = frozenset(
    {"submitting", "queued", "pending", "starting", "running"}
)


def run_ingest_fast(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    with model_gateway_scope_for_runner(envelope):
        return asyncio.run(
            await_envelope_with_cancel_watch(
                _run_ingest_fast(envelope, ctx),
                envelope,
                task_type="ingest_fast",
            )
        )


async def _run_ingest_fast(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    from novelvideo.knowledge_pipeline import is_structured_pipeline

    payload = envelope.get("payload") or {}
    novel_path = str(payload["novel_path"])
    config = dict(payload.get("config") or {})
    manager = get_task_manager()

    # structured_v1 imports never build a graph, so they open the project with
    # SQLiteStore directly rather than through the Cognee facade.
    structured = is_structured_pipeline(ctx.state_dir)
    if structured:
        from novelvideo.sqlite_store import SQLiteStore

        store = SQLiteStore(
            ctx.owner_project_label,
            output_dir=str(ctx.output_dir),
            state_dir=str(ctx.state_dir),
        )
    else:
        from novelvideo.cognee import CogneeStore

        store = CogneeStore(
            ctx.owner_project_label,
            output_dir=str(ctx.output_dir),
            state_dir=str(ctx.state_dir),
        )
    await store.initialize()

    def update(progress: float | None, task: MessageLike) -> None:
        """Persist a progress milestone or a log-only status update.

        Cognee emits log messages between the explicit ingest milestones.  A log
        message does not carry progress, so ``None`` preserves the last reported
        value instead of resetting the progress bar to zero.
        """
        manager.update_progress_for_project(
            ctx,
            "ingest_fast",
            0,
            progress=progress,
            current_task=task,
            logs=[task],
        )

    try:
        if structured:
            from novelvideo.structured_ingest import ingest_source_text_structured

            result = await ingest_source_text_structured(
                store,
                novel_path,
                spine_template=str(config.get("spine_template") or "").strip()
                or None,
                on_progress=update,
                on_log=lambda message: update(None, message),
            )
        else:
            result = await store.ingest_novel_fast(
                novel_path,
                rebuild=bool(config.get("rebuild", False)),
                spine_template=str(config.get("spine_template") or "").strip() or None,
                on_progress=update,
                on_log=lambda message: update(None, message),
            )
    finally:
        await store.close()

    # The import itself is done and persisted at this point, so the next stage
    # never reads a half-written novel.
    await _enqueue_script_process(ctx)
    return result


async def _enqueue_script_process(ctx: ProjectContext) -> None:
    """Chain the film-process-pack script processing after a successful import.

    Best effort by design: the import already succeeded, so a rejected hand-off
    must never fail it.  A duplicate is harmless — the stage skips itself when a
    result already exists, and the durable one-click coordinator dispatches the
    same stage on its own.
    """

    from novelvideo.script_process import has_script_process_result
    from novelvideo.task_backend.client import enqueue_project_task

    try:
        if has_script_process_result(ctx.output_dir):
            return
        existing = get_task_manager().get_task_for_project(
            ctx, SCRIPT_PROCESS_TASK_TYPE, 0
        )
        if existing is not None and existing.status in _ACTIVE_TASK_STATUSES:
            return
        await enqueue_project_task(
            ctx,
            task_type=SCRIPT_PROCESS_TASK_TYPE,
            product_surface="mainline",
            queue_kind="default",
            episode=0,
            payload={"output_dir": str(ctx.output_dir)},
        )
    except Exception:  # noqa: BLE001 - the import result is authoritative
        logger.exception("failed to hand off script processing after ingest")


register_project_task_runner("ingest_fast", run_ingest_fast)
