"""伴点结算端点：客户补缴重试 + 运营手动核销/放行。

「一键成片」预扣伴点见 :mod:`novelvideo.points_client`，成片后按实际用量多退
少补见 :mod:`novelvideo.points_settlement`。这里只暴露两个动作：

- ``POST /projects/{p}/episodes/{n}/points/settle``：客户补缴后**重试**这一集的
  补扣；成功即解锁成片。
- ``POST /projects/{p}/episodes/{n}/points/release``：内部端**手动核销/放行**
  （记操作人与时间），用于客户线下付款、渠道长期不可用等运营兜底。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from novelvideo.api.auth import get_api_user
from novelvideo.api.deps import resolve_project_scope

logger = logging.getLogger("novelvideo.api.points")

router = APIRouter()


def _population(entry: dict[str, Any] | None) -> dict[str, Any]:
    """给前端看的结算状态（不泄露成本，只给伴点与状态）。"""

    if not entry:
        return {"status": "not_applicable", "blocked": False, "owed_points": 0, "message": ""}
    from novelvideo import points_settlement

    status = str(entry.get("status") or "")
    blocked = status in (
        points_settlement.STATUS_OWED,
        points_settlement.STATUS_UNPRICED,
    )
    owed = 0
    if blocked:
        try:
            owed = max(
                0, int(entry.get("delta") or 0) - int(entry.get("charged") or 0)
            )
        except (TypeError, ValueError):
            owed = 0
    return {
        "status": status,
        "blocked": blocked,
        "owed_points": owed,
        "prepaid": entry.get("prepaid"),
        "settlement": entry.get("settlement"),
        "delta": entry.get("delta"),
        "message": str(entry.get("message") or ""),
    }


@router.post("/projects/{project}/episodes/{episode_num}/points/settle")
async def settle_episode_points(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    """客户补缴后重试这一集的结算（余额不足 → 充值后再点一次）。"""

    from novelvideo import points_settlement

    resolved = await resolve_project_scope(project, user, required_role="viewer")
    ctx = getattr(resolved, "ctx", None)
    entry = await points_settlement.settle_episode(
        state_dir=resolved.state_dir,
        episode=episode_num,
        projects_uuid=str(getattr(ctx, "project_id", "") or project or ""),
        trigger="client_retry",
        force=True,
    )
    data = _population(entry)
    logger.info(
        "伴点结算重试 project=%s episode=%s status=%s",
        project,
        episode_num,
        data.get("status"),
    )
    return {"ok": True, "data": data}


@router.post("/projects/{project}/episodes/{episode_num}/points/release")
async def release_episode_points(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    """内部端手动核销/放行本集成片（不扣不补，仅解锁）。"""

    from novelvideo import points_settlement
    from novelvideo.project_config import (
        points_charge_in_state_dir,
        record_points_settlement_in_state_dir,
    )

    if not points_settlement.is_operator(user):
        raise HTTPException(status_code=403, detail="仅内部端可手动核销/放行")
    resolved = await resolve_project_scope(project, user, required_role="viewer")
    previous = points_settlement.delivery_block(
        state_dir=resolved.state_dir, episode=episode_num
    )
    entry = record_points_settlement_in_state_dir(
        resolved.state_dir,
        episode_num,
        status=points_settlement.STATUS_SETTLED_MANUAL,
        message="运营已手动核销/放行",
        by=str(user.get("username") or user.get("user_id") or ""),
        action="manual_release",
        previous_status=(previous.status if previous is not None else ""),
        charged=0,
        refunded=0,
    )
    charge = points_charge_in_state_dir(resolved.state_dir, episode_num) or {}
    entry.setdefault("prepaid", charge.get("amount"))
    logger.info(
        "伴点手动放行 project=%s episode=%s by=%s",
        project,
        episode_num,
        user.get("username"),
    )
    return {"ok": True, "data": _population(entry)}
