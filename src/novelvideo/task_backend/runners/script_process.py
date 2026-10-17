"""Celery runner for the film-process-pack script processing stage.

Runs right after ``ingest_fast``: it applies the bundled
「通用AI影视制作流程包」(阶段 1—2) to the imported script and persists the
structured production script that the rest of the pipeline can reuse.
"""

from __future__ import annotations

import asyncio
from typing import Any

from novelvideo.i18n_message import MessageLike, lmsg
from novelvideo.model_gateway_runtime import model_gateway_scope_for_runner
from novelvideo.project_context import ProjectContext
from novelvideo.task_backend.cancel import await_envelope_with_cancel_watch
from novelvideo.task_backend.registry import register_project_task_runner
from novelvideo.task_state import get_task_manager

TASK_TYPE = "script_process"


def run_script_process(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    with model_gateway_scope_for_runner(envelope):
        return asyncio.run(
            await_envelope_with_cancel_watch(
                _run_script_process(envelope, ctx),
                envelope,
                task_type=TASK_TYPE,
            )
        )


async def _run_script_process(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    from novelvideo.novel_source import require_imported_novel
    from novelvideo.project_config import load_project_config_from_state_dir
    from novelvideo.script_process import (
        has_script_process_result,
        load_script_process_result,
    )
    from novelvideo.workflows.script_processing import ScriptProcessWorkflow

    payload = envelope.get("payload") or {}
    output_dir = str(payload.get("output_dir") or ctx.output_dir)
    force = bool(payload.get("force"))
    manager = get_task_manager()

    def update_progress(progress: float, task: MessageLike) -> None:
        manager.update_progress_for_project(
            ctx,
            TASK_TYPE,
            0,
            progress=progress,
            current_task=task,
            logs=[task],
        )

    def log_only(message: str) -> None:
        manager.update_progress_for_project(ctx, TASK_TYPE, 0, logs=[message])

    if not force and has_script_process_result(output_dir):
        log_only("[ScriptProcess] 已存在处理结果，本次跳过（force=true 可重跑）")
        return {
            "ok": True,
            "skipped": True,
            "output_dir": output_dir,
            "result": load_script_process_result(output_dir),
        }

    update_progress(
        0.02,
        lmsg(
            "tasks.progress.scriptProcess.start",
            "开始按制作流程包处理剧本（全文读审 → 结构化制作剧本）...",
        ),
    )

    source_text = require_imported_novel(output_dir)
    project_config = load_project_config_from_state_dir(
        ctx.state_dir,
        username=ctx.owner_username,
        project=ctx.project_name,
    )
    spine_template = (
        str(payload.get("spine_template") or "").strip()
        or str(project_config.get("spine_template") or "").strip()
        or "drama"
    )

    workflow = ScriptProcessWorkflow(
        output_dir=output_dir,
        spine_template=spine_template,
    )
    result = await workflow.run(
        source_text=source_text,
        on_progress=update_progress,
        on_log=log_only,
    )
    return {
        "ok": True,
        "output_dir": output_dir,
        "chunks": result.chunks,
        "degraded_chunks": result.degraded_chunks,
        "source_chars": result.source_chars,
        "characters": len(result.characters),
        "scenes": len(result.scenes),
        "gaps": len(result.gaps),
        "result_path": result.result_path,
    }


register_project_task_runner(TASK_TYPE, run_script_process)
