"""制作成本预估：**粗估**（按剧本字数）与**精估**（按已有分镜）。

口径是「上游真金白银」（¥），价格来自 :mod:`novelvideo.production_prices`。

两档的差别是**输入信息量**：

- ``coarse``：只有剧本字数/章节数，集数都还没定（``build_episodes`` 在流水线里才
  跑）。所以走一张可编辑的「产能换算」假设表（每镜头字数、每集镜头数…），给的是
  **区间**，并如实标注假设。
- ``refined``：分镜（beats）已经写完，能**精确数出**：素描格数（复用
  ``sketch_scene_grid_split``，按场景分格）、上色格数、镜头数、视频秒数、TTS
  调用数与计费字数。此时图/视频/音频这些**大头还没开始花钱**，是最有价值的闸口。

本模块只做纯计算：不触网、不写文件。读分镜、读账单由路由层负责。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from novelvideo.production_prices import (
    ActionRequest,
    PricedAction,
    PriceBook,
    load as load_prices,
)

logger = logging.getLogger("novelvideo.production_estimate")

KIND_LABELS = {
    "image": "图片",
    "video": "视频",
    "audio": "音频",
    "text": "文本",
    "feature": "其它",
}
KIND_ORDER = ("video", "image", "audio", "text", "feature")

#: 假设表的默认值（与 ``production_prices._default_config`` 保持一致）。
DEFAULT_ASSUMPTIONS: dict[str, float] = {
    "chars_per_beat": 60.0,
    "beats_per_episode": 40.0,
    "video_seconds_per_beat": 5.0,
    "text_tokens_per_char": 1.5,
    "text_output_token_ratio": 0.35,
    "audio_tokens_per_char": 1.0,
    "image_calls_per_beat": 1.0,
    "text_calls_per_episode": 30.0,
    "render_calls_per_beat": 1.0,
    "sketch_passes_per_grid": 1.0,
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _assumption(book: PriceBook, key: str) -> float:
    return _float(book.assumptions.get(key, DEFAULT_ASSUMPTIONS.get(key, 0.0)))


def _text_model() -> str:
    try:
        from novelvideo.config import get_newapi_text_model_name

        return get_newapi_text_model_name("SCRIPT_WRITER_MODEL", "brainclaw") or "brainclaw"
    except Exception:  # noqa: BLE001 - 预估不该因为取不到模型名而挂掉
        return "brainclaw"


def _is_space_map_beat(beat: dict) -> bool:
    try:
        from novelvideo.generators.nanobanana_grid import _is_space_map_beat as predicate

        return bool(predicate(beat))
    except Exception:  # noqa: BLE001
        return False


def _shot_beats(beats: Sequence[dict]) -> list[dict]:
    """真正会产生画面的分镜（排除空间图之类）。"""

    return [
        beat
        for beat in (beats or [])
        if _int(beat.get("beat_number"), 0) > 0 and not _is_space_map_beat(beat)
    ]


def _text_metrics(chars: float, book: PriceBook) -> dict[str, Any]:
    """把字数换成文本类动作的 metrics（token 数只能估）。"""

    tokens_per_char = _assumption(book, "text_tokens_per_char") or 1.5
    ratio = _assumption(book, "text_output_token_ratio")
    if not 0.0 <= ratio <= 1.0:
        ratio = 0.35
    tokens = max(chars, 0.0) * tokens_per_char
    return {
        "tokens": tokens,
        "prompt_tokens": tokens * (1.0 - ratio),
        "completion_tokens": tokens * ratio,
        "billable_chars": max(chars, 0.0),
    }


# ---------------------------------------------------------------------------
# 精估：按分镜
# ---------------------------------------------------------------------------


def _grid_count(beats: Sequence[dict]) -> int:
    """一集要生成多少张「格子图」（按场景分格，与流水线同一函数）。"""

    if not beats:
        return 0
    try:
        from novelvideo.generators.nanobanana_grid import sketch_scene_grid_split

        return max(len(sketch_scene_grid_split(list(beats))), 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sketch_scene_grid_split 不可用，退化为「每镜头一图」: %s", exc)
        return len(_shot_beats(beats))


def _audio_totals(beats: Sequence[dict]) -> tuple[int, int]:
    """TTS 的调用次数与计费字数（与 TTS 计划同口径：旁白 + 对白）。"""

    calls = 0
    chars = 0
    try:
        from novelvideo.audio.indextts2_beat_audio_task import (
            dialogue_text,
            narration_beat_text,
            normalize_seedance2_audio_type,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("TTS 计划助手不可用，退化为「每镜头一次」: %s", exc)
        shots = _shot_beats(beats)
        return len(shots), sum(len(str(b.get("narration_segment") or "")) for b in shots)

    for beat in beats or []:
        try:
            audio_type = normalize_seedance2_audio_type(beat)
        except Exception:  # noqa: BLE001
            audio_type = "narration"
        if audio_type not in {"narration", "dialogue"}:
            continue
        text = narration_beat_text(beat) if audio_type == "narration" else dialogue_text(beat)
        if not text:
            continue
        calls += 1
        chars += len(text)
    return calls, chars


def plan_refined(
    *,
    beats: Sequence[dict],
    episode: int | None = None,
    book: PriceBook | None = None,
) -> list[ActionRequest]:
    """由已有分镜推出「还要花钱」的动作清单（不含已发生的文本类大头）。"""

    book = book or load_prices()
    from novelvideo.config import (
        DEFAULT_VIDEO_MODEL,
        INDEXTTS2_RECORD_MODEL,
        NEWAPI_IMAGE_MODEL,
        NEWAPI_VIDEO_RESOLUTION,
    )

    shots = _shot_beats(beats)
    grids = _grid_count(beats)
    # 素描是「格子图」（多镜头拼一张，按场景分批）；上色是**每镜头一张**
    # （实测：render_grid 每条分镜一次，1x1）。别把两者都按格子数算。
    sketch_calls = int(round(grids * _assumption(book, "sketch_passes_per_grid")))
    render_calls = int(round(len(shots) * _assumption(book, "render_calls_per_beat")))

    actions: list[ActionRequest] = []
    image_params: dict[str, Any] = {}
    size = str(book.assumptions.get("image_size") or "").strip()
    quality = str(book.assumptions.get("image_quality") or "").strip()
    if size:
        image_params["size"] = size
    if quality:
        image_params["quality"] = quality

    if sketch_calls:
        actions.append(
            ActionRequest(
                kind="image",
                model=NEWAPI_IMAGE_MODEL,
                feature_key="mainline.sketch_grid",
                params=dict(image_params),
                quantity=sketch_calls,
                metrics={"call_count": sketch_calls, "item_count": sketch_calls},
                label=f"素描格图 × {sketch_calls}",
                episode=episode,
            )
        )
    if render_calls:
        actions.append(
            ActionRequest(
                kind="image",
                model=NEWAPI_IMAGE_MODEL,
                feature_key="mainline.render_grid",
                params=dict(image_params),
                quantity=render_calls,
                metrics={"call_count": render_calls, "item_count": render_calls},
                label=f"上色格图 × {render_calls}",
                episode=episode,
            )
        )

    if shots:
        total_seconds = 0.0
        try:
            from novelvideo.manual_shots import resolve_target_video_duration

            default_seconds = _assumption(book, "video_seconds_per_beat") or 5.0
            for beat in shots:
                total_seconds += resolve_target_video_duration(
                    beat, None, default=default_seconds
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("视频时长解析不可用，用假设值: %s", exc)
            total_seconds = len(shots) * (_assumption(book, "video_seconds_per_beat") or 5.0)
        total_seconds = round(total_seconds, 3)
        actions.append(
            ActionRequest(
                kind="video",
                model=DEFAULT_VIDEO_MODEL,
                feature_key="mainline.single_video",
                params={"resolution": NEWAPI_VIDEO_RESOLUTION, "video_input": "none"},
                quantity=len(shots),
                metrics={
                    "call_count": len(shots),
                    "item_count": len(shots),
                    "duration_seconds": total_seconds,
                    "output_duration_seconds": total_seconds,
                },
                label=f"视频片段 × {len(shots)}（{total_seconds:.0f} 秒）",
                episode=episode,
            )
        )

    tts_calls, tts_chars = _audio_totals(beats)
    if tts_calls:
        tokens_per_char = _assumption(book, "audio_tokens_per_char") or 1.0
        actions.append(
            ActionRequest(
                kind="audio",
                model=INDEXTTS2_RECORD_MODEL,
                feature_key="mainline.beat_tts",
                quantity=tts_calls,
                metrics={
                    "call_count": tts_calls,
                    "item_count": tts_calls,
                    "billable_chars": tts_chars,
                    "prompt_tokens": tts_chars * tokens_per_char,
                    "completion_tokens": 0.0,
                },
                label=f"配音 × {tts_calls}（{tts_chars} 字）",
                episode=episode,
            )
        )

    # 文本类：分镜之后还有提示词/身份等 LLM 调用，按「镜头数 × 每次」粗算。
    text_chars = sum(
        len(str(beat.get("narration_segment") or "")) + len(str(beat.get("visual_description") or ""))
        for beat in shots
    )
    text_calls = max(_int(round(len(shots) * 0.5)), 1) if shots else 0
    if text_calls:
        metrics = _text_metrics(text_chars, book)
        metrics["call_count"] = text_calls
        metrics["item_count"] = text_calls
        actions.append(
            ActionRequest(
                kind="text",
                model=_text_model(),
                feature_key="mainline.llm",
                quantity=text_calls,
                metrics=metrics,
                label=f"文本/提示词 LLM × {text_calls}",
                episode=episode,
            )
        )
    return actions


# ---------------------------------------------------------------------------
# 粗估：按字数
# ---------------------------------------------------------------------------


def plan_coarse(
    *,
    chars: int,
    episodes: int = 0,
    chapters: int = 0,
    book: PriceBook | None = None,
) -> list[ActionRequest]:
    """由剧本字数推整部（或若干集）的动作清单。

    集数未知时按 ``beats_per_episode`` 与 ``chars_per_beat`` 反推：相机选一个集数，
    让「总镜头数」落在合理区间。这里给的是**区间中点**，UI 上会标 ±。
    """

    book = book or load_prices()
    from novelvideo.config import (
        DEFAULT_VIDEO_MODEL,
        INDEXTTS2_RECORD_MODEL,
        NEWAPI_IMAGE_MODEL,
        NEWAPI_VIDEO_RESOLUTION,
    )

    total_chars = max(int(chars or 0), 0)
    chars_per_beat = _assumption(book, "chars_per_beat") or 60.0
    beats_per_episode = max(_assumption(book, "beats_per_episode"), 1.0)
    episode_count = int(episodes or 0)
    if episode_count <= 0:
        if chapters > 0:
            episode_count = max(int(round(chapters / 3.0)), 1)
        else:
            episode_count = max(int(round(total_chars / (chars_per_beat * beats_per_episode))), 1)
    episode_count = max(episode_count, 1)

    beats = max(int(round(total_chars / chars_per_beat)), episode_count)
    grids = max(int(round(beats * max(_assumption(book, "image_calls_per_beat"), 0.0))), 0)
    seconds = round(beats * (_assumption(book, "video_seconds_per_beat") or 5.0), 3)
    audio_chars = int(round(beats * chars_per_beat))
    text_chars = total_chars

    actions: list[ActionRequest] = []
    image_params: dict[str, Any] = {}
    size = str(book.assumptions.get("image_size") or "").strip()
    quality = str(book.assumptions.get("image_quality") or "").strip()
    if size:
        image_params["size"] = size
    if quality:
        image_params["quality"] = quality

    if grids:
        for feature_key, label in (
            ("mainline.sketch_grid", "素描格图"),
            ("mainline.render_grid", "上色格图"),
        ):
            actions.append(
                ActionRequest(
                    kind="image",
                    model=NEWAPI_IMAGE_MODEL,
                    feature_key=feature_key,
                    params=dict(image_params),
                    quantity=grids,
                    metrics={"call_count": grids, "item_count": grids},
                    label=f"{label} × {grids}（估）",
                )
            )
    if beats:
        actions.append(
            ActionRequest(
                kind="video",
                model=DEFAULT_VIDEO_MODEL,
                feature_key="mainline.single_video",
                params={"resolution": NEWAPI_VIDEO_RESOLUTION, "video_input": "none"},
                quantity=beats,
                metrics={
                    "call_count": beats,
                    "item_count": beats,
                    "duration_seconds": seconds,
                    "output_duration_seconds": seconds,
                },
                label=f"视频片段 × {beats}（{seconds:.0f} 秒，估）",
            )
        )
        actions.append(
            ActionRequest(
                kind="audio",
                model=INDEXTTS2_RECORD_MODEL,
                feature_key="mainline.beat_tts",
                quantity=beats,
                metrics={
                    "call_count": beats,
                    "item_count": beats,
                    "billable_chars": audio_chars,
                    "prompt_tokens": audio_chars
                    * (_assumption(book, "audio_tokens_per_char") or 1.0),
                    "completion_tokens": 0.0,
                },
                label=f"配音 × {beats}（{audio_chars} 字，估）",
            )
        )

    text_calls = max(
        int(round(episode_count * (_assumption(book, "text_calls_per_episode") or 30.0))), 1
    )
    metrics = _text_metrics(text_chars, book)
    metrics["call_count"] = text_calls
    metrics["item_count"] = text_calls
    actions.append(
        ActionRequest(
            kind="text",
            model=_text_model(),
            feature_key="mainline.llm",
            quantity=text_calls,
            metrics=metrics,
            label=f"文本/提示词 LLM × {text_calls}（估）",
        )
    )
    return actions


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def build_estimate(
    *,
    actions: Iterable[ActionRequest],
    book: PriceBook | None = None,
    stage: str = "refined",
    episode: int | None = None,
    view: str = "internal",
    phone: str = "",
    actual_cny: float | None = None,
    episode_count: int | None = None,
) -> dict[str, Any]:
    """把动作清单定价、汇总，并按视角脱敏。

    ``view="client"`` 时**绝不输出成本**（只给售价），避免泄露利润。
    """

    book = book or load_prices()
    priced: list[PricedAction] = [book.price(action) for action in actions]

    priced_rows = [item for item in priced if item.cny is not None]
    unpriced_rows = [item for item in priced if item.cny is None]
    cost_total = sum(float(item.cny or 0.0) for item in priced_rows)
    contingency_pct = book.contingency_pct
    contingency = cost_total * contingency_pct / 100.0
    grand = cost_total + contingency

    markup, markup_source = book.resolve_markup(phone)
    price_total = grand * markup if markup is not None else None

    by_kind: list[dict[str, Any]] = []
    for kind in KIND_ORDER:
        rows = [item for item in priced if item.action.kind == kind]
        if not rows:
            continue
        kind_total = sum(float(item.cny or 0.0) for item in rows if item.cny is not None)
        by_kind.append(
            {
                "kind": kind,
                "kind_label": KIND_LABELS.get(kind, kind),
                "items": len(rows),
                "cny": round(kind_total, 4),
                "unpriced": sum(1 for item in rows if item.cny is None),
            }
        )

    result: dict[str, Any] = {
        "stage": stage,
        "episode": episode,
        "episode_count": episode_count,
        "currency": book.currency,
        "view": view,
        "generated_at": _now_iso(),
        "contingency_pct": contingency_pct,
        "assumptions": dict(book.assumptions),
        "cost": {
            "cny": round(cost_total, 4),
            "contingency_cny": round(contingency, 4),
            "total_cny": round(grand, 4),
        },
        "price": {
            "cny": round(price_total, 4) if price_total is not None else None,
            "markup": markup,
            "markup_source": markup_source,
        },
        "by_kind": by_kind,
        "items": [item.to_dict() for item in priced],
        "unpriced": [item.to_dict() for item in unpriced_rows],
        "unpriced_count": len(unpriced_rows),
        "priced_count": len(priced_rows),
        "actual": {
            "cny": round(float(actual_cny), 4) if actual_cny is not None else None,
            "delta_pct": (
                round((actual_cny - cost_total) / actual_cny * 100.0, 1)
                if actual_cny and cost_total
                else None
            ),
        },
        "calibrated_at": str(book.raw.get("calibrated_at") or ""),
        "prices_synced_at": str((book.synced or {}).get("fetched_at") or ""),
    }
    if view == "client":
        return _mask_for_client(result)
    return result


def _mask_for_client(result: dict[str, Any]) -> dict[str, Any]:
    """客户视角：成本、未定价、假设、规则来源全部抹掉，只留售价与规模。"""

    masked = dict(result)
    masked["cost"] = None
    masked["assumptions"] = {}
    masked["unpriced"] = []
    masked["unpriced_count"] = 0
    masked["calibrated_at"] = ""
    masked["prices_synced_at"] = ""
    masked["by_kind"] = [
        {
            "kind": row["kind"],
            "kind_label": row["kind_label"],
            "items": row["items"],
            "cny": None,
            "unpriced": 0,
        }
        for row in result.get("by_kind") or []
    ]
    masked["items"] = [
        {
            "kind": item["kind"],
            "label": item["label"],
            "unit": item["unit"],
            "quantity": item["quantity"],
            "episode": item.get("episode"),
            "unit_price": None,
            "cny": None,
            "rule_id": None,
            "source": "",
            "note": "",
        }
        for item in result.get("items") or []
    ]
    masked["actual"] = {"cny": None, "delta_pct": None}
    # 倍率也不能给客户：售价 ÷ 倍率 = 我们的成本，等于把成本泄露出去。
    price = result.get("price") or {}
    masked["price"] = {
        "cny": price.get("cny"),
        "markup": None,
        "markup_source": "",
    }
    return masked


def render_quote(payload: dict[str, Any], fmt: str = "md", *, title: str = "") -> tuple[str, str]:
    """把预估载荷渲染成报价单，返回 ``(文件名, 内容)``。

    支持 ``md`` / ``csv`` / ``txt``；客户视角的载荷里没有成本字段，渲染出来自然只有
    售价，无需额外分支。
    """

    stamp = str(payload.get("generated_at") or "")[:19].replace(":", "")
    safe_title = _slug(title or "production-estimate")
    stage = str(payload.get("stage") or "")
    episode = payload.get("episode")
    scope = f"第 {episode} 集" if episode else f"{payload.get('episode_count') or '全部'} 集"
    cost = payload.get("cost") or {}
    price = payload.get("price") or {}
    items = payload.get("items") or []
    unpriced = payload.get("unpriced") or []
    # 客户版只有售价：不出现单价/金额列，也不暴露加价倍率（售价÷倍率=成本）。
    is_client = payload.get("view") == "client"

    def money(value: Any) -> str:
        if value is None:
            return "—"
        return f"¥{float(value):.2f}"

    def total_line() -> str:
        value = float(price["cny"])
        if is_client:
            return f"报价合计：{money(value)}"
        return f"售价（成本 × {price.get('markup')}）：{money(value)}"

    if fmt == "csv":
        lines = ["种类,项目,数量,单位"] if is_client else ["种类,项目,数量,单位,单价,金额"]
        for item in items:
            cells = [
                _csv_cell(item.get("kind_label") or KIND_LABELS.get(item.get("kind"), "")),
                _csv_cell(item.get("label")),
                f"{float(item.get('quantity') or 0):g}",
                _csv_cell(item.get("unit")),
            ]
            if not is_client:
                cells.append(
                    "" if item.get("unit_price") is None else f"{float(item['unit_price']):.4f}"
                )
                cells.append("" if item.get("cny") is None else f"{float(item['cny']):.2f}")
            lines.append(",".join(cells))
        lines.append("")
        if not is_client:
            lines.append(f"成本小计,,,,,{float(cost.get('cny') or 0):.2f}")
            lines.append(
                f"预留 {payload.get('contingency_pct')}%,,,,"
                f",{float(cost.get('contingency_cny') or 0):.2f}"
            )
            lines.append(f"成本合计,,,,,{float(cost.get('total_cny') or 0):.2f}")
        if price.get("cny") is not None:
            if is_client:
                lines.append(f"报价合计,{float(price['cny']):.2f}")
            else:
                lines.append(f"售价（×{price.get('markup')}）,,,,,{float(price['cny']):.2f}")
        return f"{safe_title}-quote-{stamp}.csv", "\n".join(lines) + "\n"

    if fmt == "txt":
        heading = "制作报价单" if is_client else "制作成本预估"
        rows = [
            f"{heading}（{scope}，{stage}）",
            "=" * 36,
        ]
        for item in items:
            label = item.get("label") or item.get("kind")
            rows.append(f"  {label}" if is_client else f"  {label}  {money(item.get('cny'))}")
        rows.append("")
        if not is_client:
            rows.append(f"  成本小计：{money(cost.get('cny'))}")
            rows.append(f"  预留 {payload.get('contingency_pct')}%：{money(cost.get('contingency_cny'))}")
            rows.append(f"  成本合计：{money(cost.get('total_cny'))}")
        if price.get("cny") is not None:
            rows.append(f"  {total_line()}")
        elif is_client:
            rows.append("  报价合计：待定（尚未配置加价倍率）")
        if unpriced and not is_client:
            rows.append("")
            rows.append(f"  未定价项：{len(unpriced)}（暂不计入合计）")
        return f"{safe_title}-quote-{stamp}.txt", "\n".join(rows) + "\n"

    heading = "制作报价单" if is_client else "制作成本预估"
    if is_client:
        lines = [
            f"# {heading} — {title or safe_title}",
            "",
            f"- 范围：{scope}",
            f"- 档位：{stage}",
            "",
            "## 创作规模",
            "",
            "| 种类 | 项目 | 数量 | 单位 |",
            "| --- | --- | ---: | --- |",
        ]
    else:
        lines = [
            f"# {heading} — {title or safe_title}",
            "",
            f"- 范围：{scope}",
            f"- 档位：{stage}",
            "",
            "## 分项明细",
            "",
            "| 种类 | 项目 | 数量 | 单位 | 单价 | 金额 |",
            "| --- | --- | ---: | --- | ---: | ---: |",
        ]
    for item in items:
        kind_label = item.get("kind_label") or KIND_LABELS.get(item.get("kind"), item.get("kind"))
        label = str(item.get("label") or "").replace("|", "/")
        quantity = float(item.get("quantity") or 0)
        unit = item.get("unit") or ""
        if is_client:
            lines.append(f"| {kind_label} | {label} | {quantity:g} | {unit} |")
        else:
            lines.append(
                "| {kind} | {label} | {qty:g} | {unit} | {unit_price} | {cny} |".format(
                    kind=kind_label,
                    label=label,
                    qty=quantity,
                    unit=unit,
                    unit_price=(
                        "—"
                        if item.get("unit_price") is None
                        else f"{float(item['unit_price']):.4f}"
                    ),
                    cny=money(item.get("cny")),
                )
            )
    lines.append("")
    lines.append("## 合计")
    lines.append("")
    if not is_client:
        lines.append(f"- 成本小计：{money(cost.get('cny'))}")
        lines.append(f"- 预留（{payload.get('contingency_pct')}%）：{money(cost.get('contingency_cny'))}")
        lines.append(f"- **成本合计：{money(cost.get('total_cny'))}**")
    if price.get("cny") is not None:
        lines.append(f"- **{total_line()}**")
    elif is_client:
        lines.append("- 报价合计：待定（该客户尚未配置加价倍率）")
    if unpriced and not is_client:
        lines += ["", f"> 有 {len(unpriced)} 项尚未定价，未计入合计："]
        for item in unpriced:
            lines.append(f"> - {item.get('label') or item.get('kind')}（{item.get('model') or ''}）")
    if not is_client and payload.get("assumptions"):
        lines += ["", "## 估算假设", ""]
        for key, value in sorted(payload["assumptions"].items()):
            lines.append(f"- {key} = {value}")
    return f"{safe_title}-quote-{stamp}.md", "\n".join(lines) + "\n"


def _csv_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    if any(ch in text for ch in ',"\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


def _slug(value: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in str(value or ""))
    return cleaned.strip("-") or "estimate"


def display_safety_check(payload: dict[str, Any]) -> bool:
    """自检：``view=client`` 的载荷里不能出现任何金额（也不能出现加价倍率）。纯函数，便于测试。"""

    if payload.get("view") != "client":
        return True
    if payload.get("cost") is not None:
        return False
    if payload.get("price") is None:
        return True
    # 售价 ÷ 倍率 = 成本，所以倍率同样不能出现在客户载荷里。
    if (payload.get("price") or {}).get("markup") is not None:
        return False
    for item in payload.get("items") or []:
        if item.get("cny") is not None or item.get("unit_price") is not None:
            return False
    for row in payload.get("by_kind") or []:
        if row.get("cny") is not None:
            return False
    return True
