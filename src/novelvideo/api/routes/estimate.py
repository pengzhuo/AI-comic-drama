"""制作成本预估 + 单价表端点。

三类消费者、三套鉴权：

- ``GET /projects/{p}/production-estimate`` —— 项目内预估。**按身份脱敏**：
  客户链接（``studio_client``）只看到售价，看不到成本/未定价/假设。
- ``GET /projects/{p}/production-estimate/quote`` —— 报价单（md/csv/txt），
  同样按身份脱敏，客户版标题是「制作报价单」且不含任何成本行。
- ``/settings/production-prices*`` —— 单价表读写 / 同步上游价目 / 用账单标定。
  **仅内部端**（``st_console`` 会话），对齐 ``routes/access.py`` 的设置页策略。

预估只读：不触网（同步/标定是显式运维动作）、不写项目数据。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from novelvideo import points_client
from novelvideo import production_estimate as estimate
from novelvideo import production_prices as prices
from novelvideo import studio_access
from novelvideo.api.auth import get_api_user
from novelvideo.api.deps import make_cognee_store_for_context, resolve_project_scope

logger = logging.getLogger("novelvideo.api.estimate")
router = APIRouter()

_STAGES = {"auto", "coarse", "refined"}


# ---------------------------------------------------------------------------
# 视角与鉴权
# ---------------------------------------------------------------------------


def _view_of(user: dict) -> tuple[str, str]:
    """``(view, phone)``。有手机号且不是内部端 → 客户视角。"""
    kind = str(user.get("credential_kind") or "")
    phone = prices.normalize_phone(user.get("phone"))
    if kind == studio_access.CREDENTIAL_KIND_CLIENT or (
        phone and kind != studio_access.CREDENTIAL_KIND_INTERNAL
    ):
        return "client", phone
    return "internal", ""


def _resolve_view(user: dict, as_client: str) -> tuple[str, str]:
    """内部端可以用 ``?as_client=<手机号|1>`` 预览客户看到的样子。"""
    view, phone = _view_of(user)
    preview = str(as_client or "").strip()
    if view == "internal" and preview:
        target = prices.normalize_phone(preview)
        return "client", target if target != "1" else phone
    return view, phone


def _settings_denied(request: Request, user: dict) -> JSONResponse | None:
    """单价表只有内部端能读写。

    访问验证开启时认 ``st_console`` 会话；未开启的部署（上游 CE 等）没有内部会话
    可言，此时只拒绝明确以客户身份进来的请求。
    """
    if studio_access.verify_internal_cookie(
        request.cookies.get(studio_access.INTERNAL_COOKIE_NAME)
    ):
        return None
    if not studio_access.enabled():
        kind = str(user.get("credential_kind") or "")
        if kind == studio_access.CREDENTIAL_KIND_CLIENT:
            return JSONResponse(
                {"ok": False, "error": "仅内部端可操作单价表"}, status_code=403
            )
        return None
    return JSONResponse({"ok": False, "error": "仅内部端可操作单价表"}, status_code=403)


# ---------------------------------------------------------------------------
# 项目读取助手
# ---------------------------------------------------------------------------


async def _load_beats(store: Any, episode: int) -> list[dict]:
    try:
        return list(await store.get_beats_as_dicts(int(episode)))
    except Exception:  # noqa: BLE001 - 没有分镜就是粗估，不该报错
        logger.debug("读取分镜失败 episode=%s", episode, exc_info=True)
        return []


async def _episode_numbers(store: Any) -> list[int]:
    if not hasattr(store, "list_episodes"):
        return []
    try:
        episodes = await store.list_episodes()
    except Exception:  # noqa: BLE001
        logger.debug("列出集数失败", exc_info=True)
        return []
    numbers: list[int] = []
    for item in episodes or []:
        if isinstance(item, dict):
            raw = item.get("episode_number") or item.get("number")
        else:
            raw = getattr(item, "episode_number", None) or getattr(item, "number", None)
        try:
            numbers.append(int(raw))
        except (TypeError, ValueError):
            continue
    return sorted(set(numbers))


def _project_chars(project_dir: Path) -> int:
    """项目里已上传剧本的计费字数（粗估的字数来源）。"""
    uploads = Path(project_dir) / "uploads"
    if not uploads.is_dir():
        return 0
    try:
        from novelvideo.api.chapter_preview import (
            count_billable_novel_chars,
            load_novel_text,
        )
        from novelvideo.utils.document_parsers import is_supported_novel_path
    except Exception:  # noqa: BLE001
        return 0
    for path in sorted(uploads.iterdir()):
        if not path.is_file() or not is_supported_novel_path(path.name):
            continue
        try:
            return int(count_billable_novel_chars(load_novel_text(path)) or 0)
        except Exception:  # noqa: BLE001
            continue
    return 0


async def _actual_cny(project_dir: Path) -> float | None:
    """项目已对账的真实花费（¥）；没有对账数据返回 ``None``。"""
    try:
        calibration = prices.calibrate_from_project(project_dir)
    except Exception:  # noqa: BLE001
        logger.debug("读取项目实际花费失败", exc_info=True)
        return None
    total = sum(float(rule.get("total_cny") or 0.0) for rule in calibration.get("rules") or [])
    return total if total > 0 else None


# ---------------------------------------------------------------------------
# 预估
# ---------------------------------------------------------------------------


async def _build_estimate(
    *,
    project: str,
    request: Request,
    user: dict,
    episode: int | None,
    stage: str,
    chars: int | None,
    episodes: int | None,
    chapters: int | None,
    as_client: str,
    include_actual: bool,
) -> dict[str, Any]:
    resolution = await resolve_project_scope(project, user, required_role="viewer")
    view, phone = _resolve_view(user, as_client)
    book = prices.load()

    normalized_stage = str(stage or "auto").strip().lower()
    if normalized_stage not in _STAGES:
        normalized_stage = "auto"

    actions: list[Any] = []
    refined_episodes = 0
    if normalized_stage in {"auto", "refined"}:
        store = await make_cognee_store_for_context(resolution.ctx)
        try:
            numbers = [int(episode)] if episode else await _episode_numbers(store)
            for number in numbers:
                beats = await _load_beats(store, number)
                if not beats:
                    continue
                actions.extend(
                    estimate.plan_refined(beats=beats, episode=number, book=book)
                )
                refined_episodes += 1
        finally:
            close = getattr(store, "close", None)
            if close:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    logger.debug("关闭 store 失败", exc_info=True)

    used_stage = "refined" if actions else "coarse"
    episode_count = refined_episodes or None
    if used_stage == "coarse":
        total_chars = chars if chars is not None else _project_chars(resolution.project_dir)
        actions = estimate.plan_coarse(
            chars=total_chars,
            episodes=int(episodes or 0),
            chapters=int(chapters or 0),
            book=book,
        )

    actual_cny = (
        await _actual_cny(resolution.project_dir) if include_actual else None
    )
    payload = estimate.build_estimate(
        actions=actions,
        book=book,
        stage=used_stage,
        episode=int(episode) if episode else None,
        view=view,
        phone=phone,
        actual_cny=actual_cny,
        episode_count=episode_count,
    )
    payload["project"] = project
    if not estimate.display_safety_check(payload):  # pragma: no cover - 防线
        logger.error("客户视角载荷出现金额，已强制脱敏 project=%s", project)
        payload = estimate._mask_for_client(payload)  # noqa: SLF001
    return _with_points(payload)


def _with_points(payload: dict[str, Any]) -> dict[str, Any]:
    """给 ``price`` 补上伴点数（100 伴点 = ¥1）。

    ``points`` 是客户**自己售价**的换算，不是成本，脱敏安全
    （``display_safety_check`` 只看 cost / 单价 / markup）；算不出售价时为 ``None``。
    """

    price = dict(payload.get("price") or {})
    value = price.get("cny")
    price["points"] = (
        points_client.points_for_cny(float(value))
        if isinstance(value, (int, float))
        else None
    )
    payload["price"] = price
    return payload


async def client_price_cny(
    *,
    project: str,
    request: Request,
    user: dict,
    episode: int,
    phone: str,
) -> float | None:
    """按客户身份算某一集的精估**售价**（¥）；算不出时返回 ``None``。

    伴点预扣复用这个口径——扣的就是客户在报价卡上看到的那个数。缺省倍率恒为
    1 倍，所以正常都能算出售价；``None`` 只是防御分支。
    """

    payload = await _build_estimate(
        project=project,
        request=request,
        user=user,
        episode=int(episode),
        stage="refined",
        chars=None,
        episodes=None,
        chapters=None,
        as_client=phone,
        include_actual=False,
    )
    price = payload.get("price") or {}
    value = price.get("cny")
    return float(value) if isinstance(value, (int, float)) else None


@router.get("/projects/{project}/production-estimate")
async def get_production_estimate(
    project: str,
    request: Request,
    episode: int | None = Query(None, ge=1, description="只看某一集；不传=全项目"),
    stage: str = Query("auto", description="auto | coarse | refined"),
    chars: int | None = Query(None, ge=0, description="粗估字数（默认读项目上传稿）"),
    episodes: int | None = Query(None, ge=0, description="粗估集数提示"),
    chapters: int | None = Query(None, ge=0, description="粗估章节数提示"),
    as_client: str = Query("", description="内部端预览客户视角：手机号或 1"),
    actual: bool = Query(False, description="附带已对账的真实花费"),
    user: dict = Depends(get_api_user),
):
    """制作成本预估（按身份脱敏）。"""
    payload = await _build_estimate(
        project=project,
        request=request,
        user=user,
        episode=episode,
        stage=stage,
        chars=chars,
        episodes=episodes,
        chapters=chapters,
        as_client=as_client,
        include_actual=bool(actual),
    )
    return {"ok": True, "data": payload}


@router.get("/projects/{project}/production-estimate/quote")
async def get_production_quote(
    project: str,
    request: Request,
    episode: int | None = Query(None, ge=1),
    stage: str = Query("auto"),
    chars: int | None = Query(None, ge=0),
    episodes: int | None = Query(None, ge=0),
    chapters: int | None = Query(None, ge=0),
    as_client: str = Query(""),
    format: str = Query("md", description="md | csv | txt"),
    title: str = Query("", description="报价单标题（默认用项目 id）"),
    user: dict = Depends(get_api_user),
):
    """报价单文本（Markdown / CSV / 纯文本），按身份脱敏。"""
    fmt = str(format or "md").strip().lower()
    if fmt not in {"md", "csv", "txt"}:
        return JSONResponse({"ok": False, "error": f"不支持的格式: {format}"}, status_code=400)
    payload = await _build_estimate(
        project=project,
        request=request,
        user=user,
        episode=episode,
        stage=stage,
        chars=chars,
        episodes=episodes,
        chapters=chapters,
        as_client=as_client,
        include_actual=False,
    )
    filename, content = estimate.render_quote(payload, fmt, title=title or project)
    return {
        "ok": True,
        "data": {
            "format": fmt,
            "filename": filename,
            "content": content,
            "view": payload.get("view"),
        },
    }


# ---------------------------------------------------------------------------
# 单价表（operator-only）
# ---------------------------------------------------------------------------


def _price_book_payload(book: prices.PriceBook) -> dict[str, Any]:
    synced = book.synced or {}
    return {
        "path": str(book.path) if book.path else "",
        "currency": book.currency,
        "contingency_pct": book.contingency_pct,
        "markup_default": book.raw.get("markup_default"),
        "client_markups": book.client_markups,
        "assumptions": book.assumptions,
        "default_assumptions": estimate.DEFAULT_ASSUMPTIONS,
        "aliases": book.aliases,
        "units": {
            "call": "每次调用",
            "item": "每件产出",
            "second": "每秒视频",
            "char": "每字",
            "1k_char": "每千字",
            "10k_char": "每万字",
            "1k_token": "每千 token",
            "1k_prompt_token": "每千输入 token",
            "1k_completion_token": "每千输出 token",
        },
        "rules": book.rules,
        "actual_rules": book.actual_rules,
        "calibrated_at": str(book.raw.get("calibrated_at") or ""),
        "synced": {
            "fetched_at": str(synced.get("fetched_at") or ""),
            "relays": synced.get("relays") or [],
            "model_count": len(book.synced_models),
            "errors": synced.get("errors") or [],
        },
    }


@router.get("/settings/production-prices")
async def read_production_prices(
    request: Request, user: dict = Depends(get_api_user)
):
    """当前单价表（内部端）。"""
    denied = _settings_denied(request, user)
    if denied is not None:
        return denied
    return {"ok": True, "data": _price_book_payload(prices.load(force=True))}


@router.put("/settings/production-prices")
async def update_production_prices(
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(get_api_user),
):
    """局部更新单价表（只覆盖给定字段，热生效）。"""
    denied = _settings_denied(request, user)
    if denied is not None:
        return denied
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "请求体必须是对象"}, status_code=400)

    denied_field = _validate_price_updates(body)
    if denied_field:
        return JSONResponse({"ok": False, "error": denied_field}, status_code=400)

    book = prices.load(force=True)
    for key in (
        "currency",
        "contingency_pct",
        "markup_default",
        "client_markups",
        "assumptions",
        "aliases",
        "rules",
    ):
        if key in body:
            book.raw[key] = body[key]
    for key, value in body.items():
        if key not in {"currency", "contingency_pct", "markup_default", "client_markups",
                       "assumptions", "aliases", "rules"}:
            book.raw[key] = value
    prices.save(book)
    return {"ok": True, "data": _price_book_payload(prices.load(force=True))}


def _validate_price_updates(body: dict[str, Any]) -> str:
    rules = body.get("rules")
    if rules is not None:
        if not isinstance(rules, list):
            return "rules 必须是数组"
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                return f"rules[{index}] 必须是对象"
            unit = str(rule.get("unit") or "")
            if unit and unit not in prices.UNITS:
                return f"rules[{index}].unit 不支持: {unit}"
            price = rule.get("price")
            if price is not None:
                try:
                    float(price)
                except (TypeError, ValueError):
                    return f"rules[{index}].price 必须是数字"
            match = rule.get("match")
            if match is not None and not isinstance(match, dict):
                return f"rules[{index}].match 必须是对象"
    for key in ("client_markups", "assumptions", "aliases"):
        value = body.get(key)
        if value is not None and not isinstance(value, dict):
            return f"{key} 必须是对象"
    return ""


@router.post("/settings/production-prices/sync")
async def sync_production_prices(
    request: Request, user: dict = Depends(get_api_user)
):
    """从上游中转站拉公开价目，快照进单价表（不覆盖手工规则）。"""
    denied = _settings_denied(request, user)
    if denied is not None:
        return denied
    _book, result = prices.sync_prices()
    return {"ok": True, "data": result, "price_book": _price_book_payload(prices.load(force=True))}


@router.post("/settings/production-prices/calibrate")
async def calibrate_production_prices(
    request: Request,
    project: str = Query(..., description="用来标定的项目 id"),
    apply: bool = Query(False, description="true=写入 actual_rules"),
    user: dict = Depends(get_api_user),
):
    """用项目已对账的真实账单反推 ``¥/单位``（默认只预览）。"""
    denied = _settings_denied(request, user)
    if denied is not None:
        return denied
    resolution = await resolve_project_scope(project, user, required_role="owner")
    calibration = prices.calibrate_from_project(resolution.project_dir)
    result: dict[str, Any] = {
        "preview": True,
        "rules": calibration.get("rules") or [],
        "metadata": calibration.get("metadata") or {},
    }
    if apply:
        book = prices.load(force=True)
        prices.apply_actual_rules(book, calibration)
        result["preview"] = False
        result["price_book"] = _price_book_payload(prices.load(force=True))
    return {"ok": True, "data": result}
