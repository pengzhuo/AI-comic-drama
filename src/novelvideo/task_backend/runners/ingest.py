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

#: Stage the import hands off to.  剧本预处理（确定性规范化）runs first and then
#: chains the bundled「通用AI影视制作流程包」script processing (阶段 1—2) itself, so
#: every later stage reads one canonical screenplay.  Chained from here instead of
#: from the REST handler so the automatic hand-off survives a closed browser tab.
SCREENPLAY_PREPROCESS_TASK_TYPE = "screenplay_preprocess"

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
    # never reads a half-written novel.  A fresh import invalidates any previous
    # normalization result, then the analysed text is recorded durably: the task
    # row above expires, the digest does not.
    _reset_preprocess_artifacts(ctx)
    remember_ingest_completion(ctx)
    await _enqueue_screenplay_preprocess(ctx)
    return result


def _reset_preprocess_artifacts(ctx: ProjectContext) -> None:
    """Drop the previous 剧本预处理 artifacts so the new import is re-processed."""

    from novelvideo.screenplay_preprocess import clear_preprocess_artifacts

    try:
        clear_preprocess_artifacts(ctx.output_dir)
    except Exception:  # noqa: BLE001 - 清理失败不该让已成功的导入失败
        logger.exception("failed to clear stale screenplay preprocess artifacts")


def remember_ingest_completion(ctx: ProjectContext) -> None:
    """把「这份 novel.txt 已经分析完」写进项目配置。

    ``pipeline_status`` 用它判断摄入阶段是否完成：只看 ``ingest_fast`` 任务行
    会让导入完成一小时后的项目重新显示「小说摄入」。

    指纹取自「系统实际消费的文本」（``novel_source.load_imported_novel_content``），
    所以剧本预处理跑完后必须再调用一次，把标记刷成规范化文本的指纹。
    """

    from novelvideo.novel_source import novel_source_digest
    from novelvideo.project_config import save_project_config_in_state_dir

    try:
        digest = novel_source_digest(ctx.output_dir)
        if not digest:
            return
        save_project_config_in_state_dir(
            ctx.state_dir, config={"ingest_source_digest": digest}
        )
    except Exception:  # noqa: BLE001 - 导入已成功,记账失败不该让它失败
        logger.exception("failed to persist durable ingest digest")


async def _enqueue_screenplay_preprocess(ctx: ProjectContext) -> None:
    """Chain 剧本预处理 after a successful import; it chains 剧本处理 itself.

    Best effort by design: the import already succeeded, so a rejected hand-off
    must never fail it.  A duplicate is harmless — the stage skips itself when a
    result already exists, and the durable one-click coordinator dispatches the
    same stage on its own.
    """

    from novelvideo.screenplay_preprocess import has_preprocess_result
    from novelvideo.task_backend.client import enqueue_project_task

    try:
        if has_preprocess_result(ctx.output_dir):
            return
        existing = get_task_manager().get_task_for_project(
            ctx, SCREENPLAY_PREPROCESS_TASK_TYPE, 0
        )
        if existing is not None and existing.status in _ACTIVE_TASK_STATUSES:
            return
        await enqueue_project_task(
            ctx,
            task_type=SCREENPLAY_PREPROCESS_TASK_TYPE,
            product_surface="mainline",
            queue_kind="default",
            episode=0,
            payload={"output_dir": str(ctx.output_dir)},
        )
    except Exception:  # noqa: BLE001 - the import result is authoritative
        logger.exception("failed to hand off screenplay preprocessing after ingest")


register_project_task_runner("ingest_fast", run_ingest_fast)
