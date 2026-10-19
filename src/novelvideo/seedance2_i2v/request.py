"""HuiMeng request construction for Seedance 2.0 image-to-video."""

from __future__ import annotations

import re
from typing import Any

from novelvideo.seedance2_i2v.models import (
    Seedance2I2VMode,
    Seedance2VideoConfig,
    parse_seedance2_config,
)


def clean_reference_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text:
            result.append(text)
    return result


def normalize_seedance2_prompt(prompt: str) -> str:
    return (
        str(prompt or "")
        .replace("@图片", "图片")
        .replace("@视频", "视频")
        .replace("@音频", "音频")
        .strip()
    )


def _common_params(config: Seedance2VideoConfig) -> dict[str, Any]:
    prompt = normalize_seedance2_prompt(config.final_prompt)
    if not prompt:
        raise ValueError("seedance2 final_prompt is required")
    params: dict[str, Any] = {
        "prompt": prompt,
        "duration": int(config.duration),
        "resolution": config.resolution or "720p",
        "ratio": config.ratio or "9:16",
        "generate_audio": bool(config.generate_audio),
        "return_last_frame": bool(config.return_last_frame),
    }
    if config.human_review:
        params["human_review"] = True
    scene_optimize = str(config.scene_optimize or "").strip()
    if scene_optimize:
        params["scene_optimize"] = scene_optimize
    return params


def _is_http_media_url(value: str) -> bool:
    return str(value or "").strip().startswith(("http://", "https://"))


def _validate_human_review_media_urls(params: dict[str, Any]) -> None:
    media_values: list[tuple[str, str]] = []
    for key in ("image_url", "first_frame_image", "last_frame_image"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            media_values.append((key, value.strip()))

    for array_key in ("reference_images", "reference_videos", "reference_audios"):
        for index, value in enumerate(params.get(array_key) or []):
            if isinstance(value, str) and value.strip():
                media_values.append((f"{array_key}[{index}]", value.strip()))

    invalid = [key for key, value in media_values if not _is_http_media_url(value)]
    if invalid:
        fields = ", ".join(invalid)
        raise ValueError(
            "human_review requires HTTP/HTTPS media URLs; "
            f"invalid field(s): {fields}. "
            "Local files must already be synced to OSS before HuiMeng material-review upload."
        )


#: 引用编号校验要跳过的「否定语境」。
#:
#: 生成的提示词常带「本镜无音频输入，不引用@音频1及任何后续编号」「不引用@图片2及以后任何
#: 编号」这类**声明不引用**的句子；它们不含真实引用，照字面校验会把整个 beat 直接判失败
#: （``提示词引用了 音频1，但当前请求只发送了 0 个音频素材``）。
_NEGATED_REFERENCE_MARKERS = (
    "不引用",
    "不发送",
    "不使用",
    "不包含",
    "不需要",
    "无需",
    "不含",
    "未引用",
    "未使用",
    "没有",
    "无",
    "非",
)
_NEGATION_LOOKBACK = 8


def _is_negated_reference(prompt: str, start: int) -> bool:
    """判断某个「图片N/视频N/音频N」是否处在否定语境里（即声明不引用）。"""

    window = str(prompt or "")[max(0, start - _NEGATION_LOOKBACK) : start]
    return any(window.endswith(marker) for marker in _NEGATED_REFERENCE_MARKERS)


def _rewrite_stale_image_references(prompt: str, *, image_count: int) -> str:
    """把越界的「图片N」引用改写为首帧表述，避免严格首帧模式直接校验失败。"""

    def replace(match: re.Match[str]) -> str:
        number = int(match.group("number"))
        if 1 <= number <= image_count:
            return match.group(0)
        if _is_negated_reference(match.string, match.start()):
            # 「不引用@图片2及以后任何编号」是在声明不引用，不该被改成首帧表述。
            return match.group(0)
        return "首帧画面"

    return re.sub(r"@?图片(?P<number>\d+)", replace, str(prompt or ""))


def _validate_prompt_reference_numbers(
    prompt: str,
    *,
    image_count: int,
    video_count: int,
    audio_count: int,
) -> None:
    limits = {
        "图片": int(image_count),
        "视频": int(video_count),
        "音频": int(audio_count),
    }
    text = str(prompt or "")
    for label, limit in limits.items():
        for match in re.finditer(rf"@?{label}(\d+)", text):
            if _is_negated_reference(text, match.start()):
                continue
            number = int(match.group(1))
            if 1 <= number <= limit:
                continue
            marker = f"{label}{number}"
            raise ValueError(
                f"提示词引用了 {marker}，但当前请求只发送了 {limit} 个{label}素材。"
            )


def _finalize_params(
    params: dict[str, Any],
    config: Seedance2VideoConfig,
    *,
    image_count: int,
    video_count: int = 0,
    audio_count: int = 0,
) -> dict[str, Any]:
    _validate_prompt_reference_numbers(
        params.get("prompt") or "",
        image_count=image_count,
        video_count=video_count,
        audio_count=audio_count,
    )
    if config.human_review:
        _validate_human_review_media_urls(params)
    return params


def build_seedance2_huimeng_params(
    config: Seedance2VideoConfig | dict[str, Any] | str,
    *,
    first_frame: str = "",
    last_frame: str = "",
    reference_images: list[str] | tuple[str, ...] | None = None,
    reference_videos: list[str] | tuple[str, ...] | None = None,
    reference_audios: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build mutually exclusive Seedance 2.0 params for HuiMeng."""

    config = parse_seedance2_config(config)
    params = _common_params(config)
    first_frame = str(first_frame or "").strip()
    last_frame = str(last_frame or "").strip()

    if config.mode == Seedance2I2VMode.TEXT_TO_VIDEO:
        return _finalize_params(params, config, image_count=0)

    if config.mode == Seedance2I2VMode.FIRST_LAST_FRAME:
        if not first_frame:
            raise ValueError("first_frame is required for Seedance 2.0 first-last-frame mode")
        if not last_frame:
            raise ValueError("last_frame is required for Seedance 2.0 first-last-frame mode")
        params["first_frame_image"] = first_frame
        params["last_frame_image"] = last_frame
        return _finalize_params(params, config, image_count=2)

    if config.mode == Seedance2I2VMode.MULTIMODAL_REFERENCE:
        images = clean_reference_list(reference_images)
        videos = clean_reference_list(reference_videos)
        audios = clean_reference_list(reference_audios)
        if not images and not videos:
            raise ValueError(
                "reference_images or reference_videos is required for Seedance 2.0 multimodal mode"
            )
        if len(images) > 9:
            raise ValueError("reference_images supports at most 9 images")
        if len(videos) > 3:
            raise ValueError("reference_videos supports at most 3 videos")
        if len(audios) > 3:
            raise ValueError("reference_audios supports at most 3 audios")
        if images:
            params["reference_images"] = images
        if videos:
            params["reference_videos"] = videos
        if audios:
            params["reference_audios"] = audios
        return _finalize_params(
            params,
            config,
            image_count=len(images),
            video_count=len(videos),
            audio_count=len(audios),
        )

    if not first_frame:
        raise ValueError("first_frame is required for Seedance 2.0 first-frame mode")
    audios = clean_reference_list(reference_audios)
    if len(audios) > 3:
        raise ValueError("reference_audios supports at most 3 audios")
    # 严格首帧：只发送首帧图，提示词里越界的参考图编号改写为首帧表述。
    params["prompt"] = _rewrite_stale_image_references(
        params.get("prompt") or "", image_count=1
    )
    params["image_url"] = first_frame
    if audios:
        params["reference_audios"] = audios
    return _finalize_params(
        params,
        config,
        image_count=1,
        audio_count=len(audios),
    )
