"""Celery runners for video-generation tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novelvideo.project_context import ProjectContext
from novelvideo.seedance2_i2v.spoken_dialogue import beat_subtitle_text
from novelvideo.task_backend.cancel import (
    TaskTimedOut,
    await_envelope_with_cancel_watch,
    raise_if_envelope_cancel_requested,
    remaining_timeout_seconds,
)
from novelvideo.task_backend.registry import register_project_task_runner
from novelvideo.task_backend.subprocesses import run_project_subprocess
from novelvideo.task_identity import project_task_state_key
from novelvideo.task_state import get_task_manager
from novelvideo.verification.subtitle_font import (
    SubtitleFont,
    SubtitleFontError,
    resolve_subtitle_font,
)

logger = logging.getLogger(__name__)


def _log(manager, ctx: ProjectContext, envelope: dict[str, Any], message: str) -> None:
    manager.update_progress_for_project(
        ctx,
        str(envelope["task_type"]),
        int(envelope.get("episode") or 0),
        beat_num=envelope.get("beat_num"),
        scope=envelope.get("scope"),
        current_task=message,
        logs=[message],
    )


def _resolve_video_aspect_ratio(value: object, frame_path: object) -> str:
    """Use the provider's adaptive mode when I2V should follow its first frame.

    Legacy Seedance 1.x tasks historically omitted ``ratio`` and were forced to
    9:16.  Seedance does not expose a fixed 2:3 output preset, but its I2V API
    supports ``adaptive`` specifically for following the supplied first frame.
    """
    requested = str(value or "").strip()
    if requested and requested.lower() != "auto":
        return requested
    return "adaptive" if str(frame_path or "").strip() else "9:16"


def _append_freezone_video_node_history(
    *,
    ctx: ProjectContext,
    project_dir: Path,
    payload: dict[str, Any],
    job_id: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any] | None:
    node_id = str(payload.get("node_id") or "").strip()
    if not node_id:
        return None

    from novelvideo.freezone.history import (
        append_generation_history,
        build_node_history_record,
    )

    extra: dict[str, Any] = {}
    if payload.get("model_id"):
        extra["model"] = str(payload["model_id"])
    history_mode = payload.get("requested_gen_mode") or payload.get("gen_mode")
    if history_mode:
        extra["gen_mode"] = str(history_mode)
    for key in ("generation_attempt_id", "product_operation_id"):
        if payload.get(key):
            extra[key] = str(payload[key])

    record = build_node_history_record(
        task_type="freezone_video_gen",
        job_id=job_id,
        task_key=project_task_state_key(
            "freezone_video_gen", ctx.project_id, 0, scope=job_id
        ),
        status="failed" if error else "completed",
        media_type="video",
        result=result,
        error=error,
        prompt=payload.get("prompt"),
        extra=extra or None,
    )

    return append_generation_history(
        project_dir=project_dir,
        canvas_id=str(payload.get("canvas_id") or "default"),
        node_id=node_id,
        record=record,
    )


def _beat_expects_speech(beat: dict[str, Any]) -> bool:
    """这一 beat 是否必须有真实人声（台词或解说）。

    规则本体在 ``seedance2_i2v.voice_preflight``（与参考声线预检同源，避免两处口径
    漂移）；这里只是给运行器留一个本地入口。
    """

    from novelvideo.seedance2_i2v.voice_preflight import beat_expects_speech

    return beat_expects_speech(beat)


def _speech_reference_audio_gap(
    *,
    beat: dict[str, Any],
    beat_num: int,
    references: list[Any],
    seedance2_config: Any,
) -> str:
    """说话 beat 却没有参考声线时返回错误文案（否则空串）。见 voice_preflight。"""

    from novelvideo.seedance2_i2v.voice_preflight import (
        speech_reference_audio_gap,
    )

    return speech_reference_audio_gap(
        beat=beat,
        beat_num=beat_num,
        references=references,
        seedance2_config=seedance2_config,
    )


def _beat_identity_reference_paths(
    output_dir: str, beat: dict[str, Any]
) -> list[str]:
    """Resolve the beat's identity reference images (best effort)."""

    import json as _json

    from novelvideo.models import real_detected_identities

    raw = beat.get("detected_identities")
    if raw is None:
        raw = beat.get("detected_identities_json") or []
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except (TypeError, ValueError):
            raw = []
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, (list, tuple)):
        raw = []

    paths: list[str] = []
    chars_root = Path(output_dir) / "assets" / "characters"
    for identity_id in real_detected_identities(list(raw)):
        character, _, identity = str(identity_id).partition("_")
        candidate = (
            chars_root / character / "identities" / f"{identity}.png"
            if identity
            else chars_root / character / "portrait.png"
        )
        if candidate.exists():
            paths.append(candidate.as_posix())
    return paths


async def _apply_video_consistency_gate(
    *,
    video_gen: Any,
    generate_kwargs: dict[str, Any],
    result: Any,
    frame_path: Any,
    video_path: Path,
    beat: dict[str, Any],
    output_dir: str,
    payload_config: dict[str, Any] | None = None,
    on_log,
    strict_first_frame_degraded: bool = False,
    beat_num: int = 0,
) -> tuple[Any, dict[str, Any]]:
    """生成后一致性自检 + 有界重跑；返回（最终结果, 报告）。

    每次重跑都是一版新画面，所以先把上一版留在临时目录里：重跑次数用尽时选「最接近
    首帧」的那一版落回正式路径（而不是盲留最后一版），并把需要人工复核的原因写进
    报告与 ``videos/beats/epNNN/review_flags.json``。
    """

    from novelvideo.verification.video_consistency_gate import (
        VideoConsistencyGate,
        consistency_gate_config,
        frame_aspect_warning,
        pick_best_attempt,
    )

    settings = consistency_gate_config(config=payload_config)
    if not settings.enabled or not str(frame_path or "").strip():
        return result, {"enabled": False}

    gate = VideoConsistencyGate()
    identity_paths = _beat_identity_reference_paths(output_dir, beat)
    attempts: list[dict[str, Any]] = []
    current = result
    notes: list[str] = []

    aspect_warning = frame_aspect_warning(
        Path(str(frame_path)), str((payload_config or {}).get("ratio") or "")
    )
    if aspect_warning:
        on_log(f"画幅告警：{aspect_warning}")
        notes.append(aspect_warning)

    work_dir = Path(tempfile.mkdtemp(prefix="video_gate_"))
    chosen: Path | None = None
    chosen_attempt: int | None = None
    try:
        for attempt in range(1, settings.max_attempts + 1):
            snapshot = work_dir / f"attempt_{attempt}{video_path.suffix}"
            try:
                shutil.copy2(video_path, snapshot)
            except OSError:
                snapshot = Path(video_path)
            verdict = await gate.check(
                video_path=snapshot,
                first_frame_path=Path(str(frame_path)),
                identity_image_paths=identity_paths,
                audio_type=str(beat.get("audio_type") or ""),
                expects_speech=_beat_expects_speech(beat),
                log=on_log,
            )
            attempts.append(
                {
                    "attempt": attempt,
                    **verdict.as_dict(),
                    "video_path": str(snapshot),
                }
            )
            if verdict.passed:
                if attempt > 1:
                    on_log(f"一致性自检通过（第 {attempt} 次生成）")
                chosen = snapshot
                chosen_attempt = attempt
                break
            summary = "；".join(verdict.reasons[:3]) or "画面与首帧不一致"
            if not verdict.should_rerun:
                # 景别/构图这类偏差只标记人工复核，不重跑：重跑一轮要花一次完整
                # 生成，而画面本身（身份、服装、道具、声音）并没有坏。
                chosen = snapshot
                chosen_attempt = attempt
                on_log(
                    "一致性自检只发现景别/构图类偏差，按「标记人工复核、不重跑」处理："
                    f"{summary}"
                )
                break
            if attempt >= settings.max_attempts:
                # 有界重跑用尽：不阻断产线，但要选最接近首帧的一版并显式标记人工复核。
                best = pick_best_attempt(attempts) or {}
                candidate = Path(str(best.get("video_path") or snapshot))
                if candidate != snapshot:
                    on_log(f"重跑次数用尽，改用最接近首帧的第 {best.get('attempt')} 版成片")
                chosen = candidate
                chosen_attempt = int(best.get("attempt") or attempt)
                on_log(f"一致性自检未通过且重跑次数用尽，已标记人工复核：{summary}")
                break
            on_log(f"一致性自检未通过，强化提示词后重跑（第 {attempt + 1} 次生成）：{summary}")
            generate_kwargs = dict(generate_kwargs)
            generate_kwargs["prompt"] = verdict.reinforce_prompt(
                str(generate_kwargs.get("prompt") or "")
            )
            current = await video_gen.generate(**generate_kwargs)
            if current.status.value != "done":
                raise RuntimeError(current.error or "视频生成失败")

        if chosen is not None and chosen != Path(video_path):
            shutil.copy2(chosen, video_path)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # 报告必须描述「真正落盘的那一版」：重跑用尽时它可能不是最后一次生成的那版。
    final = next(
        (item for item in attempts if item.get("attempt") == chosen_attempt),
        attempts[-1] if attempts else {},
    )
    review_reasons = [
        str(reason) for reason in (final.get("reasons") or []) if str(reason).strip()
    ]
    if aspect_warning:
        review_reasons.append(aspect_warning)
    report: dict[str, Any] = {
        "enabled": True,
        "attempts": attempts,
        "notes": notes,
        "final_attempt": final.get("attempt"),
        "frame0_similarity": final.get("frame0_similarity"),
        "passed": bool(final.get("passed")),
        "strict_first_frame_degraded": bool(strict_first_frame_degraded),
        "needs_human_review": bool(review_reasons),
        "review_reasons": review_reasons,
        # 按维度拆开记：重跑的代价是「多生成一次」，所以哪一类问题触发了重跑要能
        # 直接被统计出来（景别类现在只标记，不再计入 generated_attempts 的增长）。
        "rerun_reasons": list(final.get("rerun_reasons") or []),
        "marked_reasons": list(final.get("marked_reasons") or []),
        "generations": len(attempts),
    }
    if strict_first_frame_degraded:
        on_log(
            "严格首帧未生效：上游拒绝了硬首帧通路，已降级为多参考通路"
            "（首帧只作参考图1，不再硬锁）"
        )
    write_review_flags(
        videos_dir=Path(video_path).parent,
        beat_num=int(beat_num or beat.get("beat_number") or 0),
        report=report,
    )
    return current, report


def write_review_flags(*, videos_dir: Path, beat_num: int, report: dict[str, Any]) -> None:
    """把需要人工复核的结论累积到 ``videos/beats/epNNN/review_flags.json``。

    不新增 beats 表字段：复核标记是生成过程的副产物，跟着产物走即可。没有需要复核的
    条目时该文件不存在，避免给正常集引入噪声。
    """

    flags_path = Path(videos_dir) / "review_flags.json"
    existing: dict[str, Any] = {}
    if flags_path.exists():
        try:
            loaded = json.loads(flags_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            existing = {}
    beats = existing.get("beats")
    flags = dict(beats) if isinstance(beats, dict) else {}

    entry: dict[str, Any] = {
        "beat_number": int(beat_num or 0),
        "reasons": list(report.get("review_reasons") or []),
        "final_attempt": report.get("final_attempt"),
        "frame0_similarity": report.get("frame0_similarity"),
        "strict_first_frame_degraded": bool(report.get("strict_first_frame_degraded")),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    needs_review = bool(report.get("needs_human_review")) or bool(
        report.get("strict_first_frame_degraded")
    )
    if needs_review:
        flags[str(int(beat_num or 0))] = entry
    else:
        flags.pop(str(int(beat_num or 0)), None)

    if not flags:
        try:
            flags_path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    payload = {"schema": "beat_review_flags_v1", "beats": flags}
    try:
        flags_path.parent.mkdir(parents=True, exist_ok=True)
        flags_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        logger.warning("failed to write review flags %s", flags_path, exc_info=True)


async def _run_single_video_async(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    task_type = "single_video"
    episode = int(envelope.get("episode") or 0)
    beat_num = int(envelope.get("beat_num") or 0)
    payload = envelope.get("payload") or {}
    config = dict(payload.get("config") or {})
    output_dir = str(payload.get("output_dir") or ctx.output_dir)

    manager = get_task_manager()
    _log(manager, ctx, envelope, f"开始生成 Beat {beat_num} 视频")

    from novelvideo.generators.video_generator import (
        FALLBACK_MULTI_REFERENCE_LOG,
        ShotReference,
        create_video_generator,
    )
    from novelvideo.seedance2_i2v.pipeline import is_huimeng_seedance2_backend
    from novelvideo.utils.path_resolver import PathResolver

    beat = config.get("beat", {})
    frame_path = config.get("frame_path")
    video_mode = config.get("video_mode", "first_frame")
    prompt = config.get("prompt", "")
    video_duration = config.get("video_duration", 5.0)
    backend_str = config.get("video_backend", "comfyui")
    last_frame_path = config.get("last_frame_path")
    seedance2_config = config.get("seedance2_config") or beat.get(
        "seedance2_config_json"
    )
    is_seedance2_backend = is_huimeng_seedance2_backend(backend_str)

    paths = PathResolver(output_dir, episode)
    videos_dir = paths.videos_dir()
    videos_dir.mkdir(parents=True, exist_ok=True)
    video_path = paths.video(beat_num)
    gen_kwargs: dict[str, Any] = {}
    egress_context = envelope.get("__trusted_egress_context")
    if egress_context is not None:
        gen_kwargs["egress_context"] = egress_context
    # 非 seedance2 后端（含 seedance-1.5-pro）的清晰度走构造参数透传；
    # seedance2 的清晰度在 prepare 阶段并入 seedance2_config，无需在此重复。
    single_resolution = config.get("resolution")
    if single_resolution and not is_seedance2_backend:
        gen_kwargs["resolution"] = str(single_resolution)
    video_gen = create_video_generator(backend=backend_str, **gen_kwargs)

    # 严格首帧通路会被上游内容审核拒绝（写实人脸），提交期会静默降级为多参考通路。
    # 这里捕获降级标记，让自检报告如实反映「硬首帧实际未生效」。
    strict_first_frame_degraded = {"value": False}

    def on_log(msg: str) -> None:
        if FALLBACK_MULTI_REFERENCE_LOG in str(msg):
            strict_first_frame_degraded["value"] = True
        _log(manager, ctx, envelope, msg)

    def on_progress(value: float) -> None:
        manager.update_progress_for_project(
            ctx,
            task_type,
            episode,
            beat_num=beat_num,
            progress=value,
            current_task=f"生成 Beat {beat_num} 视频",
        )

    if video_mode == "keyframe" and last_frame_path and not is_seedance2_backend:
        video_duration = 5.0

    seedance2_references = []
    if is_seedance2_backend:
        from novelvideo.seedance2_i2v.models import Seedance2I2VMode
        from novelvideo.seedance2_i2v.pipeline import (
            prepare_seedance2_generation_inputs,
        )

        prepared = await prepare_seedance2_generation_inputs(
            project_output=output_dir,
            state_dir=ctx.state_dir,
            episode=episode,
            beat={**beat, "seedance2_config_json": seedance2_config or "{}"},
            next_beat=config.get("next_beat"),
            video_mode=video_mode,
            prompt=prompt,
            duration=video_duration,
            resolution=(
                str(config["resolution"])
                if config.get("resolution") is not None
                else None
            ),
            ratio=str(config["ratio"]) if config.get("ratio") is not None else None,
            prop_menu=config.get("prop_menu"),
        )
        prompt = prepared.prompt
        video_duration = prepared.duration
        frame_path = prepared.image_path
        last_frame_path = prepared.last_frame_path
        seedance2_config = prepared.seedance2_config_json
        seedance2_references = prepared.references
        video_mode = (
            "keyframe"
            if prepared.mode == Seedance2I2VMode.FIRST_LAST_FRAME
            else "first_frame"
        )

    model_references = seedance2_references
    if not is_seedance2_backend:
        model_references = [
            ShotReference(
                str(item.get("type") or "image"),
                str(item.get("path") or ""),
                str(item.get("role") or ""),
            )
            for item in config.get("references") or []
            if isinstance(item, dict) and str(item.get("path") or "").strip()
        ]

    generate_kwargs = {
        "image_path": frame_path,
        "prompt": prompt,
        "output_path": video_path.as_posix(),
        "aspect_ratio": _resolve_video_aspect_ratio(config.get("ratio"), frame_path),
        "duration": video_duration,
        "on_log": on_log,
        "on_progress": on_progress,
        "last_frame_path": last_frame_path,
        "project_output_dir": output_dir,
        "episode": episode,
        "beat_num": beat_num,
        "task_type": task_type,
        "scope": str(envelope.get("scope") or "beat"),
    }
    if egress_context is not None:
        generate_kwargs["egress_context"] = egress_context
    if model_references:
        generate_kwargs["references"] = model_references
    if config.get("audio_setting"):
        generate_kwargs["audio_setting"] = str(config["audio_setting"])
    if is_seedance2_backend:
        generate_kwargs["seedance2_config"] = seedance2_config
        voice_gap = _speech_reference_audio_gap(
            beat=beat,
            beat_num=beat_num,
            references=model_references,
            seedance2_config=seedance2_config,
        )
        if voice_gap:
            _log(manager, ctx, envelope, f"参考声线预检未通过：{voice_gap}")
            raise RuntimeError(voice_gap)

    result = await video_gen.generate(**generate_kwargs)
    if result.status.value != "done":
        raise RuntimeError(result.error or "视频生成失败")

    gate_report: dict[str, Any] = {"enabled": False}
    if is_seedance2_backend:
        # 生成后自检：严格首帧 / 服装脸型 / 肢体物理，不通过时有界重跑。
        result, gate_report = await _apply_video_consistency_gate(
            video_gen=video_gen,
            generate_kwargs=generate_kwargs,
            result=result,
            frame_path=frame_path,
            video_path=video_path,
            beat=beat,
            output_dir=output_dir,
            payload_config=config,
            on_log=on_log,
            strict_first_frame_degraded=strict_first_frame_degraded["value"],
            beat_num=beat_num,
        )
    elif strict_first_frame_degraded["value"]:
        # 自检关闭时不跑画面复核，但「严格首帧未生效」仍要落盘，不能静默。
        write_review_flags(
            videos_dir=videos_dir,
            beat_num=beat_num,
            report={
                "review_reasons": [
                    "严格首帧未生效：上游拒绝了硬首帧通路，已降级为多参考通路"
                ],
                "strict_first_frame_degraded": True,
            },
        )

    video_pool_id = None
    try:
        from novelvideo.generators.video_pool_indexer import add_video_to_pool

        entry = add_video_to_pool(
            videos_ep_dir=videos_dir,
            episode=episode,
            beat_num=beat_num,
            source_video_path=Path(video_path),
            duration=video_duration,
            video_mode=video_mode,
            backend=backend_str,
            prompt=prompt,
        )
        video_pool_id = entry.id
    except Exception as exc:  # noqa: BLE001
        on_log(f"添加到视频池失败 (非致命): {exc}")

    task_result = {
        "video_path": video_path.as_posix(),
        "beat_num": beat_num,
        "video_pool_id": video_pool_id,
    }
    if gate_report.get("enabled"):
        task_result["consistency_gate"] = gate_report
    if strict_first_frame_degraded["value"]:
        # 自检关掉时也要如实暴露这次降级，避免「严格首帧」看起来生效了。
        task_result["strict_first_frame_degraded"] = True
    provider_task_id = getattr(result, "provider_task_id", None) or getattr(
        result, "task_id", None
    )
    if provider_task_id:
        task_result["provider_task_id"] = provider_task_id
    if result.last_frame_path:
        task_result["last_frame_path"] = result.last_frame_path
    if result.last_frame_url:
        task_result["last_frame_url"] = result.last_frame_url
    return task_result


def run_single_video(envelope: dict[str, Any], ctx: ProjectContext) -> dict[str, Any]:
    return asyncio.run(
        await_envelope_with_cancel_watch(
            _run_single_video_async(envelope, ctx),
            envelope,
            task_type="single_video",
        )
    )


register_project_task_runner("single_video", run_single_video)


def _audio_duration(
    audio_path: Path, *, timeout_seconds: int | None = 30
) -> float | None:
    if not audio_path.exists():
        return None
    import subprocess

    try:
        result = run_project_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TaskTimedOut(timeout_seconds=timeout_seconds) from exc
    try:
        return float(result.stdout.strip())
    except Exception:
        return None


def _video_has_audio_stream(
    video_path: Path, *, timeout_seconds: int | None = 30
) -> bool:
    if not video_path.exists():
        return False
    import subprocess

    try:
        result = run_project_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TaskTimedOut(timeout_seconds=timeout_seconds) from exc
    return result.returncode == 0 and bool(result.stdout.strip())


_COMPOSE_CLIP_ENCODER_ARGS = [
    "-c:v",
    "libx264",
    "-preset",
    "fast",
    "-crf",
    "23",
    "-c:a",
    "aac",
    "-b:a",
    "128k",
    "-pix_fmt",
    "yuv420p",
]


def _beat_prefers_embedded_audio(beat: dict[str, Any]) -> bool:
    """True when a beat's own clip already carries the performed dialogue.

    Seedance 2.0 beats generated with ``generate_audio`` speak the line
    natively, so the separate dubbing track is only a timing/preview artifact
    and must not overwrite the model's own performance in the final cut.
    """

    raw = str(beat.get("seedance2_config_json") or "").strip()
    if not raw or raw in {"{}", "null", "None"}:
        return False
    try:
        from novelvideo.seedance2_i2v.models import parse_seedance2_config

        config = parse_seedance2_config(raw)
    except Exception:
        return False
    return bool(config.generate_audio)


def _beat_dub_alignment_args(
    video_path: Path,
    audio_path: Path,
    *,
    timeout_seconds: int | None,
) -> list[str]:
    """Map the beat's dubbing track and align both sides by padding.

    The old ``-shortest`` cut whichever side was longer: a 5s shot carrying a
    6.7s line lost the end of the dialogue, and a 9.4s line over a 5s shot lost
    the picture. Pad the shorter side instead (clone the last frame / pad with
    silence) so a beat keeps both its full picture and its full line.
    """

    video_seconds = _audio_duration(video_path, timeout_seconds=timeout_seconds) or 0.0
    dub_seconds = _audio_duration(audio_path, timeout_seconds=timeout_seconds) or 0.0
    target_seconds = max(video_seconds, dub_seconds)
    if target_seconds <= 0:
        # Both probes failed: keep the previous truncating behaviour rather than
        # risk an unbounded clip (apad alone would never end).
        return [
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            *_COMPOSE_CLIP_ENCODER_ARGS,
            "-shortest",
        ]
    pad_seconds = max(target_seconds - video_seconds, 0.0)
    return [
        "-filter_complex",
        f"[0:v]tpad=stop_mode=clone:stop_duration={pad_seconds:.3f}[v];[1:a]apad[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        *_COMPOSE_CLIP_ENCODER_ARGS,
        "-t",
        f"{target_seconds:.3f}",
    ]


async def _run_video_generation_async(
    envelope: dict[str, Any],
    ctx: ProjectContext,
) -> dict[str, Any]:
    from novelvideo.manual_shots import resolve_target_video_duration
    from novelvideo.utils.path_resolver import PathResolver

    payload = envelope.get("payload") or {}
    episode = int(envelope.get("episode") or payload.get("episode") or 0)
    output_dir = str(payload.get("output_dir") or ctx.output_dir)
    beats = list(payload.get("beats") or [])
    requested_beat_numbers = {
        int(value) for value in payload.get("beat_numbers") or [] if int(value) > 0
    }
    if requested_beat_numbers:
        beats = [
            beat
            for beat in beats
            if int(beat.get("beat_number") or 0) in requested_beat_numbers
        ]
    video_backend = str(payload.get("video_backend") or "mock")
    resolution = str(payload.get("resolution") or "720p")
    ratio = str(payload.get("ratio") or "9:16")
    prop_menu = payload.get("prop_menu")
    use_director_render = bool(payload.get("use_director_render"))
    manager = get_task_manager()
    paths = PathResolver(output_dir, episode)
    generated: list[dict[str, Any]] = []

    for index, beat in enumerate(beats):
        beat_num = int(beat.get("beat_number") or index + 1)
        manager.update_progress_for_project(
            ctx,
            "video_generation",
            episode,
            progress=index / max(1, len(beats)),
            current_task=f"生成 Beat {beat_num} 视频...",
        )
        frame_path = paths.first_frame_for_video(
            beat_num,
            use_director_render=use_director_render,
        )
        if not frame_path.exists():
            manager.update_progress_for_project(
                ctx,
                "video_generation",
                episode,
                logs=[f"Beat {beat_num} 缺少首帧，跳过: {frame_path}"],
            )
            continue

        video_mode = str(beat.get("video_mode") or "first_frame")
        prompt = str(
            beat.get("keyframe_prompt")
            if video_mode == "keyframe"
            else beat.get("video_prompt") or ""
        )
        audio_path = paths.audio(beat_num)
        duration = resolve_target_video_duration(
            beat,
            _audio_duration(
                audio_path,
                timeout_seconds=remaining_timeout_seconds(envelope, default_seconds=30),
            ),
        )
        last_frame_path = None
        if video_mode == "keyframe":
            next_frame = paths.first_frame_for_video(
                beat_num + 1,
                use_director_render=use_director_render,
            )
            if next_frame.exists():
                last_frame_path = str(next_frame)
            else:
                video_mode = "first_frame"

        single_envelope = {
            "task_type": "single_video",
            "episode": episode,
            "beat_num": beat_num,
            "payload": {
                "output_dir": output_dir,
                "config": {
                    "beat": beat,
                    "next_beat": beats[index + 1] if index + 1 < len(beats) else None,
                    "frame_path": str(frame_path),
                    "video_mode": video_mode,
                    "prompt": prompt,
                    "video_duration": duration,
                    "video_backend": video_backend,
                    "last_frame_path": last_frame_path,
                    "resolution": resolution,
                    "ratio": ratio,
                    "prop_menu": prop_menu,
                },
            },
        }
        generated.append(await _run_single_video_async(single_envelope, ctx))

    return {"generated": len(generated), "items": generated}


def run_video_generation(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    return asyncio.run(
        await_envelope_with_cancel_watch(
            _run_video_generation_async(envelope, ctx),
            envelope,
            task_type="video_generation",
        )
    )


def _ffprobe_duration(path: Path) -> float:
    import subprocess

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
    except Exception:
        return 0.0


def _srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis > 999:
        millis = 999
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


#: libass 覆盖标签：``\an2`` 即"整块底部居中"，折成多行时每一行都由 libass
#: 各自居中摆放（与 ``force_style`` 的 ``Alignment=2`` 同一语义）。每条 cue 都
#: 自带这个标签，这样就算 ``force_style`` 将来被丢掉或被改坏（例如字体族名里
#: 意外带上逗号导致样式串解析失败），字幕也不会退化成左对齐。
SUBTITLE_ALIGNMENT_TAG = r"{\an2}"


def _build_srt_from_clips(entries: list[tuple[str, str]]) -> str:
    """Build an SRT aligned to the composed clip timeline.

    ``entries`` is an ordered list of ``(narration_text, clip_path)``. Every
    clip advances the timeline (silent beats included) so subtitle timings match
    the concatenated film; only clips with text emit a cue.

    每条 cue 的正文前置 :data:`SUBTITLE_ALIGNMENT_TAG`（底部居中，逐行居中），
    保证"居中"既写在样式里也写在字幕本身里。该 SRT 只用于本函数调用方的临时
    烧录用文件，不交付用户，所以这些标签不会污染导出的 ``.srt``。
    """
    lines: list[str] = []
    cursor = 0.0
    seq = 0
    for text, clip in entries:
        duration = _ffprobe_duration(Path(clip)) or 1.0
        start, end = cursor, cursor + duration
        cursor = end
        if text:
            seq += 1
            lines.append(str(seq))
            lines.append(f"{_srt_time(start)} --> {_srt_time(end)}")
            lines.append(f"{SUBTITLE_ALIGNMENT_TAG}{text}")
            lines.append("")
    return "\n".join(lines)


#: 字幕样式里与字体无关的部分；libass 按恒定 PlayRes 缩放，FontSize 与影片
#: 分辨率无关，11 在 1280 高的成片上约 49px（3.8% 画面高），是电影式底带
#: 而不是铺满画面的字块。
#:
#: ``Alignment=2`` = 整块底部居中，libass 对多行文本会把每一行也各自居中
#: （已用同款 force_style 烧录两行 cue 验证：Alignment=2 两行都居中，
#: Alignment=1 才会左对齐）。``MarginL``/``MarginR`` 显式取和 libass 默认
#: 相同的 10，既保证左右边距一定对称（多行自动折行时左右留白相等），也不改
#: 变既有成片的折行位置与像素。
def _subtitle_force_style(font: SubtitleFont | None) -> str:
    """Build the libass ``force_style`` for the resolved subtitle font.

    字体名必须来自 ``resolve_subtitle_font``：镜像里没有对应字体时 libass 会
    静默回退到无中文字形的字体，把整片字幕烧成豆腐块。
    """

    family = font.family if font is not None else "Noto Sans CJK SC"
    return (
        f"FontName={family},"
        "FontSize=11,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=1,Shadow=0,"
        "MarginL=10,MarginR=10,MarginV=24,Alignment=2"
    )


def _resolve_compose_subtitle_font(
    beats: list[dict[str, Any]],
    *,
    manager,
    ctx: ProjectContext,
    episode: int,
) -> SubtitleFont | None:
    """Resolve (and self-check) the subtitle font before the burn pass runs.

    自检只验证"字体是否画得出本期实际字幕文本"，所以必须在逐 clip 归一化之前
    跑：等烧完再发现缺字形，几分钟的编码就白做了。
    """

    planned_text = "\n".join(beat_subtitle_text(beat) for beat in beats)
    if not planned_text.strip():
        return None
    try:
        font = resolve_subtitle_font(planned_text)
    except SubtitleFontError as exc:
        manager.update_progress_for_project(
            ctx,
            "compose_episode",
            episode,
            logs=[f"字幕字体自检失败: {exc}"],
        )
        raise
    if font is None:
        manager.update_progress_for_project(
            ctx,
            "compose_episode",
            episode,
            logs=["未检测到 fontconfig，跳过字幕字体自检"],
        )
        return None
    message = f"字幕字体自检通过: {font.family} ({font.file})"
    if font.missing:
        message += f"；以下字符缺字形: {''.join(font.missing[:20])}"
    manager.update_progress_for_project(
        ctx,
        "compose_episode",
        episode,
        logs=[message],
    )
    return font


def run_compose_episode(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    import subprocess
    import tempfile

    from novelvideo.utils.path_resolver import PathResolver

    payload = envelope.get("payload") or {}
    episode = int(envelope.get("episode") or payload.get("episode") or 0)
    output_dir = str(payload.get("output_dir") or ctx.output_dir)
    beats = list(payload.get("beats") or [])
    resolution = str(payload.get("resolution") or "720x1280")
    add_subtitles = bool(payload.get("add_subtitles"))
    manager = get_task_manager()
    paths = PathResolver(output_dir, episode)
    final_dir = Path(output_dir) / "videos" / "episodes"
    final_dir.mkdir(parents=True, exist_ok=True)
    output_path = final_dir / f"ep{episode:03d}_final.mp4"

    def check_cancel() -> None:
        raise_if_envelope_cancel_requested(
            envelope,
            task_type="compose_episode",
            episode=episode,
        )

    def subprocess_timeout(default_seconds: int) -> int | None:
        return remaining_timeout_seconds(envelope, default_seconds=default_seconds)

    def run_checked(cmd: list[str], *, default_timeout_seconds: int):
        try:
            return run_project_subprocess(
                cmd,
                envelope=envelope,
                capture_output=True,
                text=True,
                timeout=subprocess_timeout(default_timeout_seconds),
            )
        except subprocess.TimeoutExpired as exc:
            raise TaskTimedOut(
                timeout_seconds=int(envelope.get("__timeout_seconds") or 30 * 60)
            ) from exc

    try:
        target_width, target_height = map(int, resolution.split("x"))
    except Exception:
        target_width, target_height = 720, 1280

    # 字幕字体自检必须在逐 clip 归一化之前：缺字形时立刻失败，别等几分钟编码完
    # 才发现整片字幕是豆腐块。
    subtitle_font: SubtitleFont | None = None
    if add_subtitles:
        subtitle_font = _resolve_compose_subtitle_font(
            beats,
            manager=manager,
            ctx=ctx,
            episode=episode,
        )

    video_clips: list[str] = []
    clip_srt_entries: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for index, beat in enumerate(beats):
            check_cancel()
            beat_num = int(beat.get("beat_number") or index + 1)
            video_path = paths.video(beat_num)
            audio_path = paths.audio(beat_num)
            if not video_path.exists():
                continue
            clip_path = tmp_dir / f"beat_{beat_num:04d}.mp4"
            manager.update_progress_for_project(
                ctx,
                "compose_episode",
                episode,
                progress=index / max(1, len(beats)),
                current_task=f"合成 Beat {beat_num}...",
            )
            cmd = ["ffmpeg", "-y", "-i", str(video_path)]
            has_embedded_audio = _video_has_audio_stream(
                video_path,
                timeout_seconds=subprocess_timeout(30),
            )
            check_cancel()
            keep_native_audio = has_embedded_audio and _beat_prefers_embedded_audio(
                beat
            )
            if audio_path.exists() and not keep_native_audio:
                cmd.extend(["-i", str(audio_path)])
                cmd.extend(
                    _beat_dub_alignment_args(
                        video_path,
                        audio_path,
                        timeout_seconds=subprocess_timeout(30),
                    )
                )
                manager.update_progress_for_project(
                    ctx,
                    "compose_episode",
                    episode,
                    logs=[f"Beat {beat_num} 使用独立音频: {audio_path.name}"],
                )
            elif has_embedded_audio:
                cmd.extend(
                    [
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0",
                        *_COMPOSE_CLIP_ENCODER_ARGS,
                    ]
                )
                manager.update_progress_for_project(
                    ctx,
                    "compose_episode",
                    episode,
                    logs=[f"Beat {beat_num} 使用视频内置音轨"],
                )
            else:
                cmd.extend(
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=r=44100:cl=stereo",
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        *_COMPOSE_CLIP_ENCODER_ARGS,
                        "-shortest",
                    ]
                )
            cmd.append(str(clip_path))
            result = run_checked(cmd, default_timeout_seconds=30 * 60)
            check_cancel()
            if result.returncode == 0:
                video_clips.append(str(clip_path))
                clip_srt_entries.append((beat_subtitle_text(beat), str(clip_path)))
            else:
                manager.update_progress_for_project(
                    ctx,
                    "compose_episode",
                    episode,
                    logs=[f"Beat {beat_num} 合成失败: {result.stderr[:500]}"],
                )

        if not video_clips:
            raise RuntimeError("没有可用的视频片段")

        check_cancel()
        subtitle_path: Path | None = None
        if add_subtitles:
            srt_content = _build_srt_from_clips(clip_srt_entries)
            if srt_content.strip():
                subtitle_path = tmp_dir / "ep_subtitles.srt"
                subtitle_path.write_text(srt_content, encoding="utf-8")
                manager.update_progress_for_project(
                    ctx,
                    "compose_episode",
                    episode,
                    logs=[f"已生成字幕 {subtitle_path.name}"],
                )
        cmd = ["ffmpeg", "-y"]
        for clip in video_clips:
            cmd.extend(["-i", clip])
        filter_parts = []
        for index in range(len(video_clips)):
            filter_parts.append(
                f"[{index}:v]scale={target_width}:{target_height}:"
                f"force_original_aspect_ratio=decrease,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black,"
                f"setsar=1,format=yuv420p[v{index}]"
            )
            filter_parts.append(f"[{index}:a]aresample=44100[a{index}]")
        concat_inputs = "".join(
            f"[v{index}][a{index}]" for index in range(len(video_clips))
        )
        filter_parts.append(
            f"{concat_inputs}concat=n={len(video_clips)}:v=1:a=1[outv][outa]"
        )
        video_map = "[outv]"
        if subtitle_path is not None:
            # libass renders SRT at a constant default PlayRes (~384x288) and
            # then scales to the film, so FontSize is resolution-independent:
            # 11 lands around 49px on a 1280-high film (3.8% of the height) with
            # a modest bottom margin, i.e. a movie-style band instead of text
            # that fills the frame.
            subtitle_style = _subtitle_force_style(subtitle_font)
            filter_parts.append(
                f"[outv]subtitles=filename='{subtitle_path}':"
                f"force_style='{subtitle_style}'[vsub]"
            )
            video_map = "[vsub]"
        cmd.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                video_map,
                "-map",
                "[outa]",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(output_path),
            ]
        )
        result = run_checked(cmd, default_timeout_seconds=30 * 60)
        check_cancel()
        if result.returncode != 0:
            raise RuntimeError(f"拼接失败: {result.stderr[:500]}")

    return {
        "video_path": output_path.as_posix(),
        "add_subtitles_requested": add_subtitles,
    }


register_project_task_runner("compose_episode", run_compose_episode)


async def _run_global_optimize_video_async(
    envelope: dict[str, Any],
    ctx: ProjectContext,
) -> dict[str, Any]:
    import os

    from novelvideo.agents.global_video_optimizer import (
        get_global_video_optimizer,
        prepare_global_optimizer_input,
    )
    from novelvideo.cognee import CogneeStore
    from novelvideo.utils.path_resolver import PathResolver

    payload = envelope.get("payload") or {}
    episode = int(envelope.get("episode") or payload.get("episode") or 0)
    beats = list(payload.get("beats") or [])
    characters = list(payload.get("characters") or [])
    output_dir = str(payload.get("output_dir") or ctx.output_dir)
    language = str(payload.get("language") or "zh")
    manager = get_task_manager()

    def log(message: str, *, progress: float | None = None) -> None:
        manager.update_progress_for_project(
            ctx,
            "global_optimize_video",
            episode,
            progress=progress,
            current_task=message,
            logs=[message],
        )

    log("开始全局视频提示词优化（仅 first_frame）...", progress=0.02)
    store = CogneeStore(
        ctx.owner_project_label,
        output_dir=output_dir,
        state_dir=str(ctx.state_dir),
    )
    try:
        await store.initialize()
        await store.load_graph_state()

        sketch_paths, color_map, _total_beats = prepare_global_optimizer_input(
            beats=beats,
            characters=characters,
            output_dir=output_dir,
            episode=episode,
            project=ctx.project_name,
        )
        if not sketch_paths:
            raise RuntimeError("找不到草图网格，请先生成草图")

        resolver = PathResolver(output_dir, episode)
        sketches_dir = str(resolver.sketches_dir())
        optimizer = get_global_video_optimizer()
        sorted_beats = sorted(beats, key=lambda beat: beat.get("beat_number", 0))
        updated_count = 0
        failure_messages: list[str] = []
        prev_prompt = None

        for index, beat in enumerate(sorted_beats):
            beat_num = int(beat.get("beat_number") or 0)
            log(
                f"Beat {beat_num}/{len(sorted_beats)}: 生成视频提示词...",
                progress=0.2 + 0.7 * index / max(1, len(sorted_beats)),
            )
            sketch_path = None
            for ext in ("png", "jpg"):
                candidate = os.path.join(sketches_dir, f"beat_{beat_num:02d}.{ext}")
                if os.path.exists(candidate):
                    sketch_path = candidate
                    break
            if not sketch_path:
                log(f"Beat {beat_num}: 无草图帧，跳过")
                continue

            prev_beat = sorted_beats[index - 1] if index > 0 else None
            next_beat = (
                sorted_beats[index + 1] if index < len(sorted_beats) - 1 else None
            )
            try:
                result = await optimizer.optimize_single_beat(
                    beat=beat,
                    sketch_image_path=sketch_path,
                    character_color_map=color_map,
                    language=language,
                    prev_beat=prev_beat,
                    next_beat=next_beat,
                    prev_prompt=prev_prompt,
                    total_beats=len(sorted_beats),
                )
                prompt = result["prompt"]
                beat["video_mode"] = "first_frame"
                beat["video_prompt"] = prompt
                beat["keyframe_prompt"] = None
                await store.update_beat_asset(
                    episode_number=episode,
                    beat_number=beat_num,
                    video_mode="first_frame",
                    video_prompt=prompt,
                    keyframe_prompt=None,
                )
                updated_count += 1
                prev_prompt = prompt
            except Exception as exc:  # noqa: BLE001
                failure_messages.append(f"Beat {beat_num}: {exc}")
                log(f"Beat {beat_num}: 生成失败 ({exc})")

        if updated_count == 0:
            error = f"全局优化失败：0/{len(sorted_beats)} 个 Beat 生成成功"
            if failure_messages:
                error = f"{error}；最后错误：{failure_messages[-1]}"
            raise RuntimeError(error)

        log(
            f"全局优化完成：成功更新 {updated_count}/{len(sorted_beats)} 个 Beat",
            progress=1.0,
        )
        return {"optimized": updated_count, "beats": beats}
    finally:
        await store.close()


def run_global_optimize_video(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    return asyncio.run(
        await_envelope_with_cancel_watch(
            _run_global_optimize_video_async(envelope, ctx),
            envelope,
            task_type="global_optimize_video",
        )
    )


register_project_task_runner("global_optimize_video", run_global_optimize_video)


def _omni_video_edit_hint(payload: dict[str, Any], error: Exception) -> str | None:
    mode = str(payload.get("gen_mode") or "").strip()
    if mode not in {"all_reference", "allReference"}:
        return None
    if not any(
        str(item.get("type") or "").lower() == "video"
        for item in payload.get("reference_items") or []
        if isinstance(item, dict)
    ):
        return None
    message = str(error).lower().replace("`", "")
    if not all(
        marker in message
        for marker in (
            "seedance identified your task as video editing",
            "ratio must be adaptive",
            "duration must be -1",
        )
    ):
        return None
    return (
        "Seedance 将该提示词判定为视频编辑，输出比例和时长需跟随源视频。"
        "请改用「视频编辑」模式，或改写提示词以明确生成新视频。"
        "用于编辑的源视频需为 4–30 秒。"
    )


async def _run_freezone_video_gen_async(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    from novelvideo.api.deps import make_static_url_for_context
    from novelvideo.freezone.jobs import ensure_freezone_dirs, run_freezone_video_gen

    payload = envelope.get("payload") or {}
    job_id = str(payload["job_id"])
    project_dir = Path(str(payload.get("project_dir") or ctx.output_dir))
    ensure_freezone_dirs(project_dir)

    manager = get_task_manager()
    manager.update_progress_for_project(
        ctx,
        "freezone_video_gen",
        0,
        scope=job_id,
        progress=0.1,
        current_task="调用视频生成器...",
        logs=["开始 freezone 视频生成"],
    )

    trusted_context = envelope.get("__trusted_egress_context")
    video_leaf = run_freezone_video_gen
    trusted_kwargs: dict[str, Any] = {}
    if trusted_context is not None and trusted_context.billing_principal.kind in {
        "organization",
        "local",
    }:
        from novelvideo.freezone.video_node import run_trusted_freezone_video_gen

        video_leaf = run_trusted_freezone_video_gen
        trusted_kwargs = {
            "egress_context": trusted_context,
            "task_type": str(envelope.get("task_type") or "freezone_video_gen"),
            "episode": int(envelope.get("episode") or 0),
            "beat_num": int(envelope.get("beat_num") or 0),
            "scope": str(envelope.get("scope") or job_id),
        }

    try:
        out_path = await video_leaf(
            project_dir=project_dir,
            job_id=job_id,
            prompt=str(payload.get("prompt") or ""),
            reference_items=payload.get("reference_items") or None,
            aspect_ratio=str(payload.get("aspect_ratio") or "16:9"),
            resolution=str(payload.get("resolution") or "720p"),
            duration_seconds=int(payload.get("duration_seconds") or 5),
            generate_audio=bool(payload.get("generate_audio")),
            human_review=bool(payload.get("human_review")),
            scene_optimize=str(payload.get("scene_optimize") or ""),
            backend=str(payload.get("backend") or ""),
            last_frame_path=payload.get("last_frame_path"),
            audio_setting=payload.get("audio_setting") or None,
            gen_mode=payload.get("gen_mode") or None,
            model_params=payload.get("model_params") or None,
            request_schema=payload.get("request_schema") or None,
            **trusted_kwargs,
        )
    except Exception as exc:
        user_hint = _omni_video_edit_hint(payload, exc)
        _append_freezone_video_node_history(
            ctx=ctx,
            project_dir=project_dir,
            payload=payload,
            job_id=job_id,
            error=user_hint or str(exc),
        )
        if user_hint:
            raise RuntimeError(user_hint) from exc
        raise

    rel = out_path.relative_to(project_dir).as_posix()
    result = {
        "job_id": job_id,
        "output_path": str(out_path),
        "output_url": make_static_url_for_context(ctx, rel),
    }
    if bool(payload.get("image_animate_gif")):
        from novelvideo.task_backend.client import enqueue_project_task

        gif_job_id = f"{job_id}-gif"
        try:
            queued = await enqueue_project_task(
                ctx,
                task_type="freezone_image_animate_gif",
                product_surface="freezone",
                queue_kind="ffmpeg",
                episode=0,
                scope=gif_job_id,
                payload={
                    "job_id": gif_job_id,
                    "project_dir": str(project_dir),
                    "video_path": str(out_path),
                },
            )
            result.update(
                {
                    "gif_task_type": "freezone_image_animate_gif",
                    "gif_job_id": gif_job_id,
                    "gif_task_key": project_task_state_key(
                        "freezone_image_animate_gif", ctx.project_id, 0, scope=gif_job_id
                    ),
                    "gif_queue": queued.queue,
                }
            )
        except Exception as exc:
            # Preserve the paid video so the canvas can retry only local conversion.
            logger.warning(
                "automatic GIF dispatch failed project=%s video_job=%s: %s",
                ctx.project_id,
                job_id,
                exc,
            )
            result["gif_enqueue_error"] = "GIF 转换任务未能启动，请重试转换。"
    history_record = _append_freezone_video_node_history(
        ctx=ctx,
        project_dir=project_dir,
        payload=payload,
        job_id=job_id,
        result=result,
    )
    if history_record:
        result["generation_history_record"] = history_record
    return result


def run_freezone_video_gen(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    return asyncio.run(
        await_envelope_with_cancel_watch(
            _run_freezone_video_gen_async(envelope, ctx),
            envelope,
            task_type="freezone_video_gen",
        )
    )


register_project_task_runner(
    "freezone_video_gen", run_freezone_video_gen, requires_home_node=False
)
