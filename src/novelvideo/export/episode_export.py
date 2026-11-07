"""Episode subtitle and archive export helpers shared by API and legacy UI."""

from __future__ import annotations

from pathlib import Path

from novelvideo.utils.async_ops import call_blocking
from novelvideo.utils.media_io import get_audio_duration_async


def format_srt_time(seconds: float) -> str:
    """Format seconds as SRT time: HH:MM:SS,mmm."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


async def build_srt_content(project_dir: Path, episode: int, beats: list[dict]) -> str:
    """Build the exported SRT using the same rule the burned-in film uses.

    字幕口径与成片一致（``beat_subtitle_text``）：只出角色**对话台词**，旁白/解说
    与静音 beat 都不出字幕。时间轴仍按**每个** beat 推进——优先成片里那条
    ``beat_XX.mp4`` 的时长，其次配音 ``beat_XX.mp3``，都没有才退回 5 秒；否则被
    跳过的 beat 会让后面的 cue 越来越早、与画面对不上。

    台词再按标点**逐句拆开**（``subtitle_cue_windows``）：按字数占比把该 beat 的
    说话窗口依次分给各句，让字幕跟着说话一句一句出，而不是整段一次铺满。
    """

    from novelvideo.seedance2_i2v.spoken_dialogue import (
        beat_subtitle_text,
        subtitle_cue_windows,
    )

    audio_dir = project_dir / "audio" / f"ep{episode:03d}"
    video_dir = project_dir / "videos" / "beats" / f"ep{episode:03d}"
    srt_lines: list[str] = []
    current_time = 0.0
    seq = 0

    for index, beat in enumerate(beats, 1):
        beat_num = beat.get("beat_number", index)
        duration = await _beat_timeline_seconds(video_dir, audio_dir, beat_num)

        start = current_time
        end = current_time + duration
        current_time = end

        text = beat_subtitle_text(beat)
        if not text:
            continue

        for cue_text, cue_start, cue_end in subtitle_cue_windows(text, start, end):
            seq += 1
            srt_lines.append(f"{seq}")
            srt_lines.append(
                f"{format_srt_time(cue_start)} --> {format_srt_time(cue_end)}"
            )
            srt_lines.append(cue_text)
            srt_lines.append("")

    return "\n".join(srt_lines)


async def _beat_timeline_seconds(video_dir: Path, audio_dir: Path, beat_num: int) -> float:
    """这一 beat 在成片时间轴上占的秒数：成片片段 → 配音 → 5 秒兜底。"""

    for candidate in (
        video_dir / f"beat_{beat_num:02d}.mp4",
        audio_dir / f"beat_{beat_num:02d}.mp3",
    ):
        if not candidate.exists():
            continue
        try:
            duration = await get_audio_duration_async(str(candidate))
        except Exception:
            continue
        if duration and duration > 0:
            return float(duration)
    return 5.0


async def build_episode_srt_file(project_dir: Path, episode: int, beats: list[dict]) -> Path | None:
    """Generate and return the episode SRT file path."""
    if not beats:
        return None

    srt_content = await build_srt_content(project_dir, episode, beats)
    if not srt_content:
        return None

    srt_dir = project_dir / "videos" / "episodes"
    srt_dir.mkdir(parents=True, exist_ok=True)
    srt_path = srt_dir / f"ep{episode:03d}.srt"
    srt_path.write_text(srt_content, encoding="utf-8")
    return srt_path


async def build_episode_zip_file(
    project_dir: Path,
    project_name: str,
    episode: int,
    beats: list[dict],
) -> Path | None:
    """Generate and return the episode ZIP file path."""
    import zipfile

    from novelvideo.utils.path_resolver import PathResolver

    paths = PathResolver(str(project_dir), episode)
    files_to_pack: list[tuple[Path, str]] = []

    for beat in beats:
        beat_num = beat.get("beat_number", 0)
        if beat_num <= 0:
            continue
        audio_path = paths.audio(beat_num)
        if audio_path.exists():
            files_to_pack.append((audio_path, f"audio/{audio_path.name}"))
        video_path = paths.video(beat_num)
        if video_path.exists():
            files_to_pack.append((video_path, f"video/{video_path.name}"))

    final_path = paths.final_video()
    if final_path.exists():
        files_to_pack.append((final_path, final_path.name))

    srt_path = await build_episode_srt_file(project_dir, episode, beats)
    if srt_path and srt_path.exists():
        files_to_pack.append((srt_path, srt_path.name))

    if not files_to_pack:
        return None

    zip_dir = project_dir / "videos" / "episodes"
    zip_dir.mkdir(parents=True, exist_ok=True)
    zip_path = zip_dir / f"{project_name}_第{episode}集.zip"

    def _write_zip_file() -> None:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zip_file:
            for local_path, arc_name in files_to_pack:
                zip_file.write(local_path, arc_name)

    await call_blocking(_write_zip_file)
    return zip_path


__all__ = [
    "build_episode_srt_file",
    "build_episode_zip_file",
    "build_srt_content",
    "format_srt_time",
]
