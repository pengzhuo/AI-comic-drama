"""Celery runner for project audio generation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from novelvideo.egress_context import (
    TRUSTED_EGRESS_CONTEXT_KEY,
    TrustedEgressContext,
    TrustedRunnerEnvelope,
)
from novelvideo.project_context import ProjectContext
from novelvideo.task_backend.cancel import await_envelope_with_cancel_watch
from novelvideo.task_backend.envelope import InvalidTaskEnvelope
from novelvideo.task_backend.registry import register_project_task_runner
from novelvideo.task_state import get_task_manager

logger = logging.getLogger(__name__)

INDEXTTS2_AUDIO_TASK_TYPE = "audio_generation_indextts2"
SYSTEM_VOICE_SETUP_TASK_TYPE = "system_voice_setup"
EPISODE_AUDIO_DESIGN_TASK_TYPE = "episode_audio_design"


def run_system_voice_setup(envelope: dict[str, Any], ctx: ProjectContext) -> dict[str, Any]:
    return asyncio.run(
        await_envelope_with_cancel_watch(
            _run_system_voice_setup(envelope, ctx),
            envelope,
            task_type=SYSTEM_VOICE_SETUP_TASK_TYPE,
        )
    )


async def _run_system_voice_setup(
    envelope: dict[str, Any],
    ctx: ProjectContext,
) -> dict[str, Any]:
    from novelvideo.audio.system_voice_setup import prepare_missing_system_voices
    from novelvideo.sqlite_store import SQLiteStore

    payload = envelope.get("payload") or {}
    episode = int(envelope.get("episode") or payload.get("episode") or 0)
    manager = get_task_manager()
    store = SQLiteStore(
        ctx.owner_project_label,
        output_dir=str(ctx.output_dir),
        state_dir=str(ctx.state_dir),
    )
    await store.initialize()
    # ``prepare_missing_system_voices`` reads characters from SQLite and then
    # persists the selected voice through ``update_character``.  The latter
    # resolves characters from the store's in-memory index, so a fresh task
    # worker must hydrate that index before attempting the update.
    await store.load_graph_state()
    manager.update_progress_for_project(
        ctx,
        SYSTEM_VOICE_SETUP_TASK_TYPE,
        episode,
        progress=0.05,
        current_task="正在匹配系统声线",
        logs=["开始准备缺失的系统声线"],
    )
    try:
        result = await prepare_missing_system_voices(
            store=store,
            username=ctx.owner_username,
            project=ctx.project_name,
            project_dir=ctx.output_dir,
            episode=episode,
        )
        if not result.get("ready"):
            errors = list(result.get("remaining_errors") or [])
            detail = "；".join(str(error) for error in errors[:5])
            raise RuntimeError(detail or "系统声线准备后仍有缺失项")
        manager.update_progress_for_project(
            ctx,
            SYSTEM_VOICE_SETUP_TASK_TYPE,
            episode,
            progress=1.0,
            current_task="系统声线准备完成",
            logs=[f"已准备 {int(result.get('prepared_count') or 0)} 项系统声线"],
        )
        _continue_voice_paused_director_auto(ctx)
        return result
    finally:
        await store.close()


def run_episode_audio_design(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    """整集「声音层」：设计并生成配乐 / 环境音 / 音效（见
    :mod:`novelvideo.audio.episode_audio_design`）。

    输入是**已经生成好的整集视频**（这一阶段跑在视频之后、合成导出之前），
    因此时间轴用每镜视频的真实时长累计，混音时才能对上画面。
    """

    return asyncio.run(
        await_envelope_with_cancel_watch(
            _run_episode_audio_design(envelope, ctx),
            envelope,
            task_type=EPISODE_AUDIO_DESIGN_TASK_TYPE,
        )
    )


def _probe_media_duration(path: Path) -> float:
    import subprocess

    if not path.exists():
        return 0.0
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return max(0.0, float(str(result.stdout).strip() or 0.0))
    except Exception:  # noqa: BLE001 - 探测失败按未知时长处理
        return 0.0


async def _run_episode_audio_design(
    envelope: dict[str, Any],
    ctx: ProjectContext,
) -> dict[str, Any]:
    from novelvideo.audio.episode_audio_design import (
        STAGE,
        audio_design_caps,
        generate_episode_audio_design,
    )
    from novelvideo.project_config import load_project_config
    from novelvideo.sqlite_store import SQLiteStore

    payload = envelope.get("payload") or {}
    episode = int(envelope.get("episode") or payload.get("episode") or 0)
    manager = get_task_manager()
    store = SQLiteStore(
        ctx.owner_project_label,
        output_dir=str(ctx.output_dir),
        state_dir=str(ctx.state_dir),
    )
    await store.initialize()
    try:
        beats = await store.get_beats_as_dicts(episode)
    finally:
        await store.close()
    if not beats:
        raise RuntimeError(f"第 {episode} 集没有分镜，无法设计声音层")

    project_dir = Path(str(ctx.output_dir))
    timeline: list[tuple[dict[str, Any], float, float]] = []
    cursor = 0.0
    for beat in beats:
        beat_number = int(beat.get("beat_number") or 0)
        video_path = (
            project_dir
            / "videos"
            / "beats"
            / f"ep{episode:03d}"
            / f"beat_{beat_number:02d}.mp4"
        )
        duration = _probe_media_duration(video_path)
        if duration <= 0:
            duration = float(beat.get("duration_seconds") or 0.0) or 4.0
        timeline.append((beat, cursor, cursor + duration))
        cursor += duration

    style_hint = ""
    try:
        config = load_project_config(ctx.owner_username, ctx.project_name)
        style_hint = str(
            config.get("visual_style") or config.get("genre") or ""
        ).strip()
    except Exception:  # noqa: BLE001 - 风格提示缺失不影响设计
        style_hint = ""

    manager.update_progress_for_project(
        ctx,
        STAGE,
        episode,
        progress=0.05,
        current_task="正在设计整集声音层",
        logs=[f"共 {len(timeline)} 镜，预计时长 {cursor:.1f}s"],
    )

    max_ambience, max_sfx = audio_design_caps()

    def _log(message: str) -> None:
        manager.update_progress_for_project(ctx, STAGE, episode, logs=[message])

    def _progress(done: int, total: int) -> None:
        ratio = 0.05 + 0.9 * (done / total if total else 1.0)
        manager.update_progress_for_project(
            ctx,
            STAGE,
            episode,
            progress=min(0.95, ratio),
            current_task=f"正在生成声音层 {done}/{total}",
        )

    result = await generate_episode_audio_design(
        project_dir=project_dir,
        episode=episode,
        timeline=timeline,
        style_hint=style_hint,
        max_ambience=max_ambience,
        max_sfx=max_sfx,
        egress_context=_extract_trusted_egress_context(envelope),
        log=_log,
        progress=_progress,
    )
    manager.update_progress_for_project(
        ctx,
        STAGE,
        episode,
        progress=1.0,
        current_task="声音层设计完成",
        logs=[
            f"声音层 cue {int(result.get('cue_count') or 0)} 条"
            f"（新生成 {int(result.get('generated') or 0)}，复用 "
            f"{int(result.get('reused') or 0)}）"
        ],
    )
    return result


def _continue_voice_paused_director_auto(ctx: ProjectContext) -> None:
    """Hand a voice-gate-paused 本集自动 run back to the coordinator.

    The gate parks the run with ``pause()``, and nothing else resumes a
    ``paused`` run: preparing the system voices is the user's own choice of that
    policy, so the run has to be picked back up here or it stays parked forever.
    Strictly best-effort — the voices are already prepared, and a failed hand-off
    must not turn this task into a failure.
    """

    try:
        from novelvideo.chat.director_auto import coordinator as director_auto_coordinator

        project_id = str(getattr(ctx, "project_id", "") or "")
        username = str(getattr(ctx, "owner_username", "") or "")
        if not project_id or not username:
            return
        director_auto_coordinator.resume_after_system_voice_setup_from_thread(
            username=username,
            project_id=project_id,
        )
    except Exception:  # noqa: BLE001 - continuation is best-effort
        logger.exception(
            "failed to continue director auto after system voice setup: project_id=%s",
            getattr(ctx, "project_id", ""),
        )


def _extract_trusted_egress_context(
    envelope: dict[str, Any],
) -> TrustedEgressContext | None:
    if type(envelope) is TrustedRunnerEnvelope:
        context = envelope.get(TRUSTED_EGRESS_CONTEXT_KEY)
        if type(context) is not TrustedEgressContext:
            raise InvalidTaskEnvelope() from None
        return context
    if type(envelope) is not dict or TRUSTED_EGRESS_CONTEXT_KEY in envelope:
        raise InvalidTaskEnvelope() from None
    return None


def run_indextts2_audio(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    return asyncio.run(
        await_envelope_with_cancel_watch(
            _run_indextts2_audio(envelope, ctx),
            envelope,
            task_type=INDEXTTS2_AUDIO_TASK_TYPE,
        )
    )


async def _run_indextts2_audio(
    envelope: dict[str, Any],
    ctx: ProjectContext,
) -> dict[str, Any] | None:
    from novelvideo.audio.indextts2_beat_audio_task import (
        run_indextts2_beat_audio_generation,
    )
    from novelvideo.sqlite_store import SQLiteStore

    payload = envelope.get("payload") or {}
    episode = int(envelope.get("episode") or payload.get("episode") or 0)
    mode = str(payload.get("mode") or "sync_changed")
    beat_numbers = payload.get("beat_numbers")
    if beat_numbers:
        beat_numbers = [int(value) for value in beat_numbers]
    manager = get_task_manager()

    store = SQLiteStore(
        ctx.owner_project_label,
        output_dir=str(ctx.output_dir),
        state_dir=str(ctx.state_dir),
    )
    await store.initialize()

    async def on_progress(done: int, total: int, current: str) -> None:
        manager.update_progress_for_project(
            ctx,
            INDEXTTS2_AUDIO_TASK_TYPE,
            episode,
            progress=(done / total) if total else 0.0,
            current_task=current,
            logs=[current],
        )

    def on_log(message: str) -> None:
        manager.update_progress_for_project(
            ctx,
            INDEXTTS2_AUDIO_TASK_TYPE,
            episode,
            logs=[message],
        )

    try:
        generation_kwargs: dict[str, Any] = {
            "store": store,
            "username": ctx.owner_username,
            "project": ctx.project_name,
            "episode": episode,
            "beat_numbers": beat_numbers,
            "mode": mode,
            "progress_callback": on_progress,
            "log_callback": on_log,
        }
        egress_context = _extract_trusted_egress_context(envelope)
        # 与 runners/freezone.py 的 _call_freezone_leaf 同理：只有组织任务携带受信任的
        # 组织出网身份走网关；平台与本地任务保持无组织上下文的直连语义，否则
        # IndexTTS2FalClient.generate 会把非组织身份判为 ORG_EGRESS_DENIED。
        if egress_context is not None and egress_context.is_organization:
            generation_kwargs["egress_context"] = egress_context
        result = await run_indextts2_beat_audio_generation(
            **generation_kwargs,
        )
        if result.generated == 0 and result.failed:
            raise RuntimeError(
                "音频生成失败：没有生成可用结果；"
                f"最后错误：{result.failed[-1]}"
            )
        skipped = (
            result.skipped_existing
            + result.skipped_empty
            + result.skipped_manual
            + result.skipped_silence
            + result.skipped_non_dialogue
        )
        return {
            "generated": result.generated,
            "total": result.total_targets,
            "skipped": skipped,
            "failed": len(result.failed),
            "generated_beats": list(result.generated_beats),
            "indextts2_detail": result.to_dict(),
        }
    finally:
        await store.close()


register_project_task_runner(INDEXTTS2_AUDIO_TASK_TYPE, run_indextts2_audio)
register_project_task_runner(SYSTEM_VOICE_SETUP_TASK_TYPE, run_system_voice_setup)
register_project_task_runner(EPISODE_AUDIO_DESIGN_TASK_TYPE, run_episode_audio_design)
