"""Seedance 2.x clip-duration rules shared by the panel and the pipeline.

The product basis is a 5 second floor. Seedance 2.0 tops out at 15s and Seedance
2.5 at 30s, so the effective request is the shot's own content length (dub length
or explicit manual length) clamped into the model's supported range.

``resolve_seedance2_target_duration`` raises the request to the beat's content
length instead of pinning every shot to the 5s basis, and callers that know the
selected video backend pass ``max_duration`` so a 2.0 model still stops at 15s
while a 2.5 model can reach 30s. ``duration_floor`` keeps reporting the length a
talking beat would need for the panel's "台词比镜头长" hint.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from novelvideo.manual_shots import resolve_target_video_duration

SEEDANCE2_BASE_DURATION_SECONDS = 5
# Seedance 2.5 常规单次上限 30s（2.0 系列仍由各自的 NEWAPI_VIDEO_DURATION_BOUNDS
# 在生成器处回落到 15s）。这里给的是不指定模型时的全局上限。
SEEDANCE2_MAX_DURATION_SECONDS = 30
_SPOKEN_AUDIO_TYPES = frozenset({"narration", "dialogue"})


def beat_audio_path(project_dir: str | Path, episode: int, beat_num: int) -> Path:
    """Path of the beat's dubbing track (shared by Seedance2 and IndexTTS2)."""

    return (
        Path(project_dir)
        / "audio"
        / f"ep{int(episode):03d}"
        / f"beat_{int(beat_num):02d}.mp3"
    )


def spoken_audio_seconds(
    *,
    project_dir: str | Path,
    episode: int,
    beat: dict[str, Any],
) -> float | None:
    """Length of the beat's dubbing track when the beat actually speaks."""

    from novelvideo.seedance2_i2v.voice_clone import normalize_seedance2_audio_type
    from novelvideo.utils.media_io import get_audio_duration

    try:
        beat_num = int(beat.get("beat_number") or 0)
    except (TypeError, ValueError):
        return None
    if beat_num <= 0:
        return None
    if normalize_seedance2_audio_type(beat) not in _SPOKEN_AUDIO_TYPES:
        return None
    audio_path = beat_audio_path(project_dir, episode, beat_num)
    if not audio_path.exists():
        return None
    try:
        seconds = float(get_audio_duration(str(audio_path)))
    except Exception:  # noqa: BLE001 - 探测失败时按无音频处理，不阻断生成
        return None
    return seconds if seconds > 0 else None


def seedance2_duration_floor(
    *,
    project_dir: str | Path,
    episode: int,
    beat: dict[str, Any],
) -> int:
    """Minimum length a talking beat would need for its spoken line.

    Informational only: it feeds the panel's "台词比镜头长" hint. It mirrors the
    length :func:`resolve_target_video_duration` would pick (explicit manual
    length, else the spoken track, else the by-text estimate), clamped into the
    Seedance range.

    ``resolve_target_video_duration`` already prefers an explicit beat duration
    and otherwise falls back to the spoken track, so the floor respects a manual
    shot length and never lets a dialogue beat run shorter than its line.
    """

    audio_seconds = spoken_audio_seconds(
        project_dir=project_dir,
        episode=episode,
        beat=beat,
    )
    return _clamp_duration(
        resolve_target_video_duration(beat, audio_seconds),
        minimum=SEEDANCE2_BASE_DURATION_SECONDS,
    )


def resolve_seedance2_target_duration(
    *,
    config_duration: Any = None,
    requested: Any = None,
    max_duration: int | None = None,
) -> int:
    """Pick the effective clip seconds sent to the backend for one beat.

    The request is the beat's own content length (dub length / explicit manual
    length), never below the 5s basis and never above the model's ceiling.
    ``max_duration`` lets a caller that knows the selected backend pin the
    ceiling to that model (Seedance 2.0 -> 15s, Seedance 2.5 -> 30s); otherwise
    the global :data:`SEEDANCE2_MAX_DURATION_SECONDS` applies.
    """

    candidates = [SEEDANCE2_BASE_DURATION_SECONDS]
    for value in (config_duration, requested):
        try:
            parsed = int(math.ceil(float(value)))
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            candidates.append(parsed)
    ceiling = SEEDANCE2_MAX_DURATION_SECONDS
    if max_duration is not None:
        try:
            ceiling = max(SEEDANCE2_BASE_DURATION_SECONDS, int(max_duration))
        except (TypeError, ValueError):
            ceiling = SEEDANCE2_MAX_DURATION_SECONDS
    return _clamp_duration(
        max(candidates),
        minimum=SEEDANCE2_BASE_DURATION_SECONDS,
        maximum=ceiling,
    )


def _clamp_duration(
    value: Any,
    *,
    minimum: int,
    maximum: int = SEEDANCE2_MAX_DURATION_SECONDS,
) -> int:
    try:
        seconds = int(math.ceil(float(value)))
    except (TypeError, ValueError):
        seconds = minimum
    return max(minimum, min(seconds, maximum))


__all__ = [
    "SEEDANCE2_BASE_DURATION_SECONDS",
    "SEEDANCE2_MAX_DURATION_SECONDS",
    "beat_audio_path",
    "resolve_seedance2_target_duration",
    "seedance2_duration_floor",
    "spoken_audio_seconds",
]
