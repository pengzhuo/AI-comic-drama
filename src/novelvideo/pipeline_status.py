"""Shared mainline pipeline stage/status computation.

Both the read-only ``GET /pipeline/status`` route and the project-level auto
coordinator read this module, so "what is the next step" and "which episode is
next" have a single source of truth instead of two drifting copies.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool

from novelvideo.project_context import ProjectContext
from novelvideo.script_process import has_script_process_result
from novelvideo.sqlite_store import SQLiteStore
from novelvideo.task_state import get_task_manager
from novelvideo.utils.path_resolver import compute_identity_path, compute_portrait_path

_log = logging.getLogger(__name__)


STEP_MAP: dict[str, tuple[str | None, str]] = {
    "ingest": ("ingest_fast", "小说摄入"),
    "script_process": ("script_process", "剧本处理"),
    "configure": (None, "配置项目"),
    "characters": ("build_characters", "角色提取"),
    "episodes": ("build_episodes", "分集规划"),
    "portraits": (None, "肖像生成"),
    "identity_plan": (None, "身份规划"),
    "identity_images": (None, "身份图生成"),
    "script": ("script_writer", "脚本生成"),
    "sketches": ("sketch_generation", "草图生成"),
    "coloring": (None, "配色+身份/道具检测"),
    "global_optimize": ("global_optimize_video", "全局视频优化"),
    "first_frames": ("selected_regen", "首帧生成"),
    "voice_setup": (None, "准备配音声线"),
    "tts": (None, "TTS 配音"),
    "seedance2_prompt": ("seedance2_prompt", "Seedance 2.0 提示词"),
    "video": ("single_video", "视频生成"),
    "compose": ("compose_episode", "合成导出"),
    "done": (None, "全部完成"),
}


def _all_or_empty(items: list[bool]) -> bool:
    return bool(items) and all(items)


def _user_has_configured(username: str, project: str) -> bool:
    from novelvideo.project_config import load_project_config

    config = load_project_config(username, project)
    return bool(config.get("ethnicity") or config.get("narration_style"))


def _file_series_complete(directory: Path, suffix: str, count: int) -> bool:
    if count <= 0:
        return False
    return all((directory / f"beat_{i + 1:02d}.{suffix}").exists() for i in range(count))


def _beat_file_series_complete(directory: Path, suffix: str, beats: list[dict]) -> bool:
    beat_numbers = [
        int(beat.get("beat_number", 0) or 0)
        for beat in beats
        if int(beat.get("beat_number", 0) or 0) > 0
    ]
    if not beat_numbers:
        return False
    return all((directory / f"beat_{beat_num:02d}.{suffix}").exists() for beat_num in beat_numbers)


def _beat_sketch_series_complete(directory: Path, beats: list[dict]) -> bool:
    """Every beat has a canonical sketch (``beat_NN.png`` or ``beat_NN.jpg``).

    This mirrors the optimizer/billing eligibility for ``global_optimize_video``
    (``_global_optimize_billable_beat_numbers``), which only processes beats with a
    canonical sketch. Declaring "草图生成" complete after the first grid leaves the
    remaining beats unsketchable, so ``global_optimize`` can never satisfy its
    all-beats predicate and the pipeline deadlocks on "全局视频优化".
    """

    beat_numbers = [
        int(beat.get("beat_number", 0) or 0)
        for beat in beats
        if int(beat.get("beat_number", 0) or 0) > 0
    ]
    if not beat_numbers:
        return False
    return all(
        any(
            (directory / f"beat_{beat_num:02d}.{suffix}").exists()
            for suffix in ("png", "jpg")
        )
        for beat_num in beat_numbers
    )


def _beat_requires_audio(beat: dict) -> bool:
    from novelvideo.seedance2_i2v.voice_clone import (
        dialogue_text,
        narration_beat_text,
        normalize_seedance2_audio_type,
    )

    audio_type = normalize_seedance2_audio_type(beat)
    if audio_type == "narration":
        return bool(narration_beat_text(beat))
    if audio_type == "dialogue":
        return bool(dialogue_text(beat))
    return False


def _beat_audio_series_complete(directory: Path, beats: list[dict]) -> bool:
    required_numbers = [
        int(beat.get("beat_number", 0) or 0)
        for beat in beats
        if int(beat.get("beat_number", 0) or 0) > 0 and _beat_requires_audio(beat)
    ]
    return all((directory / f"beat_{beat_num:02d}.mp3").exists() for beat_num in required_numbers)


def _beat_has_script_content(beat: dict) -> bool:
    return bool(
        str(beat.get("narration_segment") or "").strip()
        or str(beat.get("narration") or "").strip()
        or str(beat.get("visual_description") or "").strip()
    )


def beat_seedance2_prompt_ready(beat: dict) -> bool:
    """Whether the beat carries the Seedance 2.0 final prompt its render needs."""

    from novelvideo.seedance2_i2v.models import parse_seedance2_config

    try:
        config = parse_seedance2_config(beat.get("seedance2_config_json"))
    except Exception:  # noqa: BLE001 - a broken row must not break the whole status
        return False
    return bool(str(config.final_prompt or "").strip())


def seedance2_prompt_required(username: str, project: str) -> bool:
    """Whether this project renders with a Seedance 2.0-family backend.

    Only those backends refuse to render a beat whose 2.0 final prompt is empty
    (``beat_seedance2_final_prompt_missing``), so every other backend treats the
    step as vacuously complete and its ordering is unchanged.
    """

    from novelvideo.project_config import effective_video_backend
    from novelvideo.seedance2_i2v.pipeline import is_huimeng_seedance2_backend

    return is_huimeng_seedance2_backend(effective_video_backend(username, project))


async def _collect_voice_prereq_errors(
    *,
    store: SQLiteStore,
    username: str,
    project: str,
    episode: int,
) -> list[str]:
    """Check voice references without dispatching an audio generation task."""

    from novelvideo.audio.indextts2_beat_audio_task import (
        collect_indextts2_voice_prereq_errors,
    )

    try:
        return await collect_indextts2_voice_prereq_errors(
            store=store,
            username=username,
            project=project,
            episode=episode,
            beat_numbers=None,
            mode="sync_changed",
        )
    except AttributeError:
        # Narrow API test stores may not implement the complete voice surface.
        return []
    except Exception:
        _log.exception(
            "failed to inspect audio voice prerequisites: user=%s project=%s episode=%s",
            username,
            project,
            episode,
        )
        return ["暂时无法检查配音声线，请稍后重试"]


def final_video_path(project_dir: Path, episode: int) -> Path:
    return project_dir / "videos" / "episodes" / f"ep{episode:03d}_final.mp4"


def incomplete_episodes(store: SQLiteStore, project_dir: Path) -> list[int]:
    """Episode numbers that have no final composed video yet, ascending."""

    episodes = store.get_all_episodes()
    result: list[int] = []
    for ep in sorted(episodes, key=lambda item: getattr(item, "number", 0)):
        number = int(getattr(ep, "number", 0) or 0)
        if number <= 0:
            continue
        if not final_video_path(project_dir, number).exists():
            result.append(number)
    return result


def character_needs_portrait(character: Any) -> bool:
    """Whether a character must own a ``portrait.png`` face anchor.

    Main characters always need one. A non-main character needs one too once it
    owns identities, because an identity image whose age matches the character
    is rendered from that character portrait. Characters without a face prompt
    cannot be rendered at all, so they are excluded instead of wedging the
    pipeline on an unsatisfiable prerequisite.
    """
    if not str(getattr(character, "face_prompt", "") or "").strip():
        return False
    return bool(getattr(character, "is_main", False)) or bool(
        getattr(character, "identities", None)
    )


async def compute_pipeline_status(
    *,
    store: SQLiteStore,
    username: str,
    project_name: str,
    project_dir: Path,
    ctx: ProjectContext | None = None,
    episode: int | None = None,
) -> dict[str, Any]:
    """Compute the aggregate pipeline status for a project or one episode.

    The returned mapping is the ``data`` payload of the API response; callers
    wrap it with ``{"ok": True, "data": ...}``.
    """

    mgr = get_task_manager()

    characters = store.get_all_characters()
    episodes = store.get_all_episodes()
    portrait_chars = [c for c in characters if character_needs_portrait(c)]

    ingest_task = (
        await run_in_threadpool(
            mgr.get_task_for_project,
            ctx,
            "ingest_fast",
            0,
        )
        if ctx
        else await run_in_threadpool(
            mgr.get_task,
            "ingest_fast",
            username,
            project_name,
            0,
        )
    )
    ingested = (
        bool(ingest_task and ingest_task.status == "completed")
        or bool(characters)
        or bool(episodes)
    )
    configured = _user_has_configured(username, project_name)
    portraits_done = bool(portrait_chars) and all(
        bool(compute_portrait_path(project_dir, c.name)) for c in portrait_chars
    )
    # 导入后先按内置流程包处理剧本，再进入角色/分集等后续阶段。产物存在即视为
    # 本阶段完成。为不打断存量项目（角色/分集已在推进、甚至视频已在渲染），
    # 只要分集已经建立就不再回灌这个新阶段；存量项目仍可用手动端点重跑。
    script_processed = has_script_process_result(project_dir)
    script_process_pending = (
        ingested and not script_processed and not episodes
    )

    global_status = {
        "ingested": ingested,
        "script_processed": script_processed,
        "script_process_pending": script_process_pending,
        "configured": configured,
        "characters": len(characters),
        "episodes": len(episodes),
        "portraits_done": portraits_done,
    }

    if not (ingested and configured and characters and episodes and portraits_done):
        if not ingested:
            next_step = "ingest"
        elif script_process_pending:
            next_step = "script_process"
        elif not configured:
            next_step = "configure"
        elif not characters:
            next_step = "characters"
        elif not episodes:
            next_step = "episodes"
        else:
            next_step = "portraits"
        task_type, step_name = STEP_MAP[next_step]
        return {
            "project": project_name,
            "global": global_status,
            "current_episode": None,
            "episode_status": None,
            "next_step": task_type or next_step,
            "next_step_name": step_name,
        }

    target_ep = episode
    if target_ep is None:
        unfinished = [
            ep.number
            for ep in sorted(episodes, key=lambda item: getattr(item, "number", 0))
            if not final_video_path(project_dir, ep.number).exists()
        ]
        target_ep = unfinished[0] if unfinished else max((ep.number for ep in episodes), default=1)

    target_episode = store.get_episode(target_ep)
    identity_ids = set(getattr(target_episode, "identity_ids", []) or [])
    has_identity_plan = bool(identity_ids)
    # Which planned identities still need an image. Reported so the agent never
    # has to guess a per-episode identity route to find out what is missing.
    planned_identities = [
        (char, ident)
        for char in characters
        for ident in (getattr(char, "identities", []) or [])
        if ident.identity_id in identity_ids
    ]
    missing_identities = [
        {
            "character": char.name,
            "identity_id": ident.identity_id,
            "identity_name": ident.identity_name,
        }
        for char, ident in planned_identities
        if not compute_identity_path(project_dir, char.name, ident.identity_name)
    ]
    has_identity_images = has_identity_plan and not missing_identities

    beats = await store.get_beats_as_dicts(target_ep)
    has_script = _all_or_empty([_beat_has_script_content(b) for b in beats])

    sketches_dir = project_dir / "sketches" / f"ep{target_ep:03d}"
    has_sketches = _beat_sketch_series_complete(sketches_dir, beats)

    has_coloring = has_sketches and _all_or_empty(
        [
            b.get("detected_identities") is not None or b.get("detected_props") is not None
            for b in beats
        ]
    )
    has_global_optimize = _all_or_empty(
        [bool(b.get("video_mode")) and bool(b.get("video_prompt")) for b in beats]
    )

    has_seedance2_prompt = (
        not seedance2_prompt_required(username, project_name)
        or _all_or_empty([beat_seedance2_prompt_ready(b) for b in beats])
    )

    episode_status = {
        "identity_plan": has_identity_plan,
        "identity_images": has_identity_images,
        "identity_images_planned": len(planned_identities),
        "identity_images_done": len(planned_identities) - len(missing_identities),
        "identities_missing": missing_identities,
        "script": has_script,
        "sketches": has_sketches,
        "coloring": has_coloring,
        "global_optimize": has_global_optimize,
        "first_frames": _beat_file_series_complete(
            project_dir / "frames" / f"ep{target_ep:03d}", "png", beats
        ),
        "tts": _beat_audio_series_complete(
            project_dir / "audio" / f"ep{target_ep:03d}", beats
        ),
        "seedance2_prompt": has_seedance2_prompt,
        "video": _beat_file_series_complete(
            project_dir / "videos" / "beats" / f"ep{target_ep:03d}", "mp4", beats
        ),
    }

    next_step = "done"
    for key in (
        "identity_plan",
        "identity_images",
        "script",
        "sketches",
        "coloring",
        "global_optimize",
        "first_frames",
        "tts",
        "seedance2_prompt",
        "video",
    ):
        if not episode_status[key]:
            next_step = key
            break

    audio_prerequisites = {
        "checked": False,
        "ready": None,
        "errors": [],
    }
    if next_step == "tts":
        voice_errors = await _collect_voice_prereq_errors(
            store=store,
            username=username,
            project=project_name,
            episode=target_ep,
        )
        audio_prerequisites = {
            "checked": True,
            "ready": not voice_errors,
            "errors": voice_errors,
        }
        if voice_errors:
            next_step = "voice_setup"
    elif episode_status["tts"]:
        audio_prerequisites = {
            "checked": True,
            "ready": True,
            "errors": [],
        }
    if next_step == "done" and not final_video_path(project_dir, target_ep).exists():
        next_step = "compose"

    task_type, step_name = STEP_MAP[next_step]
    return {
        "project": project_name,
        "global": global_status,
        "current_episode": target_ep,
        "episode_status": episode_status,
        "audio_prerequisites": audio_prerequisites,
        "next_step": task_type or next_step,
        "next_step_name": step_name,
    }
