"""Seedance 2.0 clip-duration rules shared by the panel and the pipeline.

The product basis is a 5 second shot. That is also what the configured backend
actually delivers: every Seedance 2.0 clip produced through the instance gateway
comes back at ~5.09s no matter which duration the request asked for, so a longer
request only inflates the beat clock written into the prompt (and the per-second
gateway billing) while the delivered clip stays 5s.

The dub length therefore must not raise the generated duration. ``duration_floor``
still reports the length a talking beat would need so the panel can point out a
line that does not fit, while ``resolve_seedance2_target_duration`` keeps the
generated shot on the 5s basis unless a user explicitly asked for more.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from novelvideo.manual_shots import resolve_target_video_duration

SEEDANCE2_BASE_DURATION_SECONDS = 5
SEEDANCE2_MAX_DURATION_SECONDS = 15
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

    Informational only: it feeds the panel's "台词比镜头长" hint. It must not be
    used as the generated duration, because the backend ignores requests above
    its ~5s ceiling and the prompt's beat clock would then describe a clip that
    never arrives.

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
) -> int:
    """Pick the effective clip seconds sent to the backend for one beat.

    The product basis is 5 seconds and the configured backend caps its output at
    about 5 seconds regardless of the requested value, so the dub length is not a
    candidate here (see the module docstring). Only an explicit user value can
    raise the shot above the basis.
    """

    candidates = [SEEDANCE2_BASE_DURATION_SECONDS]
    for value in (config_duration, requested):
        try:
            parsed = int(math.ceil(float(value)))
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            candidates.append(parsed)
    return _clamp_duration(max(candidates), minimum=SEEDANCE2_BASE_DURATION_SECONDS)


def _clamp_duration(value: Any, *, minimum: int) -> int:
    try:
        seconds = int(math.ceil(float(value)))
    except (TypeError, ValueError):
        seconds = minimum
    return max(minimum, min(seconds, SEEDANCE2_MAX_DURATION_SECONDS))


__all__ = [
    "SEEDANCE2_BASE_DURATION_SECONDS",
    "SEEDANCE2_MAX_DURATION_SECONDS",
    "beat_audio_path",
    "resolve_seedance2_target_duration",
    "seedance2_duration_floor",
    "spoken_audio_seconds",
]
