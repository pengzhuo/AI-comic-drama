"""Seedance 2.0 prompt composition helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from novelvideo.brainclaw_contract import BrainClawProfile
from novelvideo.models import (
    beat_scene_ref,
    real_detected_identities,
    real_detected_props,
)
from novelvideo.seedance2_i2v.models import Seedance2I2VMode
from novelvideo.seedance2_i2v.spoken_dialogue import speaker_display_name
from novelvideo.utils.source_language import (
    AssetLanguage,
    asset_language_instruction,
    detect_asset_language,
)


@dataclass(frozen=True)
class Seedance2PromptGeneration:
    """Result of AI-first prompt generation."""

    prompt: str
    used_ai: bool
    draft_prompt: str
    error: str = ""


Seedance2PromptComposer = Callable[..., Awaitable[str]]

logger = logging.getLogger(__name__)

# The composer hits an upstream model that can intermittently hang; a single
# stalled attempt used to burn the whole request timeout and then fall back to
# the rule draft. Retry a couple of times (each attempt bounded by the model
# timeout) so we land on a healthy attempt instead of giving up on the first
# slow/hung response.
SEEDANCE2_COMPOSER_ATTEMPTS = 2


SEEDANCE2_PROMPT_GUIDANCE_TEMPLATES: dict[str, dict[AssetLanguage, str]] = {
    "subject": {
        "zh": "主体：明确画面核心人物或物体、当前动作和状态，避免多个主体争抢焦点。",
        "en": "Subject: Define the primary character or object, its current action and state, and avoid competing focal subjects.",
    },
    "scene": {
        "zh": "场景：补充空间背景、地点关系、关键道具和环境材质，保持与参考图一致。",
        "en": "Scene: Describe the spatial background, location relationships, key props, and environmental materials while staying consistent with the references.",
    },
    "lighting": {
        "zh": "光影：描述主光源、明暗层次、色温和氛围，避免忽明忽暗。",
        "en": "Lighting: Describe the key light, tonal layers, color temperature, and atmosphere while avoiding unintended flicker.",
    },
    "camera": {
        "zh": "镜头：说明景别、视角、运镜速度和运动方向，保持镜头运动清晰可执行。",
        "en": "Camera: Specify shot size, viewpoint, movement speed, and direction so the camera move is clear and executable.",
    },
    "style": {
        "zh": "风格：限定画面质感、时代感、色彩倾向和真实度，避免风格漂移。",
        "en": "Style: Define visual texture, period, color direction, and realism to prevent style drift.",
    },
    "no_subtitle": {
        "zh": "无字幕：避免生成任何文字或字幕，保持画面纯净。",
        "en": "No subtitles: Do not generate any text or subtitles; keep the frame visually clean.",
    },
}


SEEDANCE2_COMPOSER_SYSTEM_PROMPT = """You write image-to-video prompts for Seedance 2.0.
Use the fixed asset manifest, storyboard context, user guidance, and rule-based draft to write the final Seedance 2.0 prompt.
Asset order is fixed. Only use existing reference tokens such as 图片1 and 音频1 from the asset manifest.
Never add or reorder image/audio reference numbers, and do not output @ symbols.
Do not force every asset into the prompt; reference only assets that materially help the current shot.
Duration, resolution, aspect ratio, and human-review controls are API parameters and must not appear in the prompt.
Output the final prompt directly without explaining the generation process."""


def detect_seedance2_prompt_language(beat: dict[str, Any]) -> AssetLanguage:
    """Detect language from authored script fields, excluding generated prompts."""
    source = "\n".join(
        _text(beat.get(field))
        for field in (
            "visual_description",
            "synopsis",
            "dialogue",
            "narration_segment",
            "narration",
            "scene_description",
            "props_description",
        )
        if _text(beat.get(field))
    )
    return detect_asset_language(source)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sentence_text(value: Any) -> str:
    return _text(value).strip(" \t\r\n。；;，,")


def _beat_spoken_text(beat: dict[str, Any]) -> str:
    return _text(
        beat.get("dialogue")
        or beat.get("narration_segment")
        or beat.get("narration")
        or ""
    )


def _beat_spoken_label(
    beat: dict[str, Any], language: AssetLanguage = "zh"
) -> str:
    if _text(beat.get("audio_type")) == "dialogue" or _text(beat.get("dialogue")):
        return "Dialogue" if language == "en" else "台词"
    return "Narration" if language == "en" else "旁白/解说"


def _clean_sentence(value: Any, language: AssetLanguage = "zh") -> str:
    text = _sentence_text(value)
    if not text:
        return ""
    return f"{text}." if language == "en" else f"{text}。"


def _asset_value(asset: Any, name: str, default: Any = "") -> Any:
    if isinstance(asset, dict):
        return asset.get(name, default)
    return getattr(asset, name, default)


def _split_identity_label(identity_id: str) -> tuple[str, str]:
    text = _text(identity_id)
    if "_" not in text:
        return text, ""
    return text.split("_", 1)


def _selected_scene_asset_label(assets: list[Any] | None) -> str:
    for asset in assets or []:
        key = _text(_asset_value(asset, "key"))
        if key.startswith("scene:") and bool(_asset_value(asset, "selected")):
            return key.split(":", 1)[1]
    return ""


def _desired_scene_ref_label(beat: dict[str, Any]) -> str:
    scene_ref = beat_scene_ref(beat)
    if not scene_ref:
        return _text(beat.get("scene_description"))
    label = scene_ref.scene_id
    if scene_ref.variant_id:
        label = f"{label}_{scene_ref.variant_id}"
    return label


def _scene_ref_label(
    beat: dict[str, Any],
    assets: list[Any] | None = None,
    language: AssetLanguage = "zh",
) -> str:
    asset_label = _selected_scene_asset_label(assets)
    desired_label = _desired_scene_ref_label(beat)
    desired_time = _text(beat.get("time_of_day"))
    if asset_label:
        details: list[str] = []
        if desired_label and desired_label != asset_label:
            details.append(
                f"target scene state: {desired_label}"
                if language == "en"
                else f"目标场景状态：{desired_label}"
            )
        if desired_time:
            details.append(
                f"target time: {desired_time}"
                if language == "en"
                else f"目标时间：{desired_time}"
            )
        if details:
            separator = "; " if language == "en" else "；"
            return f"{asset_label} ({separator.join(details)})"
        return asset_label
    if desired_label and desired_time:
        if language == "en":
            return f"{desired_label} (target time: {desired_time})"
        return f"{desired_label}（目标时间：{desired_time}）"
    if desired_time:
        return f"target time: {desired_time}" if language == "en" else f"目标时间：{desired_time}"
    return desired_label


def normalize_seedance2_editor_prompt(prompt: str) -> str:
    """Convert editor-only @ mentions into model-facing reference labels."""

    return (
        _text(prompt)
        .replace("@图片", "图片")
        .replace("@音频", "音频")
        .replace("@视频", "视频")
    )


def _guidance_matches_language(guidance: str, language: AssetLanguage) -> bool:
    """Keep rule-based fallbacks inside the supported output-language contract."""

    if language == "en":
        return detect_asset_language(guidance) == "en"
    if re.search(r"[\u3040-\u30ff\uac00-\ud7af]", guidance):
        return False
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", guidance))


def _localized_prompt_guidance(
    guidance: str,
    template_keys: list[str] | tuple[str, ...] | None,
    language: AssetLanguage,
) -> str:
    normalized = normalize_seedance2_editor_prompt(guidance)
    keys = [key for key in template_keys or [] if key in SEEDANCE2_PROMPT_GUIDANCE_TEMPLATES]
    template_texts = {
        text
        for key in keys
        for text in SEEDANCE2_PROMPT_GUIDANCE_TEMPLATES[key].values()
    }
    freeform = "\n".join(
        line for line in normalized.splitlines() if line.strip() not in template_texts
    ).strip()
    parts = [freeform] if freeform and _guidance_matches_language(freeform, language) else []
    parts.extend(SEEDANCE2_PROMPT_GUIDANCE_TEMPLATES[key][language] for key in keys)
    return "\n".join(dict.fromkeys(parts))


def _selected_assets(assets: list[Any] | None) -> list[Any]:
    return [
        asset
        for asset in assets or []
        if bool(_asset_value(asset, "selected"))
        and _text(_asset_value(asset, "reference_label")) not in {"", "未发送"}
        and _text(_asset_value(asset, "request_field"))
    ]


def _prompt_available_assets(assets: list[Any] | None) -> list[Any]:
    result: list[Any] = []
    for asset in assets or []:
        label = _text(_asset_value(asset, "reference_label"))
        if label in {"", "未发送"}:
            continue
        if not re.match(r"^(图片|音频)\d+$", label):
            continue
        if _text(_asset_value(asset, "request_field")):
            result.append(asset)
            continue
        if (
            _text(_asset_value(asset, "media_type")) == "audio"
            and bool(_asset_value(asset, "exists"))
            and not _text(_asset_value(asset, "validation_error"))
        ):
            result.append(asset)
    return result


def build_seedance2_asset_manifest(assets: list[Any] | None) -> list[dict[str, str]]:
    """Return the fixed asset order visible to the AI composer and prompt editor."""

    manifest: list[dict[str, str]] = []
    for asset in _prompt_available_assets(assets):
        label = _text(_asset_value(asset, "reference_label"))
        manifest.append(
            {
                "label": label,
                "title": _text(_asset_value(asset, "label")),
                "media_type": _text(_asset_value(asset, "media_type")),
                "request_field": _text(_asset_value(asset, "request_field")),
                "note": _text(_asset_value(asset, "note")),
                "identity_id": _text(_asset_value(asset, "identity_id")),
                "prop_id": _text(_asset_value(asset, "prop_id")),
                "prop_scope": _text(_asset_value(asset, "prop_scope")),
                "key": _text(_asset_value(asset, "key")),
            }
        )
    return manifest


def build_seedance2_asset_fallback_manifest(
    assets: list[Any] | None,
) -> list[dict[str, str]]:
    """Return text-only reference constraints for assets that are not sent to the API."""

    manifest: list[dict[str, str]] = []
    for asset in assets or []:
        fallback_text = _text(_asset_value(asset, "fallback_text"))
        if not fallback_text or bool(_asset_value(asset, "selected")):
            continue
        manifest.append(
            {
                "title": _text(_asset_value(asset, "label")),
                "media_type": _text(_asset_value(asset, "media_type")),
                "note": _text(_asset_value(asset, "note")),
                "identity_id": _text(_asset_value(asset, "identity_id")),
                "prop_id": _text(_asset_value(asset, "prop_id")),
                "prop_scope": _text(_asset_value(asset, "prop_scope")),
                "key": _text(_asset_value(asset, "key")),
                "fallback_text": fallback_text,
            }
        )
    return manifest


def _identity_reference_labels(assets: list[Any] | None) -> dict[str, str]:
    labels: dict[str, str] = {}
    for asset in _selected_assets(assets):
        identity_id = _text(_asset_value(asset, "identity_id"))
        reference_label = _text(_asset_value(asset, "reference_label"))
        if identity_id and reference_label.startswith("图片"):
            labels[identity_id] = reference_label
    return labels


def _identity_prompt_fallbacks(assets: list[Any] | None) -> dict[str, str]:
    fallbacks: dict[str, str] = {}
    for asset in assets or []:
        identity_id = _text(_asset_value(asset, "identity_id"))
        fallback_text = _text(_asset_value(asset, "fallback_text"))
        if identity_id and fallback_text:
            fallbacks[identity_id] = fallback_text
    return fallbacks


def _prop_reference_labels(assets: list[Any] | None) -> dict[str, str]:
    labels: dict[str, str] = {}
    for asset in _selected_assets(assets):
        prop_id = _text(_asset_value(asset, "prop_id"))
        reference_label = _text(_asset_value(asset, "reference_label"))
        if prop_id and reference_label.startswith("图片"):
            labels[prop_id] = reference_label
    return labels


def _prop_prompt_fallbacks(assets: list[Any] | None) -> dict[str, str]:
    fallbacks: dict[str, str] = {}
    for asset in assets or []:
        prop_id = _text(_asset_value(asset, "prop_id"))
        fallback_text = _text(_asset_value(asset, "fallback_text"))
        if prop_id and fallback_text:
            fallbacks[prop_id] = fallback_text
    return fallbacks


def _beat_spoken_prompt_fragment(
    beat: dict[str, Any],
    assets: list[Any] | None,
    language: AssetLanguage = "zh",
) -> str:
    spoken = _beat_spoken_text(beat)
    if not spoken:
        return ""

    voice_references: list[str] = []
    for asset in _selected_assets(assets):
        label = _text(_asset_value(asset, "reference_label"))
        if not label.startswith("音频"):
            continue
        identity_id = _text(_asset_value(asset, "identity_id"))
        title = speaker_display_name(identity_id) or _text(_asset_value(asset, "label"))
        if language == "en":
            voice_references.append(
                f"{title} uses the voice from {label}"
                if title
                else f"use the voice from {label}"
            )
        else:
            voice_references.append(
                f"{title}参考{label}声线" if title else f"参考{label}声线"
            )

    speaker = speaker_display_name(_text(beat.get("speaker")))
    context: list[str] = []
    if speaker:
        context.append(
            f"the speaking character in this Beat is {speaker}"
            if language == "en"
            else f"本 Beat 说话角色为{speaker}"
        )
    if voice_references:
        context.append(
            "voice mapping: " + ", ".join(dict.fromkeys(voice_references))
            if language == "en"
            else "声线对应：" + "、".join(dict.fromkeys(voice_references))
        )
    context_text = ("; " if language == "en" else "；").join(context)
    if context_text:
        context_text = f"; {context_text}" if language == "en" else f"；{context_text}"
    if language == "en":
        return (
            "Original dialogue and performance text (distinguish action directions "
            "from spoken dialogue; preserve every spoken word verbatim"
            f"{context_text}): {spoken}"
        )
    return (
        "原始对白与表演文本（请语义区分动作说明和实际说出的台词，"
        f"实际台词必须逐字完整保留{context_text}）：{spoken}"
    )


def _text_with_identity_references(
    text: Any,
    assets: list[Any] | None,
    language: AssetLanguage = "zh",
) -> str:
    identity_labels = _identity_reference_labels(assets)
    identity_fallbacks = _identity_prompt_fallbacks(assets)

    def replace_marker(match: re.Match[str]) -> str:
        identity_id = match.group(1)
        character, _identity = _split_identity_label(identity_id)
        image_label = identity_labels.get(identity_id)
        if image_label:
            return (
                f"{character} from {image_label}"
                if language == "en"
                else f"{image_label}中的{character}"
            )
        fallback_text = identity_fallbacks.get(identity_id)
        if fallback_text:
            return f"{character}（{fallback_text}）"
        return character

    return re.sub(r"\{\{([^}]+)\}\}", replace_marker, _text(text))


def _text_with_asset_references(
    text: Any,
    assets: list[Any] | None,
    language: AssetLanguage = "zh",
) -> str:
    prop_labels = _prop_reference_labels(assets)
    prop_fallbacks = _prop_prompt_fallbacks(assets)

    def replace_prop_marker(match: re.Match[str]) -> str:
        prop_id = _text(match.group(1))
        image_label = prop_labels.get(prop_id)
        if image_label:
            return (
                f"the {prop_id} prop from {image_label}"
                if language == "en"
                else f"{image_label}中的{prop_id}道具"
            )
        fallback_text = prop_fallbacks.get(prop_id)
        if fallback_text:
            return f"{prop_id}（{fallback_text}）"
        return prop_id

    text_with_identities = _text_with_identity_references(text, assets, language)
    return re.sub(r"\[\[([^\]]+)\]\]", replace_prop_marker, text_with_identities)


def _reference_sentence_for_assets(
    *,
    mode: Seedance2I2VMode | str,
    assets: list[Any] | None,
    language: AssetLanguage = "zh",
) -> str:
    mode = Seedance2I2VMode(mode)
    image_parts: list[str] = []
    audio_parts: list[str] = []

    for asset in _selected_assets(assets):
        label = _text(_asset_value(asset, "reference_label"))
        title = _text(_asset_value(asset, "label"))
        key = _text(_asset_value(asset, "key"))
        note = _text(_asset_value(asset, "note"))
        media_type = _text(_asset_value(asset, "media_type"))
        identity_id = _text(_asset_value(asset, "identity_id"))
        prop_id = _text(_asset_value(asset, "prop_id"))

        if media_type == "audio" or label.startswith("音频"):
            audio_parts.append(
                f"{label} as {title or 'an audio reference'}"
                if language == "en"
                else f"{label}作为{title or '声音参考'}"
            )
            continue

        if mode == Seedance2I2VMode.FIRST_FRAME:
            image_parts.append(
                f"{label} as the first frame"
                if language == "en"
                else f"{label}作为首帧画面"
            )
            continue
        if mode == Seedance2I2VMode.FIRST_LAST_FRAME and key == "last_frame":
            image_parts.append(
                f"{label} as the last frame"
                if language == "en"
                else f"{label}作为尾帧画面"
            )
            continue
        if key == "first_frame":
            image_parts.append(
                f"{label} as the starting state and overall composition reference"
                if language == "en"
                else f"{label}作为起始状态和整体构图依据"
            )
            continue
        if identity_id:
            character, _identity = _split_identity_label(identity_id)
            image_parts.append(
                f"keep the appearance of {character} consistent with {label}"
                if language == "en"
                else f"{label}中的{character}形象保持人物特征一致"
            )
            continue
        if prop_id:
            image_parts.append(
                f"keep the shape, material, and details of the {prop_id} prop consistent with {label}"
                if language == "en"
                else f"{label}中的{prop_id}道具保持物体造型、材质和细节一致"
            )
            continue
        if key.startswith("scene:"):
            image_parts.append(
                f"{label} as the reference for {title or 'the scene'}"
                if language == "en"
                else f"{label}作为{title or '场景'}参考"
            )
            continue
        if note:
            image_parts.append(
                f"{label} for {note}" if language == "en" else f"{label}用于{note}"
            )
            continue
        image_parts.append(
            f"{label} as {title or 'a visual reference'}"
            if language == "en"
            else f"{label}作为{title or '参考图'}"
        )

    for asset in assets or []:
        if bool(_asset_value(asset, "selected")):
            continue
        identity_id = _text(_asset_value(asset, "identity_id"))
        prop_id = _text(_asset_value(asset, "prop_id"))
        fallback_text = _text(_asset_value(asset, "fallback_text"))
        if not fallback_text:
            continue
        if identity_id:
            character, _identity = _split_identity_label(identity_id)
            image_parts.append(
                f"generate {character}'s appearance from this description: {fallback_text}"
                if language == "en"
                else f"{character}造型按提示词生成：{fallback_text}"
            )
        elif prop_id:
            image_parts.append(
                f"generate the {prop_id} prop from this description: {fallback_text}"
                if language == "en"
                else f"{prop_id}道具按提示词生成：{fallback_text}"
            )

    if language == "en":
        if mode == Seedance2I2VMode.FIRST_LAST_FRAME and len(image_parts) >= 2:
            base = f"Use {image_parts[0]} and {image_parts[1]}, with a natural video transition"
        elif mode == Seedance2I2VMode.FIRST_FRAME and image_parts:
            base = (
                f"Use {image_parts[0]} to generate an image-to-video clip; "
                "frame 0 must match the first frame exactly (framing, subject scale, "
                "wardrobe, hairstyle, makeup, props, scene and lighting unchanged). Never "
                "push in, crop out body parts or landmarks already visible in the "
                "first frame, reframe a full-body shot into a portrait, change "
                "clothing, colour, makeup or face, and keep every pose physically in "
                "contact with its supporting surface"
            )
        else:
            base = (
                "Use " + ", ".join(image_parts)
                if image_parts
                else "Generate an image-to-video clip from the provided reference assets"
            )
        if audio_parts:
            base = f"{base}, using {', '.join(audio_parts)}"
        return base

    if mode == Seedance2I2VMode.FIRST_LAST_FRAME and len(image_parts) >= 2:
        base = f"以{image_parts[0]}，{image_parts[1]}，视频自然过渡"
    elif mode == Seedance2I2VMode.FIRST_FRAME and image_parts:
        base = (
            f"以{image_parts[0]}生成图生视频，视频第 0 帧必须与首帧完全一致"
            "（景别、取景范围、人物占比、服装、发型、妆容、道具、场景与光线不变），"
            "禁止推近或裁掉首帧已可见的身体部位与地标，禁止把全身镜头重构成特写，"
            "禁止换装、换色、换妆容或换脸，姿态必须与画面内支撑面真实接触"
        )
    else:
        base = (
            "参考" + "、".join(image_parts)
            if image_parts
            else "根据输入参考素材生成图生视频"
        )

    if audio_parts:
        base = f"{base}，参考{'、'.join(audio_parts)}"
    return base


#: 叠加文字的"居中"判据：用户填的位置里出现这些词，就说明整块文字要水平居中。
_OVERLAY_CENTER_HINTS = ("居中", "中心", "中间", "center", "middle")


def _overlay_placement_is_centered(placement: str) -> bool:
    lowered = placement.lower()
    return any(hint in lowered for hint in _OVERLAY_CENTER_HINTS)


def build_text_overlay_prompt_fragment(
    text_overlay: dict[str, Any] | None,
    language: AssetLanguage = "zh",
) -> str:
    overlay = text_overlay or {}
    if not overlay.get("enabled"):
        return ""
    content = _sentence_text(overlay.get("content"))
    if not content:
        return ""
    kind_label = {
        "ad_copy": "广告语",
        "subtitle": "字幕",
        "speech_bubble": "气泡台词",
    }.get(_text(overlay.get("kind")) or "subtitle", "字幕")
    placement = _sentence_text(overlay.get("placement")) or "画面下方居中"
    timing = _sentence_text(overlay.get("timing")) or "全片持续"
    style = _sentence_text(overlay.get("style")) or "干净易读"
    speaker = _text(overlay.get("speaker"))
    if language == "en":
        kind_label = {
            "ad_copy": "advertising copy",
            "subtitle": "subtitle",
            "speech_bubble": "speech-bubble text",
        }.get(_text(overlay.get("kind")) or "subtitle", "subtitle")
        placement = _sentence_text(overlay.get("placement")) or "bottom center"
        timing = _sentence_text(overlay.get("timing")) or "throughout the clip"
        style = _sentence_text(overlay.get("style")) or "clean and readable"
        speaker_text = f", spoken by {speaker_display_name(speaker) or speaker}" if speaker else ""
        centering_rule = (
            " Typesetting: if the text wraps onto multiple lines, center every line "
            "individually inside the text block - no left alignment, no hanging indent, "
            "no space padding - and keep the text to at most two lines."
        )
        if _overlay_placement_is_centered(placement):
            centering_rule = (
                " Typesetting: center the text block horizontally in frame; if the text "
                "wraps onto multiple lines, center every line individually inside the block "
                "- no left alignment, no hanging indent, no space padding - and keep the "
                "text to at most two lines."
            )
        return (
            f'Use {kind_label} text "{content}" at {placement}, visible {timing}, '
            f"with a {style} style{speaker_text}.{centering_rule}"
        )
    speaker_text = ""
    if speaker:
        character, identity = _split_identity_label(speaker)
        speaker_text = (
            f"，说话人为{character}（{identity}）"
            if identity
            else f"，说话人为{character}"
        )
    centering_rule = (
        "文字排版要求：若文字因过长折成多行，每一行都必须相对文字块各自居中，"
        "禁止左对齐、禁止悬挂缩进、禁止用空格手工对齐；文字块最多两行。"
    )
    if _overlay_placement_is_centered(placement):
        centering_rule = (
            "文字排版要求：文字块整体在画面中水平居中；若因过长折成多行，每一行都必须"
            "相对文字块各自居中，禁止左对齐、禁止悬挂缩进、禁止用空格手工对齐；"
            "文字块最多两行。"
        )
    return (
        f"画面文字使用{kind_label}“{content}”，位置为{placement}，"
        f"出现时机为{timing}，文字样式为{style}{speaker_text}。{centering_rule}"
    )


def build_seedance2_prompt_draft(
    *,
    mode: Seedance2I2VMode | str,
    beat: dict[str, Any],
    assets: list[Any] | None,
    text_overlay: dict[str, Any] | None,
    prompt_guidance: str = "",
    prompt_guidance_template_keys: list[str] | tuple[str, ...] | None = None,
    manual_prompt_reference: str = "",
    language: AssetLanguage = "zh",
) -> str:
    """Build a model-facing draft prompt without API-only video params."""

    reference = _reference_sentence_for_assets(
        mode=mode, assets=assets, language=language
    )
    visual = _text_with_asset_references(
        beat.get("visual_description") or beat.get("synopsis") or "",
        assets,
        language,
    )
    scene = _text_with_asset_references(
        _scene_ref_label(beat, assets, language), assets, language
    )
    props = _text_with_asset_references(
        beat.get("props_description") or "", assets, language
    )
    legacy_motion = _text_with_asset_references(
        beat.get("video_prompt") or beat.get("keyframe_prompt") or "",
        assets,
        language,
    )
    spoken = _beat_spoken_prompt_fragment(beat, assets, language)
    spoken_label = _beat_spoken_label(beat, language)
    overlay = build_text_overlay_prompt_fragment(text_overlay, language)
    guidance = _localized_prompt_guidance(
        prompt_guidance, prompt_guidance_template_keys, language
    )
    manual = normalize_seedance2_editor_prompt(manual_prompt_reference)

    lines = [_clean_sentence(reference, language)]
    if language == "en":
        if visual:
            lines.append(_clean_sentence(f"The scene shows {_sentence_text(visual)}", language))
        if scene:
            lines.append(_clean_sentence(f"The environment is {_sentence_text(scene)}", language))
        if props:
            lines.append(_clean_sentence(f"Keep these key props consistent: {_sentence_text(props)}", language))
        if legacy_motion:
            lines.append(_clean_sentence(f"Motion: {_sentence_text(legacy_motion)}", language))
        if spoken:
            lines.append(_clean_sentence(f"{spoken_label}: {_sentence_text(spoken)}", language))
        if overlay:
            lines.append(overlay)
        if guidance:
            lines.append(_clean_sentence(guidance, language))
        if manual:
            lines.append(
                _clean_sentence(
                    f"Use this manual version as a rewrite reference: {_sentence_text(manual)}",
                    language,
                )
            )
        return normalize_seedance2_editor_prompt(" ".join(line for line in lines if line))

    if visual:
        lines.append(_clean_sentence(f"画面呈现{_sentence_text(visual)}", language))
    if scene:
        lines.append(_clean_sentence(f"环境为{_sentence_text(scene)}", language))
    if props:
        lines.append(_clean_sentence(f"关键道具保持{_sentence_text(props)}", language))
    if legacy_motion:
        lines.append(_clean_sentence(f"动态过程：{_sentence_text(legacy_motion)}", language))
    if spoken:
        lines.append(_clean_sentence(f"{spoken_label}内容：{_sentence_text(spoken)}", language))
    if overlay:
        lines.append(overlay)
    if guidance:
        lines.append(_clean_sentence(guidance, language))
    if manual:
        lines.append(
            _clean_sentence(
                f"用户手动版本可作为改写参考：{_sentence_text(manual)}", language
            )
        )

    return normalize_seedance2_editor_prompt("".join(line for line in lines if line))


def _clip_seconds(value: Any) -> int:
    """Integer clip seconds from a request/config value; 0 when unknown."""

    try:
        seconds = int(math.ceil(float(value)))
    except (TypeError, ValueError):
        return 0
    return seconds if seconds > 0 else 0


def compute_seedance2_prompt_inputs_hash(
    *,
    mode: Seedance2I2VMode | str,
    beat: dict[str, Any],
    assets: list[Any] | None,
    text_overlay: dict[str, Any] | None,
    prompt_guidance: str = "",
    prompt_guidance_template_keys: list[str] | tuple[str, ...] | None = None,
    language: AssetLanguage = "zh",
    duration: int | float | str | None = None,
) -> str:
    """Hash only prompt-relevant inputs, excluding request-only video controls.

    Clip duration is part of the prompt: the composer writes the beat clock and
    the lip choreography inside it, so a changed duration must invalidate the
    stored prompt. Other request-only controls still stay out of the hash.
    """

    payload = {
        "mode": Seedance2I2VMode(mode).value,
        "clip_duration_seconds": _clip_seconds(duration),
        "beat": {
            "visual_description": beat.get("visual_description") or "",
            "scene_ref": beat.get("scene_ref") or {},
            "scene_description": beat.get("scene_description") or "",
            "props_description": beat.get("props_description") or "",
            "video_prompt": beat.get("video_prompt") or "",
            "keyframe_prompt": beat.get("keyframe_prompt") or "",
            "synopsis": beat.get("synopsis") or "",
            "dialogue": beat.get("dialogue") or "",
            "narration_segment": beat.get("narration_segment") or "",
            "detected_identities": real_detected_identities(
                beat.get("detected_identities") or []
            ),
            "detected_props": real_detected_props(beat.get("detected_props") or []),
        },
        "assets": build_seedance2_asset_manifest(assets),
        "asset_fallbacks": build_seedance2_asset_fallback_manifest(assets),
        "text_overlay": text_overlay or {},
        "prompt_guidance": _localized_prompt_guidance(
            prompt_guidance, prompt_guidance_template_keys, language
        ),
        "language": language,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_seedance2_prompt_composer_task(
    *,
    mode: Seedance2I2VMode | str,
    beat: dict[str, Any],
    assets: list[Any] | None,
    text_overlay: dict[str, Any] | None,
    prompt_guidance: str,
    draft_prompt: str,
    request_params: dict[str, Any] | None = None,
    manual_prompt_reference: str = "",
    language: AssetLanguage = "zh",
    moderation_repair_note: str = "",
) -> str:
    payload = {
        "mode": Seedance2I2VMode(mode).value,
        "clip_duration_seconds": _clip_seconds(
            (request_params or {}).get("duration")
        ),
        "asset_manifest": build_seedance2_asset_manifest(assets),
        "beat": {
            "visual_description": beat.get("visual_description") or "",
            "scene_ref": beat.get("scene_ref") or {},
            "scene_description": beat.get("scene_description") or "",
            "props_description": beat.get("props_description") or "",
            "video_prompt": beat.get("video_prompt") or "",
            "keyframe_prompt": beat.get("keyframe_prompt") or "",
            "dialogue": beat.get("dialogue") or "",
            "narration_segment": beat.get("narration_segment") or "",
            "detected_identities": real_detected_identities(
                beat.get("detected_identities") or []
            ),
            "detected_props": real_detected_props(beat.get("detected_props") or []),
        },
        "asset_fallbacks": build_seedance2_asset_fallback_manifest(assets),
        "text_overlay": text_overlay or {},
        "user_prompt_guidance": _text(prompt_guidance),
        "manual_prompt_reference": normalize_seedance2_editor_prompt(
            manual_prompt_reference
        ),
        "request_params_for_context_only": request_params or {},
        "rule_based_draft_prompt": normalize_seedance2_editor_prompt(draft_prompt),
    }
    language_rule = asset_language_instruction(language)
    payload_mode = Seedance2I2VMode(mode).value
    if payload_mode == Seedance2I2VMode.FIRST_FRAME.value:
        mode_rule_zh = (
            "- 本次是严格首帧模式：prompt 必须写明视频第 0 帧与首帧完全一致"
            "（景别、取景、人物占比、服装、发型、妆容、道具、场景与光线不变），"
            "并写明禁止推近、禁止裁掉首帧已可见的身体部位与地标、禁止换装/换色/换妆容/换脸，"
            "姿态与画面支撑面必须真实接触；只能引用图片1，不得引用图片2及以后编号。\n"
        )
        mode_rule_en = (
            "- This run is strict first frame: state that frame 0 matches the first "
            "frame exactly (framing, subject scale, wardrobe, hairstyle, makeup, props, "
            "scene, lighting unchanged), forbid push-in, cropping already-visible body "
            "parts or landmarks, costume/colour/makeup/face changes, and require poses in "
            "real contact with their supporting surface; reference 图片1 only.\n"
        )
    else:
        mode_rule_zh = ""
        mode_rule_en = ""
    clip_seconds = _clip_seconds((request_params or {}).get("duration"))
    if clip_seconds:
        clock_rule_zh = (
            f"- 本镜成片时长 {clip_seconds} 秒（clip_duration_seconds）：内部节拍时钟必须写作 "
            f"0:00–0:{clip_seconds:02d}，运镜、动作、口型开合和台词都要落在该时钟内；"
            "禁止照搬其它档位的时间轴（如 15 秒档、25–30 秒档）。\n"
        )
        clock_rule_en = (
            f"- This clip is {clip_seconds} seconds long (clip_duration_seconds): write the "
            f"internal beat clock as 0:00–0:{clip_seconds:02d} and keep every camera move, "
            "action, lip movement, and spoken line inside it. Never reuse another tier's "
            "timeline (such as a 15-second or 25–30-second clip).\n"
        )
    else:
        clock_rule_zh = ""
        clock_rule_en = ""
    overlay_rule_zh = ""
    overlay_rule_en = ""
    overlay = text_overlay or {}
    if overlay.get("enabled") and _sentence_text(overlay.get("content")):
        if _overlay_placement_is_centered(
            _sentence_text(overlay.get("placement")) or "画面下方居中"
        ):
            overlay_rule_zh = (
                "- 叠加文字（text_overlay）：文字块整体在画面中水平居中；若因长度折成多行，"
                "每一行都必须相对文字块各自居中，禁止左对齐、禁止悬挂缩进、禁止用空格对齐。\n"
            )
            overlay_rule_en = (
                "- Overlay text (text_overlay): center the text block horizontally in frame; if it "
                "wraps onto multiple lines, center every line individually inside the block - no "
                "left alignment, no hanging indent, no space padding.\n"
            )
        else:
            overlay_rule_zh = (
                "- 叠加文字（text_overlay）：按 placement 指定的位置摆放文字块；若因长度折成多行，"
                "每一行都必须相对文字块各自居中，禁止左对齐、禁止悬挂缩进、禁止用空格对齐。\n"
            )
            overlay_rule_en = (
                "- Overlay text (text_overlay): place the block as the placement asks; if it wraps "
                "onto multiple lines, center every line individually inside the block - no left "
                "alignment, no hanging indent, no space padding.\n"
            )
    # 内容审核补救：上一版被上游判敏感时的附加硬性要求。只在自动补救路径注入，
    # 不进任何用户可见字段，也不参与草稿生成（规则草稿仍照旧产出）。
    repair_rule_zh = ""
    repair_rule_en = ""
    repair_note = _sentence_text(moderation_repair_note)
    if repair_note:
        repair_rule_zh = f"- 内容审核补救（本条优先于其它美化要求）：{repair_note}\n"
        repair_rule_en = (
            f"- Content-safety remediation (takes priority over styling): {repair_note}\n"
        )
    if language == "en":
        return (
            "Write the final Seedance 2.0 image-to-video prompt from the JSON below.\n\n"
            "Hard requirements:\n"
            "- Follow the official reference syntax using tokens such as 图片1 and 音频1.\n"
            "- Only use numbered tokens present in asset_manifest; never add or reorder them.\n"
            "- Do not force every manifest asset into the prompt.\n"
            "- asset_fallbacks are text-only constraints and must never receive image numbers.\n"
            "- Do not describe the request JSON or output @.\n"
            "- Keep resolution, ratio, generate_audio, return_last_frame, and human_review out of the prompt.\n"
            "- Express length only through the beat clock (for example 0:00–0:05), never as a duration parameter.\n"
            f"{mode_rule_en}"
            f"- {language_rule}\n"
            f"{clock_rule_en}"
            f"{overlay_rule_en}"
            f"{repair_rule_en}"
            "- Only output the final prompt.\n\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}"
        )
    return (
        "请根据下面 JSON 写出最终 Seedance 2.0 图生视频 prompt。\n\n"
        "硬性要求：\n"
        "- 必须遵循官方写法，使用“图片1”“音频1”等编号指代参考素材。\n"
        "- 只能使用 asset_manifest 中已有编号，不能新增图片或音频编号。\n"
        "- 不要强行用完 asset_manifest；只引用当前镜头真正需要的图片或音频。\n"
        "- 音频素材是可选声线/声音参考，不是必须全部使用；没有必要就不要写入 prompt。\n"
        "- asset_fallbacks 是未发送的文字约束资产；可用其中 fallback_text 描述道具或角色，但不能给它编图片编号。\n"
        "- 不能重排编号，不能描述请求 JSON，不要输出 @。\n"
        "- 时长只体现为节拍时钟（例如 0:00–0:05），不要把 duration 写成参数；"
        "resolution、ratio、generate_audio、return_last_frame、human_review "
        "只作为 API 请求参数，不要写进 prompt。\n"
        "- 如果用户写作要求和资产清单、道具 fallback 冲突，以资产清单和 fallback 为准。\n"
        "- dialogue 或 narration_segment 属于对白时，请根据语义区分动作说明和实际说出的台词；"
        "动作可自然改写，但实际台词必须逐字完整保留，不能删减或概括。\n"
        f"{mode_rule_zh}"
        f"{clock_rule_zh}"
        f"{overlay_rule_zh}"
        f"{repair_rule_zh}"
        "- 只输出最终 prompt，不要解释。\n\n"
        f"输出语言：{language_rule}\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}"
    )


def create_seedance2_prompt_composer_agent(language: AssetLanguage = "zh"):
    from pydantic_ai import Agent

    from novelvideo.config import get_newapi_text_pydantic_model
    from novelvideo.skills.ai_film_process import pack_block

    pack = pack_block("seedance2_prompt", model_env="SEEDANCE2_PROMPT_COMPOSER_MODEL")
    return Agent(
        get_newapi_text_pydantic_model(
            "SEEDANCE2_PROMPT_COMPOSER_MODEL",
            brainclaw_profile=BrainClawProfile.SEEDANCE2_PROMPT_COMPOSITION,
            capability="text.generate.workflow",
        ),
        system_prompt="\n\n".join(
            part
            for part in (
                SEEDANCE2_COMPOSER_SYSTEM_PROMPT,
                pack,
                f"## Output Language\n{asset_language_instruction(language)}",
            )
            if part
        ),
        output_type=str,
        name="Seedance 2.0 Prompt Composer",
    )


async def compose_seedance2_prompt_with_agent(
    *,
    mode: Seedance2I2VMode | str,
    beat: dict[str, Any],
    assets: list[Any] | None,
    text_overlay: dict[str, Any] | None,
    prompt_guidance: str,
    draft_prompt: str,
    request_params: dict[str, Any] | None = None,
    manual_prompt_reference: str = "",
    language: AssetLanguage = "zh",
    moderation_repair_note: str = "",
) -> str:
    agent = create_seedance2_prompt_composer_agent(language)
    result = await agent.run(
        build_seedance2_prompt_composer_task(
            mode=mode,
            beat=beat,
            assets=assets,
            text_overlay=text_overlay,
            prompt_guidance=prompt_guidance,
            draft_prompt=draft_prompt,
            request_params=request_params,
            manual_prompt_reference=manual_prompt_reference,
            language=language,
            moderation_repair_note=moderation_repair_note,
        )
    )
    prompt = normalize_seedance2_editor_prompt(result.output)
    if not prompt:
        raise ValueError("AI composer returned an empty prompt")
    return prompt


async def generate_seedance2_prompt(
    *,
    mode: Seedance2I2VMode | str,
    beat: dict[str, Any],
    assets: list[Any] | None,
    text_overlay: dict[str, Any] | None,
    prompt_guidance: str,
    prompt_guidance_template_keys: list[str] | tuple[str, ...] | None = None,
    request_params: dict[str, Any] | None = None,
    manual_prompt_reference: str = "",
    composer: Seedance2PromptComposer | None = None,
    language: AssetLanguage = "zh",
    moderation_repair_note: str = "",
) -> Seedance2PromptGeneration:
    """Generate a Seedance 2.0 prompt, preferring AI and falling back to rules."""

    localized_guidance = _localized_prompt_guidance(
        prompt_guidance, prompt_guidance_template_keys, language
    )
    draft_prompt = build_seedance2_prompt_draft(
        mode=mode,
        beat=beat,
        assets=assets,
        text_overlay=text_overlay,
        prompt_guidance=localized_guidance,
        manual_prompt_reference=manual_prompt_reference,
        language=language,
    )
    compose = composer or compose_seedance2_prompt_with_agent
    last_error = ""
    for attempt in range(1, SEEDANCE2_COMPOSER_ATTEMPTS + 1):
        try:
            prompt = await compose(
                mode=mode,
                beat=beat,
                assets=assets,
                text_overlay=text_overlay,
                prompt_guidance=localized_guidance,
                draft_prompt=draft_prompt,
                request_params=request_params or {},
                manual_prompt_reference=manual_prompt_reference,
                language=language,
                moderation_repair_note=moderation_repair_note,
            )
            prompt = normalize_seedance2_editor_prompt(prompt)
            if not prompt:
                raise ValueError("AI composer returned an empty prompt")
            return Seedance2PromptGeneration(
                prompt=prompt,
                used_ai=True,
                draft_prompt=draft_prompt,
                error="",
            )
        except Exception as exc:  # noqa: BLE001 - fall back to rules after retries
            last_error = str(exc)
            logger.warning(
                "Seedance2 prompt composer attempt %d/%d failed: %s",
                attempt,
                SEEDANCE2_COMPOSER_ATTEMPTS,
                last_error,
            )
    return Seedance2PromptGeneration(
        prompt=draft_prompt,
        used_ai=False,
        draft_prompt=draft_prompt,
        error=last_error,
    )
