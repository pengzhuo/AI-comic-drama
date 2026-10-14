"""流水线聚合状态端点。"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from novelvideo.api.auth import get_api_user
from novelvideo.api.deps import get_sqlite_store, resolve_project_scope
from novelvideo.pipeline_status import (
    STEP_MAP as _STEP_MAP,
    _beat_audio_series_complete,
    _beat_has_script_content,
    compute_pipeline_status,
)
from novelvideo.sqlite_store import SQLiteStore

router = APIRouter()

# Backwards-compatible re-exports: existing tests and callers import these from
# this route module. The single source of truth lives in novelvideo.pipeline_status.
__all__ = [
    "_STEP_MAP",
    "_beat_audio_series_complete",
    "_beat_has_script_content",
    "compute_pipeline_status",
    "pipeline_status",
    "router",
]


@router.get("/projects/{project}/pipeline/status")
async def pipeline_status(
    project: str,
    episode: Optional[int] = Query(None, description="指定集数，不传则自动检测最新活跃集"),
    user: dict = Depends(get_api_user),
    store: SQLiteStore = Depends(get_sqlite_store),
):
    resolved = await resolve_project_scope(project, user, required_role="viewer")
    data = await compute_pipeline_status(
        store=store,
        username=resolved.username,
        project_name=resolved.project_name,
        project_dir=resolved.project_dir,
        ctx=resolved.ctx,
        episode=episode,
    )
    return {"ok": True, "data": data}
