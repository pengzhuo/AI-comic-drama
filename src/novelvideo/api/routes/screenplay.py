"""剧本预处理（导入 → 规范化 → 技能处理）端点。

一键流程在导入之后、剧本处理之前自动跑这一阶段；这里的端点提供手动触发与结果
读取，让存量项目（导入时还没有这个阶段）也能把剧本规范化一次。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from novelvideo.api.auth import get_api_user, require_scope
from novelvideo.api.deps import resolve_project_scope
from novelvideo.api.schemas import ScreenplayPreprocessStart
from novelvideo.novel_source import has_imported_novel, novel_import_required_response
from novelvideo.screenplay_preprocess import (
    has_preprocess_result,
    load_preprocess_result,
    read_normalized_novel,
)
from novelvideo.task_backend.client import enqueue_project_task
from novelvideo.task_identity import project_task_state_key
from novelvideo.task_state import get_task_manager

logger = logging.getLogger("novelvideo.api.screenplay")

TASK_TYPE = "screenplay_preprocess"

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


@router.get("/projects/{project}/screenplay/preprocess")
async def get_screenplay_preprocess(
    project: str,
    include_text: bool = Query(False, description="是否返回规范化后的全文"),
    user: dict = Depends(get_api_user),
):
    """读取剧本预处理状态与结果（不触发任何处理）。"""

    resolved = await resolve_project_scope(project, user)
    has_result = has_preprocess_result(resolved.output_dir)
    payload: dict[str, Any] = {
        "ok": True,
        "has_result": has_result,
        "task": _task_summary(resolved.ctx),
        "result": load_preprocess_result(resolved.output_dir),
    }
    if include_text:
        payload["normalized_text"] = read_normalized_novel(resolved.output_dir)
    return payload


@router.post("/projects/{project}/screenplay/preprocess/start")
async def start_screenplay_preprocess(
    project: str,
    body: ScreenplayPreprocessStart | None = None,
    user: dict = Depends(require_scope("tasks:submit")),
):
    """手动触发（或强制重跑）剧本预处理。"""

    body = body or ScreenplayPreprocessStart()
    resolved = await resolve_project_scope(project, user, required_role="editor")
    ctx = resolved.ctx
    if ctx is None:
        return {"ok": False, "error": "剧本预处理需要 project context"}
    if not has_imported_novel(resolved.output_dir):
        return novel_import_required_response()
    if not body.force and has_preprocess_result(resolved.output_dir):
        return {
            "ok": True,
            "already_done": True,
            "task": _task_summary(ctx),
            "message": "剧本预处理已完成；force=true 可重跑",
        }

    logger.info("[%s] start_screenplay_preprocess force=%s", project, body.force)
    payload: dict[str, Any] = {"output_dir": resolved.output_dir}
    if body.force:
        payload["force"] = True
    payload["apply_to_episodes"] = bool(body.apply_to_episodes)

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
        "message": "剧本预处理任务已进入队列",
    }
