"""剧本处理（内置制作流程包阶段 1—2）端点。

一键流程在导入之后自动跑这一阶段；这里的端点提供手动触发与结果读取，
让用户可以不依赖一键流程单独处理/重跑剧本。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from novelvideo.api.auth import get_api_user, require_scope
from novelvideo.api.deps import resolve_project_scope
from novelvideo.api.schemas import ScriptProcessStart
from novelvideo.novel_source import has_imported_novel, novel_import_required_response
from novelvideo.script_process import (
    has_script_process_result,
    load_script_process_result,
    read_production_script,
)
from novelvideo.task_backend.client import enqueue_project_task
from novelvideo.task_identity import project_task_state_key
from novelvideo.task_state import get_task_manager

logger = logging.getLogger("novelvideo.api.script_process")

TASK_TYPE = "script_process"

router = APIRouter()


def _task_summary(ctx: Any) -> dict[str, Any]:
    if ctx is None:
        return {"status": "idle", "task_id": "", "progress": 0.0, "current_task": ""}
    state = get_task_manager().get_task_for_project(ctx, TASK_TYPE, 0)
    if state is None:
        return {"status": "idle", "task_id": "", "progress": 0.0, "current_task": ""}
    return {
        "status": state.status,
        "task_id": state.task_id,
        "progress": float(state.progress or 0.0),
        "current_task": str(state.current_task or ""),
    }


@router.get("/projects/{project}/script-process")
async def get_script_process(
    project: str,
    include_script: bool = Query(False, description="是否返回制作剧本全文"),
    user: dict = Depends(get_api_user),
):
    """读取剧本处理状态与结果（不触发任何生成）。"""

    resolved = await resolve_project_scope(project, user)
    has_result = has_script_process_result(resolved.output_dir)
    payload: dict[str, Any] = {
        "ok": True,
        "has_result": has_result,
        "task": _task_summary(resolved.ctx),
        "result": load_script_process_result(resolved.output_dir),
    }
    if include_script:
        payload["production_script"] = read_production_script(resolved.output_dir)
    return payload


@router.post("/projects/{project}/script-process/start")
async def start_script_process(
    project: str,
    body: ScriptProcessStart | None = None,
    user: dict = Depends(require_scope("tasks:submit")),
):
    """手动触发（或强制重跑）剧本处理。"""

    body = body or ScriptProcessStart()
    resolved = await resolve_project_scope(project, user, required_role="editor")
    ctx = resolved.ctx
    if ctx is None:
        return {"ok": False, "error": "剧本处理需要 project context"}
    if not has_imported_novel(resolved.output_dir):
        return novel_import_required_response()
    if not body.force and has_script_process_result(resolved.output_dir):
        return {
            "ok": True,
            "already_done": True,
            "task": _task_summary(ctx),
            "message": "剧本处理已完成；force=true 可重跑",
        }

    logger.info("[%s] start_script_process force=%s", project, body.force)
    payload: dict[str, Any] = {"output_dir": resolved.output_dir}
    if body.force:
        payload["force"] = True
    if body.spine_template is not None:
        payload["spine_template"] = body.spine_template

    queued = await enqueue_project_task(
        ctx,
        task_type=TASK_TYPE,
        product_surface="mainline",
        queue_kind="default",
        episode=0,
        payload=payload,
    )
    return {
        "ok": True,
        "task_type": TASK_TYPE,
        "task_id": queued.task_state.task_id,
        "task_key": project_task_state_key(TASK_TYPE, ctx.project_id, 0),
        "backend": queued.backend,
        "queue": queued.queue,
        "message": "剧本处理任务已进入队列",
    }
