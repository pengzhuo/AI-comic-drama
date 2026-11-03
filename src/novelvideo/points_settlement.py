"""一键成片：成片完成后按**实际用量**多退少补（伴点结算）。

预扣（:mod:`novelvideo.points_client`）在客户确认「制作成本预估」闸门时按
**计划售价**扣一次；成片落盘后这里按**实际用量**重算售价，多退少补：

- 实际成本 = 该集四张用量表里**能在中转站账单对上的** ``quota`` 之和
  （``quota / QUOTA_PER_USD * USD_CNY_RATE``，与
  :func:`novelvideo.production_prices.calibrate_from_project` 同口径）；
  只认 relay 真值，不把网关侧虚高的默认倍率算给客户。
- 售价 = 实际成本 × (1 + ``contingency_pct``/100) × 客户倍率，再按
  ``100 伴点 = ¥1`` 折算，与报价卡完全一致。
- 差额 = 结算伴点 − 预扣伴点，四舍五入到整数；``|差额| < 1`` 直接忽略。
- 差额为正 → 补扣；差额为负 → 退还（``optype=2``）。

状态机（台账 ``project_config.points_settlements``，**键是集号**）::

    settled          已结清（含退款成功、差额在容差内）→ 放行
    settled_manual   运营手动核销/放行 → 放行
    refund_pending   退款失败 → 放行（不重复退，等人核销）
    pending          实际用量算不出来（relay 账号缺失/超时/对不上）→ 放行
    owed             待补缴（余额不足 / 渠道不可用）→ **拦成片**
    （无台账但已有预扣 = 正在核算）→ **短暂拦**，并触发一次后台结算

设计约定
--------
- **成片完成后后台触发**（``director_auto`` fire-and-forget），不拖慢成片、不挂
  客户请求；带预算超时，超时按「算不出来」记 ``pending`` 放行。
- **前置条件**：只有走过预扣（``points_charges`` 有这一集）的项目才结算，
  否则与这个功能无关。
- **交付后成本再涨不再追扣**：终态一次写死，重复触发直接返回台账。
- 凭据走 :mod:`novelvideo.relay_billing`（环境变量优先，其次
  ``/data/relay-billing.json``）；取不到账号照样可以命中账单缓存。
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("novelvideo.points_settlement")

STATUS_SETTLED = "settled"
STATUS_SETTLED_MANUAL = "settled_manual"
STATUS_OWED = "owed"
STATUS_INFLIGHT = "inflight"
STATUS_PENDING = "pending"
STATUS_REFUND_PENDING = "refund_pending"

REASON_INSUFFICIENT = "insufficient"
REASON_UNAVAILABLE = "unavailable"

INFLIGHT_MESSAGE = "正在核算本集实际用量，请稍候"
PENDING_MESSAGE = "本集实际用量暂不可用，已按预扣放行"
UNAVAILABLE_MESSAGE = "结算服务暂时不可用，请稍后重试补缴"

#: 成片的规范相对路径：``videos/episodes/epNNN_final.mp4``。
_FINAL_VIDEO_RE = re.compile(
    r"(?:^|/)videos/episodes/ep(\d{1,4})_final\.mp4$", re.IGNORECASE
)


def owed_message(points: int) -> str:
    """``owed`` 给客户看的一句话（前端也会渲染自己的卡片）。"""

    return f"本集实际用量超过预扣，需补缴 {int(points)} 伴点后解锁成片"


def final_video_episode(file_path: str | Path | None) -> int | None:
    """从项目内相对路径识别成片属于哪一集；不是成片返回 ``None``。"""

    normalized = str(file_path or "").replace("\\", "/").strip().lstrip("/")
    match = _FINAL_VIDEO_RE.search(normalized)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):  # pragma: no cover - 正则已限位
        return None


# ---------------------------------------------------------------------------
# 身份 → 客户手机号
# ---------------------------------------------------------------------------


def viewer_client_phone(user: dict[str, Any] | None, as_client: str = "") -> str:
    """交付闸门用：客户的手机号；内部视角返回空串（**不拦内部端**）。

    ``as_client`` 是内部端预览客户视角的开关（手机号或 ``1``），与报价卡
    ``_resolve_view`` 一致——运营以客户视角预览时同样按客户处理。
    """

    from novelvideo import production_prices as prices
    from novelvideo import studio_access

    data = user or {}
    kind = str(data.get("credential_kind") or "")
    phone = prices.normalize_phone(data.get("phone")) or ""
    if kind == studio_access.CREDENTIAL_KIND_CLIENT:
        return phone
    if phone and kind != studio_access.CREDENTIAL_KIND_INTERNAL:
        return phone
    preview = str(as_client or "").strip()
    if preview:
        target = prices.normalize_phone(preview)
        return "" if target in {"", "1"} else target
    return ""


#: 内部/受信任的运营身份（可手动核销、放行成片）。
OPERATOR_KINDS = frozenset({"internal_console", "local_trusted_agent"})


def is_operator(user: dict[str, Any] | None) -> bool:
    """运营/内部端身份（客户身份不算，即便它是项目 owner）。"""

    data = user or {}
    if str(data.get("credential_kind") or "") in OPERATOR_KINDS:
        return True
    try:
        from novelvideo import studio_access

        return str(data.get("user_id") or "") == studio_access.INTERNAL_USER_ID
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 实际成本（relay 口径，只读）
# ---------------------------------------------------------------------------


def _relay_maps(project_db: Path, *, workers: int = 4) -> tuple[dict[str, int], dict[str, int], list[str]]:
    """问中转站要这一集用到的单据账单（缓存命中就直接用，miss 才需要账号）。"""

    from novelvideo import relay_billing
    from novelvideo.config import NEWAPI_BILLING_DB_PATH
    from novelvideo.gateway_cost_reconcile import (
        build_relay_index,
        collect_usage_gateway_ids,
    )

    settings = relay_billing.load_settings()
    endpoints = relay_billing.configured_endpoints(settings=settings)
    cache_path = relay_billing.billing_cache_path(settings)
    needed = collect_usage_gateway_ids(project_db)
    task_quota, log_quota, errors, _statuses = build_relay_index(
        NEWAPI_BILLING_DB_PATH,
        endpoints=endpoints,
        only_ids=needed,
        cache_path=cache_path,
        workers=workers,
    )
    return task_quota, log_quota, errors


def _actual_cny_from_quota(quota: int) -> float:
    from novelvideo.config import QUOTA_PER_USD, USD_CNY_RATE

    per_usd = float(QUOTA_PER_USD or 500000.0)
    rate = float(USD_CNY_RATE or 7.3)
    return (int(quota) / per_usd) * rate


async def _episode_actual(
    state_dir: str | Path, episode: int, *, budget_seconds: float
) -> dict[str, Any] | None:
    """该集 relay 口径的实际成本；算不出来返回 ``None``（→ ``pending`` 放行）。"""

    from novelvideo.gateway_cost_reconcile import episode_quota, reconcile_project_db

    project_db = Path(state_dir) / "data.db"
    try:
        task_quota, log_quota, errors = await asyncio.wait_for(
            asyncio.to_thread(_relay_maps, project_db),
            timeout=budget_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning("结算对账超时（%ss），按算不出来处理: %s", budget_seconds, project_db)
        return None
    except Exception:  # noqa: BLE001 - 对账失败不影响成片，也不锁客户
        logger.warning("结算对账失败，按算不出来处理: %s", project_db, exc_info=True)
        return None

    # 把 relay 真值回写台账库，运营报表与客户账单同口径（best-effort）。
    try:
        await asyncio.to_thread(
            reconcile_project_db,
            project_db,
            task_quota=task_quota,
            log_quota=log_quota,
            apply=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug("结算回写 cost_quota 失败: %s", project_db, exc_info=True)

    stat = episode_quota(
        project_db, episode, task_quota=task_quota, log_quota=log_quota
    )
    if int(stat.get("matched") or 0) <= 0 or int(stat.get("quota") or 0) <= 0:
        logger.info(
            "结算无可用 relay 账单 episode=%s rows=%s matched=%s errors=%s",
            episode,
            stat.get("rows"),
            stat.get("matched"),
            errors or "-",
        )
        return None
    return {
        "cny": _actual_cny_from_quota(stat["quota"]),
        "quota": int(stat["quota"]),
        "matched": int(stat["matched"]),
        "rows": int(stat["rows"]),
        "errors": len(errors or []),
    }


def _sale_cny(actual_cny: float, phone: str) -> float | None:
    """实际成本 → 客户售价（与报价卡同口径：成本 ×(1+应急) × 客户倍率）。"""

    from novelvideo import production_prices as prices

    book = prices.load()
    markup, _source = book.resolve_markup(phone)
    if markup is None:
        return None
    contingency = float(getattr(book, "contingency_pct", 0.0) or 0.0)
    return float(actual_cny) * (1.0 + contingency / 100.0) * float(markup)


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------


def _record(state_dir: str | Path, episode: int, status: str, **fields: Any) -> dict:
    from novelvideo.project_config import record_points_settlement_in_state_dir

    return record_points_settlement_in_state_dir(
        state_dir, episode, status=status, **fields
    )


async def _move_points(
    *,
    config: Any,
    phone: str,
    amount: int,
    projects_uuid: str,
    refund: bool,
) -> tuple[str, str]:
    """补扣/退还一笔；返回 ``(outcome, message)``。

    ``outcome`` 是 ``ok`` / ``insufficient`` / ``unavailable``；``insufficient``
    时 ``message`` 是接口原文（如「伴点不足，请先充值」），直接给客户看。
    """

    from novelvideo import points_client

    client = points_client.PointsClient(config)
    try:
        if refund:
            client.refund(phone=phone, amount=amount, projects_uuid=projects_uuid)
        else:
            client.charge(phone=phone, amount=amount, projects_uuid=projects_uuid)
    except points_client.PointsNotEnough as exc:
        logger.info(
            "伴点结算业务失败(%s) amount=%s: %s",
            "退款" if refund else "补扣",
            amount,
            exc,
        )
        if refund:
            return "unavailable", str(exc) or UNAVAILABLE_MESSAGE
        return "insufficient", str(exc) or owed_message(amount)
    except points_client.PointsUnavailable as exc:
        logger.warning(
            "伴点结算接口不可用(%s) amount=%s: %s",
            "退款" if refund else "补扣",
            amount,
            exc,
        )
        return "unavailable", UNAVAILABLE_MESSAGE
    return "ok", ""


async def _apply_delta(
    *,
    state_dir: str | Path,
    episode: int,
    config: Any,
    phone: str,
    projects_uuid: str,
    prepaid: int,
    settlement: int,
    actual: dict[str, Any] | None,
    charged: int = 0,
    refunded: int = 0,
    trigger: str = "compose",
) -> dict:
    """把「结算 − 预扣」的差额落到账上，并写台账。"""

    delta = int(settlement) - int(prepaid)
    base: dict[str, Any] = {
        "prepaid": int(prepaid),
        "settlement": int(settlement),
        "delta": delta,
        "charged": int(charged),
        "refunded": int(refunded),
        "trigger": str(trigger),
        "projects_uuid": str(projects_uuid or ""),
    }
    if actual is not None:
        base.update(
            {
                "actual_cny": round(float(actual["cny"]), 4),
                "quota": actual.get("quota"),
                "matched": actual.get("matched"),
                "rows": actual.get("rows"),
            }
        )

    if delta == 0:
        return _record(state_dir, episode, STATUS_SETTLED, message="", **base)

    if delta > 0:
        remaining = delta - int(charged)
        if remaining <= 0:
            return _record(state_dir, episode, STATUS_SETTLED, message="", **base)
        outcome = await _move_points(
            config=config,
            phone=phone,
            amount=remaining,
            projects_uuid=projects_uuid,
            refund=False,
        )
        if outcome[0] == "ok":
            base["charged"] = int(charged) + remaining
            return _record(state_dir, episode, STATUS_SETTLED, message="", **base)
        # fail-closed：补扣没成（余额不足 / 渠道不可用）都拦下，等补缴。
        reason = (
            REASON_INSUFFICIENT
            if outcome[0] == "insufficient"
            else REASON_UNAVAILABLE
        )
        message = outcome[1] or (
            UNAVAILABLE_MESSAGE
            if reason == REASON_UNAVAILABLE
            else owed_message(remaining)
        )
        return _record(
            state_dir, episode, STATUS_OWED, reason=reason, message=message, **base
        )

    # delta < 0：退款；退款失败只放行（不重复退，等运营核销）。
    remaining_refund = (-delta) - int(refunded)
    if remaining_refund <= 0:
        return _record(state_dir, episode, STATUS_SETTLED, message="", **base)
    outcome = await _move_points(
        config=config,
        phone=phone,
        amount=remaining_refund,
        projects_uuid=projects_uuid,
        refund=True,
    )
    if outcome[0] == "ok":
        base["refunded"] = int(refunded) + remaining_refund
        return _record(state_dir, episode, STATUS_SETTLED, message="", **base)
    base["refunded"] = int(refunded)
    return _record(
        state_dir,
        episode,
        STATUS_REFUND_PENDING,
        message=outcome[1] or "退款暂未成功，已先放行成片，客服会跟进",
        **base,
    )


async def _compute_and_settle(
    *,
    state_dir: str | Path,
    episode: int,
    config: Any,
    phone: str,
    projects_uuid: str,
    prepaid: int,
    trigger: str,
) -> dict:
    from novelvideo import points_client

    actual = await _episode_actual(
        state_dir, episode, budget_seconds=float(config.settle_budget_seconds)
    )
    if actual is None:
        return _record(
            state_dir,
            episode,
            STATUS_PENDING,
            prepaid=int(prepaid),
            message=PENDING_MESSAGE,
            trigger=str(trigger),
            projects_uuid=str(projects_uuid or ""),
        )
    price = _sale_cny(float(actual["cny"]), phone)
    if price is None:
        # 防御分支：``resolve_markup`` 现在恒返回 1 倍起，正常不会再走到这里。
        logger.info("结算缺少客户倍率 phone=%s episode=%s", phone, episode)
        return _record(
            state_dir,
            episode,
            STATUS_PENDING,
            prepaid=int(prepaid),
            actual_cny=round(float(actual["cny"]), 4),
            quota=actual.get("quota"),
            matched=actual.get("matched"),
            rows=actual.get("rows"),
            message=PENDING_MESSAGE,
            trigger=str(trigger),
            projects_uuid=str(projects_uuid or ""),
        )
    settlement = int(points_client.points_for_cny(price, config))
    tolerance = max(0.0, float(config.settle_tolerance_pct))
    if abs(settlement - int(prepaid)) < 1 or (
        tolerance > 0
        and abs(settlement - int(prepaid)) <= abs(int(prepaid)) * tolerance / 100.0
    ):
        return _record(
            state_dir,
            episode,
            STATUS_SETTLED,
            prepaid=int(prepaid),
            settlement=settlement,
            delta=settlement - int(prepaid),
            actual_cny=round(float(actual["cny"]), 4),
            quota=actual.get("quota"),
            matched=actual.get("matched"),
            rows=actual.get("rows"),
            charged=0,
            refunded=0,
            trigger=str(trigger),
            projects_uuid=str(projects_uuid or ""),
        )
    return await _apply_delta(
        state_dir=state_dir,
        episode=episode,
        config=config,
        phone=phone,
        projects_uuid=projects_uuid,
        prepaid=int(prepaid),
        settlement=settlement,
        actual=actual,
        trigger=trigger,
    )


#: 同一事件循环内按 (项目, 集) 串行化结算，避免双击补缴并列扣两次。
_EPISODE_LOCKS: dict[tuple[int, str, int], asyncio.Lock] = {}
_EPISODE_LOCKS_GUARD = threading.Lock()


def _episode_lock(state_dir: str | Path, episode: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (id(loop), str(state_dir), int(episode))
    with _EPISODE_LOCKS_GUARD:
        lock = _EPISODE_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _EPISODE_LOCKS[key] = lock
        return lock


async def settle_episode(
    *,
    state_dir: str | Path,
    episode: int,
    projects_uuid: str = "",
    phone: str = "",
    trigger: str = "compose",
    force: bool = False,
) -> dict | None:
    """结算一集；返回台账（``None`` = 这一集与伴点结算无关，未做任何事）。

    - 开关关 / 没预扣过 → ``None``
    - 已是终态 → 直接返回台账（幂等，不重复扣/退）
    - ``owed`` / ``pending`` / ``inflight`` → 只有 ``force=True``（客户点补缴、
      运营重试）才重算

    同一 (事件循环, 项目, 集) 用锁串行化：客户双击「补缴」不会并列扣两次。
    """

    async with _episode_lock(state_dir, episode):
        return await _settle_episode_locked(
            state_dir=state_dir,
            episode=episode,
            projects_uuid=projects_uuid,
            phone=phone,
            trigger=trigger,
            force=force,
        )


async def _settle_episode_locked(
    *,
    state_dir: str | Path,
    episode: int,
    projects_uuid: str = "",
    phone: str = "",
    trigger: str = "compose",
    force: bool = False,
) -> dict | None:
    from novelvideo import points_client
    from novelvideo.project_config import (
        POINTS_SETTLEMENT_FINAL,
        points_charge_in_state_dir,
        points_settlement_in_state_dir,
    )

    config = points_client.load()
    if not config.settle_enabled:
        return None
    charge = points_charge_in_state_dir(state_dir, episode)
    if not charge or int(charge.get("amount") or 0) <= 0:
        return None
    client_phone = str(charge.get("phone") or phone or "").strip()
    prepaid = int(charge.get("amount") or 0)

    existing = points_settlement_in_state_dir(state_dir, episode)
    if existing:
        status = str(existing.get("status") or "")
        if status in POINTS_SETTLEMENT_FINAL:
            return existing
        if not force:
            return existing
        if status == STATUS_OWED:
            # 补缴重试：按台账里已定的结算额补差额，**不重算**实际用量。
            return await _apply_delta(
                state_dir=state_dir,
                episode=episode,
                config=config,
                phone=client_phone,
                projects_uuid=str(existing.get("projects_uuid") or projects_uuid or ""),
                prepaid=int(existing.get("prepaid") or prepaid),
                settlement=int(existing.get("settlement") or prepaid),
                actual=None,
                charged=int(existing.get("charged") or 0),
                refunded=int(existing.get("refunded") or 0),
                trigger=str(existing.get("trigger") or trigger),
            )

    if not client_phone:
        return None
    return await _compute_and_settle(
        state_dir=state_dir,
        episode=episode,
        config=config,
        phone=client_phone,
        projects_uuid=projects_uuid,
        prepaid=prepaid,
        trigger=trigger,
    )


# ---------------------------------------------------------------------------
# 交付闸门
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryBlock:
    """成片被拦下的原因，直接给前端/客户用。"""

    status: str
    owed_points: int
    message: str


def delivery_block(
    *,
    state_dir: str | Path,
    episode: int,
) -> DeliveryBlock | None:
    """``None`` = 放行；否则拦（调用方决定的 HTTP 形态）。"""

    from novelvideo import points_client
    from novelvideo.project_config import (
        points_charge_in_state_dir,
        points_settlement_in_state_dir,
    )

    config = points_client.load()
    if not config.settle_enabled:
        return None
    if not points_charge_in_state_dir(state_dir, episode):
        return None
    entry = points_settlement_in_state_dir(state_dir, episode)
    if not entry:
        # 有预扣但还没结算（核算中 / 还没触发）→ 短暂拦，由调用方触发结算。
        return DeliveryBlock(STATUS_INFLIGHT, 0, INFLIGHT_MESSAGE)
    status = str(entry.get("status") or "")
    if status != STATUS_OWED:
        return None
    owed = entry.get("delta")
    try:
        points = int(owed) - int(entry.get("charged") or 0)
    except (TypeError, ValueError):
        points = 0
    points = max(0, points)
    message = str(entry.get("message") or "").strip() or owed_message(points)
    return DeliveryBlock(status, points, message)


#: 正在后台核算的 (项目目录, 集号)，避免同一集重复起任务。
_SETTLING: set[tuple[str, int]] = set()
_SETTLING_LOCK = threading.Lock()


def schedule_settle(
    *,
    state_dir: str | Path,
    episode: int,
    projects_uuid: str = "",
    phone: str = "",
    force: bool = False,
) -> bool:
    """后台跑一次结算（fire-and-forget）；同一集已在跑则跳过。"""

    key = (str(state_dir), int(episode))
    with _SETTLING_LOCK:
        if key in _SETTLING:
            return False
        _SETTLING.add(key)

    def _run() -> None:
        try:
            asyncio.run(
                settle_episode(
                    state_dir=state_dir,
                    episode=episode,
                    projects_uuid=projects_uuid,
                    phone=phone,
                    trigger="compose",
                    force=force,
                )
            )
        except Exception:  # noqa: BLE001 - 后台任务绝不外抛
            logger.warning(
                "后台伴点结算失败 state_dir=%s episode=%s", state_dir, episode, exc_info=True
            )
        finally:
            with _SETTLING_LOCK:
                _SETTLING.discard(key)

    threading.Thread(
        target=_run, name=f"points-settle-ep{episode}", daemon=True
    ).start()
    return True


def gate_delivery(
    *,
    user: dict[str, Any] | None,
    state_dir: str | Path,
    episode: int,
    projects_uuid: str = "",
    as_client: str = "",
) -> DeliveryBlock | None:
    """成片交付（预览 + 下载）统一闸门；内部视角直接放行。

    有预扣但还没结算时顺手触发一次后台核算——客户第一次打开成片页面就把账算完。
    """

    phone = viewer_client_phone(user, as_client)
    if not phone:
        return None
    block = delivery_block(state_dir=state_dir, episode=episode)
    if block is not None and block.status == STATUS_INFLIGHT:
        schedule_settle(
            state_dir=state_dir,
            episode=episode,
            projects_uuid=projects_uuid,
            phone=phone,
        )
    return block


__all__ = [
    "INFLIGHT_MESSAGE",
    "OPERATOR_KINDS",
    "PENDING_MESSAGE",
    "REASON_INSUFFICIENT",
    "REASON_UNAVAILABLE",
    "STATUS_INFLIGHT",
    "STATUS_OWED",
    "STATUS_PENDING",
    "STATUS_REFUND_PENDING",
    "STATUS_SETTLED",
    "STATUS_SETTLED_MANUAL",
    "UNAVAILABLE_MESSAGE",
    "DeliveryBlock",
    "delivery_block",
    "final_video_episode",
    "gate_delivery",
    "is_operator",
    "owed_message",
    "schedule_settle",
    "settle_episode",
    "viewer_client_phone",
]
