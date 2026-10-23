"""内容审核失败后的「提示词修复」策略。

上游（Seedance / Doubao）的审核拒绝分几类，补救手段不同：

* 提示词**文本**被拒（``InputTextSensitiveContentDetected``）：文字本身有问题，
  用 ``prompt_safety`` 的确定性去敏就能产出新参数，零 LLM 成本；
* **输出视频**（含版权）被拒：提示词文字已经通过上游文本审核，被拦的是画面内容，
  纯替换表救不回来，必须重新生成一版提示词（把失败原因作为附加约束喂给 composer）；
* **输入参考图**被拒：提示词改写根本救不回来，直接交回用户决定。

三条路径遵守同一契约：**产不出与失败时不同的提示词就不算修复成功**。调用方
（``chat/director_auto.py``）据此拒绝「原样参数再重投」，这也是本模块存在的理由。

自动改写会留痕：``prompt_source`` 标记为 ``moderation_repair``，并把改写前后的
提示词追加到项目状态目录的 ``moderation_repairs.jsonl``，便于复核与回滚。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novelvideo.prompt_safety import (
    is_desensitize_exhausted,
    is_output_video_moderation_rejection,
    is_text_moderation_rejection,
    neutralize_sensitive_elements,
    sanitize_prompt_for_moderation,
)

logger = logging.getLogger(__name__)

#: 分档：提示词文本被拒 → 确定性去敏。
MODERATION_KIND_INPUT_TEXT = "input_text"
#: 分档：输出视频/版权被拒 → 重新生成提示词。
MODERATION_KIND_OUTPUT_VIDEO = "output_video"
#: 分档：输入参考图被拒 → 提示词改写救不回。
MODERATION_KIND_INPUT_IMAGE = "input_image"
#: 分档：只知道是审核类（表单没有更多信息）→ 保守走确定性去敏。
MODERATION_KIND_UNKNOWN = "unknown"

KIND_LABELS = {
    MODERATION_KIND_INPUT_TEXT: "提示词文本被审核拦截",
    MODERATION_KIND_OUTPUT_VIDEO: "生成画面被审核拦截",
    MODERATION_KIND_INPUT_IMAGE: "参考图被审核拦截",
    MODERATION_KIND_UNKNOWN: "内容审核未通过",
}

#: 自动改写后的 ``prompt_source`` 标记值。
MODERATION_REPAIR_PROMPT_SOURCE = "moderation_repair"

#: 追加给 composer 的附加硬性要求（只在审核补救时注入，不进用户可见字段）。
MODERATION_REPAIR_NOTE = (
    "本镜上一版被上游内容安全审核拒绝（不是画面一致性或时长问题）。请在**不改变"
    "剧情、人物身份、服装、道具、景别与光线**的前提下改写，降低命中审核的概率："
    "画面里不要出现可辨识的证件、单据、文件、屏幕文字或编号（如身份证号、工牌编号），"
    "这些元素一律写成模糊不可辨识；不要描写暴露、亲密或血腥画面；"
    "把可能敏感的表述改成中性说法。其余要求与上一版保持一致。"
)

#: 台账文件名（写在项目 state_dir 下，只追加不覆盖）。
LEDGER_FILENAME = "moderation_repairs.jsonl"


@dataclass(frozen=True)
class PromptRepairOutcome:
    """一次提示词修复的结果。

    ``repaired`` 为 True 表示**产出了与失败时不同的提示词并已持久化**，此时调用方
    才允许重投；为 False 时 ``detail`` 说明原因，调用方必须停下来交回用户。
    """

    repaired: bool
    kind: str
    detail: str = ""
    previous_prompt: str = ""
    new_prompt: str = ""

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind or "内容审核未通过")


def classify_moderation_text(text: str) -> str:
    """把一段审核错误文本归到上面四个分档之一（纯函数，便于单测）。"""

    raw = str(text or "")
    if is_text_moderation_rejection(raw):
        return MODERATION_KIND_INPUT_TEXT
    lowered = raw.lower()
    if "inputimagesensitivecontentdetected" in lowered or "input image" in lowered:
        return MODERATION_KIND_INPUT_IMAGE
    if is_output_video_moderation_rejection(raw):
        return MODERATION_KIND_OUTPUT_VIDEO
    return MODERATION_KIND_UNKNOWN


def moderation_repair_recovery_key(episode: int, beat_num: int) -> str:
    """把「这个 beat 已经改过一次词」记进 run 的 recovery key。"""

    return f"moderation-prompt-repair:{int(episode)}:{int(beat_num)}"


async def repair_prompt_for_moderation(
    *,
    kind: str,
    store: Any,
    state_dir: str | Path,
    project_dir: str | Path,
    episode: int,
    beat_num: int,
    raw_error: str = "",
) -> PromptRepairOutcome:
    """改写 ``beat_num`` 的 Seedance2 提示词，让下一次重投不是原样参数。

    只负责改词与留痕，不负责重投、不负责计费（自动补救不算用户发起的 LLM 调用）。
    ``raw_error`` 是上游/提交层的原始错误文本，用来判断确定性去敏这一档还有没有意义。
    """

    from novelvideo.seedance2_i2v.models import parse_seedance2_config

    episode = int(episode)
    beat_num = int(beat_num)
    if kind == MODERATION_KIND_INPUT_IMAGE:
        # 参考图被拒：提示词里没有可改的东西，重投必然带着同一张图再被拒。
        return PromptRepairOutcome(
            repaired=False,
            kind=kind,
            detail="被拦的是参考图，改写提示词无法解决，需要更换参考素材",
        )

    try:
        script_data = await store.get_script_as_dict(episode)
    except Exception as exc:  # noqa: BLE001 - 读不到就当作修复失败
        logger.warning("读取剧本失败，无法修复提示词：%s", exc)
        return PromptRepairOutcome(repaired=False, kind=kind, detail="读取剧本失败")

    beats = list((script_data or {}).get("beats") or [])
    target = next(
        (beat for beat in beats if int(beat.get("beat_number") or 0) == beat_num), None
    )
    if target is None:
        return PromptRepairOutcome(
            repaired=False, kind=kind, detail=f"第 {episode} 集找不到第 {beat_num} 镜"
        )

    config = parse_seedance2_config(target.get("seedance2_config_json"))
    previous = str(config.final_prompt or "").strip()
    if not previous:
        return PromptRepairOutcome(
            repaired=False, kind=kind, detail="该镜还没有提示词，无法改写"
        )

    if kind in {MODERATION_KIND_INPUT_TEXT, MODERATION_KIND_UNKNOWN}:
        # 提交层可能已经用同一套确定性规则抢救过一次（错误文本里会写明），
        # 那就别再重放一次必然被拒的改写，直接走重新生成。
        candidate = (
            "" if is_desensitize_exhausted(raw_error) else _deterministic_candidate(previous)
        )
        strategy = "确定性去敏"
        if not candidate:
            # 规则表没有命中任何可中性化的表述：这时确定性改写是空操作，
            # 只能升级为「重新生成」，否则等于拿原样参数重投。
            candidate, strategy = await _regenerate_candidate(
                store=store,
                script_data=script_data,
                beats=beats,
                target=target,
                beat_num=beat_num,
                episode=episode,
                state_dir=state_dir,
                project_dir=project_dir,
            )
    else:
        candidate, strategy = await _regenerate_candidate(
            store=store,
            script_data=script_data,
            beats=beats,
            target=target,
            beat_num=beat_num,
            episode=episode,
            state_dir=state_dir,
            project_dir=project_dir,
        )

    if not candidate:
        return PromptRepairOutcome(
            repaired=False,
            kind=kind,
            detail=strategy or "重写提示词未产出可用结果",
            previous_prompt=previous,
        )
    if candidate.strip() == previous:
        return PromptRepairOutcome(
            repaired=False,
            kind=kind,
            detail=f"{strategy}后的提示词与原来一致，不能原样重投",
            previous_prompt=previous,
        )

    try:
        # 重新生成那条路径已经把 composer 的结果写回了 beat 行，所以这里以
        # 「此刻的目标 beat」为准，避免把 panel 刚写的 prompt_inputs_hash 等字段回退。
        from novelvideo.seedance2_i2v.models import (
            dump_seedance2_config,
            parse_seedance2_config,
        )

        config = parse_seedance2_config(target.get("seedance2_config_json"))
        config.final_prompt = candidate
        config.prompt_source = MODERATION_REPAIR_PROMPT_SOURCE
        config.prompt_updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        saved_json = dump_seedance2_config(config)
        await store.update_beat_asset(
            episode_number=episode,
            beat_number=beat_num,
            seedance2_config_json=saved_json,
        )
    except Exception as exc:  # noqa: BLE001 - 写回失败等于没修复
        logger.warning("写回审核补救后的提示词失败：%s", exc)
        return PromptRepairOutcome(
            repaired=False, kind=kind, detail="写回提示词失败", previous_prompt=previous
        )

    _append_ledger(
        state_dir=state_dir,
        record={
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "episode": episode,
            "beat_num": beat_num,
            "kind": kind,
            "strategy": strategy,
            "previous_prompt": previous,
            "new_prompt": candidate,
        },
    )
    logger.info(
        "已按内容审核补救改写第 %s 集第 %s 镜提示词（%s，%s）",
        episode,
        beat_num,
        kind,
        strategy,
    )
    return PromptRepairOutcome(
        repaired=True,
        kind=kind,
        detail=(
            "已对提示词做确定性去敏"
            if strategy == "确定性去敏"
            else "已重新生成提示词"
        ),
        previous_prompt=previous,
        new_prompt=candidate,
    )


def _deterministic_candidate(prompt: str) -> str:
    """确定性去敏：先跑通用中性化，再跑画面元素中性化。"""

    candidate = neutralize_sensitive_elements(
        sanitize_prompt_for_moderation(prompt)
    ).strip()
    return "" if candidate == str(prompt or "").strip() else candidate


async def _regenerate_candidate(
    *,
    store: Any,
    script_data: dict[str, Any] | None,
    beats: list[dict[str, Any]],
    target: dict[str, Any],
    beat_num: int,
    episode: int,
    state_dir: str | Path,
    project_dir: str | Path,
) -> tuple[str, str]:
    """重新生成提示词；返回 (新提示词, 策略说明)，失败时返回 ("", 说明)。"""

    from novelvideo.seedance2_i2v.panel_service import (
        generate_seedance2_prompt_for_panel,
    )

    next_beat = next(
        (beat for beat in beats if int(beat.get("beat_number") or 0) == beat_num + 1),
        None,
    )
    try:
        saved_json = await generate_seedance2_prompt_for_panel(
            store=store,
            state_dir=Path(state_dir),
            episode=episode,
            beat=target,
            project_dir=Path(project_dir),
            next_beat=next_beat,
            moderation_repair_note=MODERATION_REPAIR_NOTE,
            prop_menu=list((script_data or {}).get("prop_menu") or []),
        )
    except Exception as exc:  # noqa: BLE001 - 生成失败即修复失败
        logger.warning("重新生成提示词失败：%s", exc)
        return "", "重新生成提示词失败"

    from novelvideo.seedance2_i2v.models import parse_seedance2_config

    config = parse_seedance2_config(saved_json)
    if str(config.prompt_source or "") == "fallback":
        # composer 失败退回规则草稿：那只是把 beat 原文摊开，没有规避审核的改动。
        return "", "重新生成提示词退回了规则草稿"
    return str(config.final_prompt or "").strip(), "重新生成提示词"


def _append_ledger(*, state_dir: str | Path, record: dict[str, Any]) -> None:
    """把改写前后的提示词追加到台账；台账只是取证，写不进去也不影响流程。"""

    try:
        path = Path(state_dir) / LEDGER_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - 台账失败不能打断审核补救
        logger.warning("写入审核补救台账失败：%s", exc)


__all__ = [
    "KIND_LABELS",
    "LEDGER_FILENAME",
    "MODERATION_KIND_INPUT_IMAGE",
    "MODERATION_KIND_INPUT_TEXT",
    "MODERATION_KIND_OUTPUT_VIDEO",
    "MODERATION_KIND_UNKNOWN",
    "MODERATION_REPAIR_NOTE",
    "MODERATION_REPAIR_PROMPT_SOURCE",
    "PromptRepairOutcome",
    "classify_moderation_text",
    "moderation_repair_recovery_key",
    "repair_prompt_for_moderation",
]