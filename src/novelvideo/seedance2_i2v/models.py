"""Typed Seedance 2.0 image-to-video configuration."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from novelvideo.seedance2_i2v.duration import SEEDANCE2_BASE_DURATION_SECONDS


SEEDANCE2_PROMPT_GUIDANCE_TEMPLATE_KEYS = frozenset(
    {"subject", "scene", "lighting", "camera", "style", "no_subtitle"}
)


class Seedance2I2VMode(str, Enum):
    TEXT_TO_VIDEO = "text_to_video"
    FIRST_FRAME = "first_frame"
    FIRST_LAST_FRAME = "first_last_frame"
    MULTIMODAL_REFERENCE = "multimodal_reference"


class Seedance2VideoConfig(BaseModel):
    """Per-beat Seedance 2.0 settings persisted in beats.seedance2_config_json."""

    mode: Seedance2I2VMode = Seedance2I2VMode.MULTIMODAL_REFERENCE
    # 严格首帧：视频第 0 帧必须等于 render 首帧（构图/景别/服装/脸型/场景不漂移）。
    # 默认开启；用户在 Prompt 面板显式选择其它模式时会被置为 False。
    strict_first_frame: bool = True
    final_prompt: str = ""
    prompt_guidance: str = ""
    prompt_guidance_template_keys: list[str] = Field(default_factory=list)
    prompt_source: str = ""
    prompt_inputs_hash: str = ""
    prompt_updated_at: str = ""
    duration: int = SEEDANCE2_BASE_DURATION_SECONDS
    resolution: str = "720p"
    ratio: str = "9:16"
    generate_audio: bool = True
    generate_audio_user_set: bool = False
    return_last_frame: bool = False
    human_review: bool = True
    human_review_user_set: bool = False
    scene_optimize: str = ""
    reference_image_paths: list[str] = Field(default_factory=list)
    reference_audio_paths: list[str] = Field(default_factory=list)
    text_overlay: dict[str, Any] = Field(default_factory=dict)
    selected_asset_keys: list[str] = Field(default_factory=list)

    @field_validator(
        "final_prompt",
        "prompt_guidance",
        "prompt_source",
        "prompt_inputs_hash",
        "prompt_updated_at",
        "resolution",
        "ratio",
        "scene_optimize",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("duration", mode="before")
    @classmethod
    def _coerce_duration(cls, value: Any) -> int:
        try:
            duration = int(float(value or SEEDANCE2_BASE_DURATION_SECONDS))
        except (TypeError, ValueError):
            duration = SEEDANCE2_BASE_DURATION_SECONDS
        return max(1, duration)

    @field_validator("prompt_guidance_template_keys", mode="before")
    @classmethod
    def _normalize_guidance_template_keys(cls, value: Any) -> list[str]:
        values = value if isinstance(value, (list, tuple)) else []
        normalized: list[str] = []
        for item in values:
            key = str(item or "").strip()
            if key in SEEDANCE2_PROMPT_GUIDANCE_TEMPLATE_KEYS and key not in normalized:
                normalized.append(key)
        return normalized


def parse_seedance2_config(value: Any) -> Seedance2VideoConfig:
    """Parse a stored dict/JSON config into a normalized config object."""

    if isinstance(value, Seedance2VideoConfig):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return Seedance2VideoConfig()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return Seedance2VideoConfig(final_prompt=text)
    if isinstance(value, dict):
        value = dict(value)
        if value.get("generate_audio") is False and not value.get("generate_audio_user_set"):
            value["generate_audio"] = True
        if value.get("human_review") is False and not value.get("human_review_user_set"):
            value["human_review"] = True
        if value.get("strict_first_frame") is not False:
            # 默认强锁严格首帧：视频阶段以 render 图作为硬首帧输入，
            # 避免模型在多参考模式下换装/换脸/把全身镜头裁成特写。
            stored_mode = (
                str(value.get("mode") or "").strip()
                or Seedance2I2VMode.MULTIMODAL_REFERENCE.value
            )
            if stored_mode in {
                Seedance2I2VMode.MULTIMODAL_REFERENCE.value,
                Seedance2I2VMode.FIRST_FRAME.value,
            }:
                value["mode"] = Seedance2I2VMode.FIRST_FRAME.value
        return Seedance2VideoConfig.model_validate(value)
    return Seedance2VideoConfig()


def dump_seedance2_config(config: Seedance2VideoConfig | dict[str, Any] | str | None) -> str:
    """Serialize config for SQLite storage."""

    return parse_seedance2_config(config).model_dump_json()
