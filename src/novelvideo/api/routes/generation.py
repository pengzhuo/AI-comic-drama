"""画面/网格/视频生成端点。"""

import json
import io
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse

from novelvideo.api.auth import get_api_user, require_scope
from novelvideo.api.responses import TemporaryFileResponse
from novelvideo.api.upload_workers import run_asset_upload_operation
from novelvideo.api.deps import (
    get_user_base_dir,
    get_state_dir,
    make_sqlite_store_for_context,
    make_sqlite_store,
    make_static_url_for_context,
    resolve_project_scope,
)
from novelvideo.api.schemas import (
    GlobalOptimizeRequest,
    VideoGenerateRequest,
    VideoBackendOption,
    VideoComposeRequest,
    TTSGenerateRequest,
    TTSPreviewRequest,
    SystemVoicePrepareRequest,
    SketchGenerateRequest,
    GridRegenerateRequest,
    BeatsRegenerateRequest,
    SketchRegenerateRequest,
    SingleVideoRequest,
    PoolSelectRequest,
    VideoPoolSelectRequest,
    GridCutRequest,
    GridSketchPreviewRequest,
    PlanEntryOut,
    OperatorPasswordVerifyRequest,
    RenderPlanExecuteRequest,
    RenderPlanExecuteResponse,
    RenderPlanRequest,
    RenderPlanResponse,
    RenderSettingsUpdate,
    SketchRegenQueueUpdate,
    SketchSettingsUpdate,
    BeatBackgroundAnchorUpdate,
    Seedance2AssetAudioTrimRequest,
    Seedance2AssetCropRequest,
    Seedance2AssetDeleteRequest,
)
from novelvideo.utils.source_language import detect_episode_asset_language
from novelvideo.api.viewer_manifests import (
    build_director_stage_manifest,
    build_pano_viewer_manifest,
    default_director_stage_palette,
)
from novelvideo.generators.nanobanana_grid import (
    build_regen_plan,
    compute_input_fingerprint,
    hash_plan,
)
from novelvideo.generators.render_identity_guard import render_ai_detection_error
from novelvideo.manual_shots import pick_beats_by_number
from novelvideo.render_plan.ref_image_hash import RefImageHasher
from novelvideo.seedance2_i2v.pipeline import (
    is_huimeng_seedance2_backend,
    prepare_seedance2_generation_inputs,
)
from novelvideo.seedance2_i2v.voice_clone import normalize_seedance2_audio_type
from novelvideo.project_config import load_project_config, save_project_config
from novelvideo.project_context import ProjectContext
from novelvideo.ports import get_credit_quote, get_task_backend, get_usage_meter
from novelvideo.task_backend.limit_logging import log_task_limit_rejection
from novelvideo.task_backend.limits import (
    ChannelTaskLimitExceeded,
    ProjectTaskLimitExceeded,
    ProjectUserTaskLimitExceeded,
    UserTaskLimitExceeded,
)
from novelvideo.task_identity import project_task_state_key
from novelvideo.models import beat_scene_id
from novelvideo.services.background_anchor_service import (
    BackgroundAnchorError,
    build_background_anchors_payload,
    crop_background_anchor_to_selected,
    save_uploaded_background_anchor_image,
    select_background_anchor,
)
from novelvideo.utils.path_resolver import PathResolver, compute_identity_path, compute_portrait_path


_TemporaryFileResponse = TemporaryFileResponse

router = APIRouter()

logger = logging.getLogger(__name__)

AI_IDENTITY_DETECTION_TASK_TYPE = "ai_identity_detection"
AI_IDENTITY_DETECTION_FEATURE_KEY = "mainline.ai_identity_detection"
MODEL_CALL_CREDIT_POLICY_FEATURE_INCLUDED = "feature_included"
















































_FANOUT_ACTIVE_LIMIT_ERRORS = (
    ChannelTaskLimitExceeded,
    UserTaskLimitExceeded,
    ProjectTaskLimitExceeded,
    ProjectUserTaskLimitExceeded,
)


_FanoutLimitError = (
    ChannelTaskLimitExceeded
    | UserTaskLimitExceeded
    | ProjectTaskLimitExceeded
    | ProjectUserTaskLimitExceeded
)
































SEEDANCE2_RESOLUTION_OPTIONS_BY_MODEL = {
    "seedance-2.0-fast": ("480p", "720p"),
    "seedance-2.0": ("480p", "720p", "1080p"),
    "seedance-2.0-value": ("720p", "1080p"),
    "seedance-2.0-fast-value": ("720p", "1080p"),
    # Seedance 1.5 Pro（有声）清晰度，来源 huimengi /api/v1/models（480p/720p/1080p）
    "seedance-1.5-pro": ("480p", "720p", "1080p"),
}
SEEDANCE2_DEFAULT_RESOLUTION_OPTIONS = ("480p", "720p")
HAPPYHORSE_RESOLUTION_OPTIONS = ("720p", "1080p")
HAPPYHORSE_RATIO_OPTIONS = ("16:9", "9:16", "1:1", "4:3", "3:4")
HAPPYHORSE_SUPPORTED_MODES = ("first_frame", "multimodal_reference")
GROK_VIDEO_RESOLUTION_OPTIONS = ("720p", "480p")
GROK_VIDEO_RATIO_OPTIONS = ("16:9", "9:16", "1:1", "2:3", "3:2")
GROK_VIDEO_SUPPORTED_MODES = ("first_frame", "multimodal_reference")






















SEEDANCE2_SINGLE_VIDEO_CONFIG_FIELDS = {
    "mode",
    "duration",
    "resolution",
    "ratio",
    "generate_audio",
    "return_last_frame",
    "human_review",
    "scene_optimize",
    "final_prompt",
    "prompt_guidance",
    "text_overlay",
}
























@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/seedance2-status")
async def get_seedance2_beat_status(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Return NiceGUI-aligned read-only Seedance 2.0 status for one Beat."""
    ctx = await _seedance2_panel_context(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        user=user,
    )
    return _seedance2_status_response(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        ctx=ctx,
    )


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/seedance2/assets/upload")
async def upload_seedance2_asset(
    project: str,
    episode_num: int,
    beat_num: int,
    file: UploadFile = File(...),
    user: dict = Depends(get_api_user),
):
    """Upload a manual Seedance 2.0 reference asset."""
    ctx = await _seedance2_panel_context(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        user=user,
    )
    from novelvideo.seedance2_i2v.panel_service import (
        save_seedance2_uploaded_file,
    )

    target = await save_seedance2_uploaded_file(
        store=ctx["store"],
        episode=episode_num,
        beat=ctx["beat"],
        project_dir=ctx["output_dir"],
        filename=file.filename or "seedance2_asset",
        upload_stream=file.file,
        content_type=file.content_type or "",
    )
    if target is None:
        return {"ok": False, "error": "unsupported or empty Seedance2 reference asset"}
    return _seedance2_status_response(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        ctx=ctx,
    )


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/seedance2/assets/delete")
async def delete_seedance2_asset(
    project: str,
    episode_num: int,
    beat_num: int,
    body: Seedance2AssetDeleteRequest,
    user: dict = Depends(get_api_user),
):
    """Remove a manually attached Seedance 2.0 reference asset."""
    ctx = await _seedance2_panel_context(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        user=user,
    )
    from novelvideo.seedance2_i2v.panel_service import (
        remove_seedance2_uploaded_asset,
    )

    removed = await remove_seedance2_uploaded_asset(
        store=ctx["store"],
        episode=episode_num,
        beat=ctx["beat"],
        project_dir=ctx["output_dir"],
        media_kind=body.media_kind,
        path=body.path,
    )
    if not removed:
        return {"ok": False, "error": "Seedance2 reference asset was not removed"}
    return _seedance2_status_response(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        ctx=ctx,
    )


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/seedance2/assets/crop")
async def crop_seedance2_asset(
    project: str,
    episode_num: int,
    beat_num: int,
    body: Seedance2AssetCropRequest,
    user: dict = Depends(get_api_user),
):
    """Crop an existing Seedance 2.0 image reference into a manual reference."""
    ctx = await _seedance2_panel_context(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        user=user,
    )
    from novelvideo.seedance2_i2v.panel_service import (
        crop_seedance2_asset_to_reference,
    )

    target = await crop_seedance2_asset_to_reference(
        store=ctx["store"],
        episode=episode_num,
        beat=ctx["beat"],
        project_dir=ctx["output_dir"],
        asset_key=body.asset_key,
        source_path=body.source_path,
        crop_data=body.model_dump(),
    )
    if target is None:
        return {"ok": False, "error": "Seedance2 reference crop failed"}
    return _seedance2_status_response(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        ctx=ctx,
    )


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/seedance2/assets/audio-trim")
async def trim_seedance2_audio_asset(
    project: str,
    episode_num: int,
    beat_num: int,
    body: Seedance2AssetAudioTrimRequest,
    user: dict = Depends(get_api_user),
):
    """Trim an existing Seedance 2.0 audio reference into a 3-5 second clip."""
    ctx = await _seedance2_panel_context(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        user=user,
    )
    from novelvideo.seedance2_i2v.panel_service import (
        trim_seedance2_audio_to_reference,
    )

    try:
        target = await trim_seedance2_audio_to_reference(
            store=ctx["store"],
            episode=episode_num,
            beat=ctx["beat"],
            project_dir=ctx["output_dir"],
            asset_key=body.asset_key,
            source_path=body.source_path,
            start_seconds=body.start_seconds,
            duration_seconds=body.duration_seconds,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if target is None:
        return {"ok": False, "error": "Seedance2 audio reference trim failed"}
    return _seedance2_status_response(
        project=project,
        episode_num=episode_num,
        beat_num=beat_num,
        ctx=ctx,
    )




@router.get("/projects/{project}/video-backends")
async def get_video_backend_options(
    project: str,
    user: dict = Depends(get_api_user),
):
    """Return video backend options shared with the NiceGUI render workbench."""
    await _resolve_generation_project(project, user, required_role="viewer")
    return {"ok": True, "data": [item.model_dump() for item in _api_video_backend_options()]}


@router.get("/projects/{project}/render-settings")
async def get_render_settings(
    project: str,
    user: dict = Depends(get_api_user),
):
    """Return Render-stage image model and sizing settings for React."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    return {
        "ok": True,
        "data": _render_settings_payload(resolved.username, resolved.project_name),
    }


@router.patch("/projects/{project}/render-settings")
async def update_render_settings(
    project: str,
    body: RenderSettingsUpdate,
    user: dict = Depends(get_api_user),
):
    """Persist Render-stage image model and sizing settings."""
    from novelvideo.config import image_generation_selection_options

    resolved = await _resolve_generation_project(project, user, required_role="editor")
    username = resolved.username
    project_name = resolved.project_name
    updates: dict[str, Any] = {}

    if body.render_image_selection is not None:
        selection = str(body.render_image_selection or "").strip()
        if selection not in image_generation_selection_options():
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": f"Invalid render_image_selection: {selection}",
                },
            )
        updates["render_image_selection"] = selection
    if body.sketch_aspect_padding is not None:
        updates["sketch_aspect_padding"] = bool(body.sketch_aspect_padding)

    if updates:
        save_project_config(username, project_name, config=updates)
    return {"ok": True, "data": _render_settings_payload(username, project_name)}


@router.get("/projects/{project}/sketch-settings")
async def get_sketch_settings(
    project: str,
    user: dict = Depends(get_api_user),
):
    """Return Sketch-stage image model settings for React."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    return {
        "ok": True,
        "data": _sketch_settings_payload(resolved.username, resolved.project_name),
    }


@router.patch("/projects/{project}/sketch-settings")
async def update_sketch_settings(
    project: str,
    body: SketchSettingsUpdate,
    user: dict = Depends(get_api_user),
):
    """Persist Sketch-stage image model settings."""
    from novelvideo.config import image_generation_selection_options

    resolved = await _resolve_generation_project(project, user, required_role="editor")
    username = resolved.username
    project_name = resolved.project_name
    updates: dict[str, Any] = {}

    if body.sketch_image_selection is not None:
        selection = str(body.sketch_image_selection or "").strip()
        if selection not in image_generation_selection_options():
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": f"Invalid sketch_image_selection: {selection}",
                },
            )
        updates["sketch_image_selection"] = selection

    if updates:
        save_project_config(username, project_name, config=updates)
    return {"ok": True, "data": _sketch_settings_payload(username, project_name)}










@router.get("/projects/{project}/episodes/{episode_num}/sketch-regen-queue")
async def get_sketch_regen_queue(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """Return the persisted React sketch regeneration dispatch queue."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    return {
        "ok": True,
        "data": _sketch_regen_queue_payload(
            resolved.username,
            resolved.project_name,
            episode_num,
        ),
    }


@router.put("/projects/{project}/episodes/{episode_num}/sketch-regen-queue")
async def update_sketch_regen_queue(
    project: str,
    episode_num: int,
    body: SketchRegenQueueUpdate,
    user: dict = Depends(get_api_user),
):
    """Persist the React sketch regeneration dispatch queue per episode."""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    username = resolved.username
    project_name = resolved.project_name
    config = load_project_config(username, project_name)
    queues, cleaned_legacy, legacy_changed = _react_sketch_regen_queues(config)
    queues[_sketch_regen_queue_key(episode_num)] = [item.model_dump() for item in body.items]
    updates = {"react_sketch_regen_queue": queues}
    if legacy_changed:
        updates["sketch_regen_queue"] = cleaned_legacy
    save_project_config(username, project_name, config=updates)
    return {"ok": True, "data": _sketch_regen_queue_payload(username, project_name, episode_num)}


@router.get("/projects/{project}/episodes/{episode_num}/sketch-image-usage")
async def get_sketch_image_usage(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """Return NiceGUI-style Sketch image request usage summary."""
    from novelvideo.image_request_usage import get_image_usage_summary

    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    summary = get_image_usage_summary(
        project_output_dir=project_dir,
        task_types=("sketch_grid",),
        episode=episode_num,
    )
    return {"ok": True, "data": summary}




@router.get("/projects/{project}/episodes/{episode_num}/image-generation-guard")
async def get_image_generation_guard(
    project: str,
    episode_num: int,
    task_type: str = Query(...),
    scope: str = Query(...),
    subject: str = Query("当前生成任务"),
    user: dict = Depends(get_api_user),
):
    """Return per-scope image generation guard status used before dispatch."""
    from novelvideo.image_request_usage import count_image_scope_attempts

    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    attempt_count = count_image_scope_attempts(
        project_output_dir=project_dir,
        task_type=task_type,
        scope=scope,
        episode=episode_num,
    )
    return {"ok": True, "data": _image_generation_guard_payload(attempt_count, subject)}


@router.post("/projects/{project}/episodes/{episode_num}/image-generation-guard/verify-password")
async def verify_image_generation_guard_password(
    project: str,
    episode_num: int,
    body: OperatorPasswordVerifyRequest,
    user: dict = Depends(get_api_user),
):
    """Verify the same operator password NiceGUI requires after repeated image attempts."""
    from novelvideo.security.operator_auth import get_prompt_export_password

    _ = (project, episode_num, user)
    configured = get_prompt_export_password()
    verified = bool(configured) and (body.password or "") == configured
    return {
        "ok": True,
        "data": {"verified": verified},
    }


@router.post("/projects/{project}/episodes/{episode_num}/videos/compose")
async def compose_video(
    project: str,
    episode_num: int,
    body: VideoComposeRequest,
    user: dict = Depends(get_api_user),
):
    """合成成片。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir
    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)
    if not beats:
        return {"ok": False, "error": f"No beats found for episode {episode_num}"}

    config = {
        "beats": beats,
        "add_subtitles": body.add_subtitles,
        "add_bgm": body.add_bgm,
    }

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="compose_episode",
            queue_kind="ffmpeg",
            episode=episode_num,
            payload={
                **config,
                "episode": episode_num,
                "output_dir": output_dir,
                "resolution": getattr(body, "resolution", "720x1280"),
            },
        )
        return {
            "ok": True,
            "task_type": "compose_episode",
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key("compose_episode", ctx.project_id, episode_num),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集成片合成已进入队列",
        }

    return {"ok": False, "error": "成片合成需要 project context"}


@router.get("/projects/{project}/episodes/{episode_num}/final")
async def get_final_video(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """读取 episode 成片状态，供 supertale-fe compose 页刷新 hydration。"""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    filename = f"ep{episode_num:03d}_final.mp4"
    final_path = project_dir / "videos" / "episodes" / filename
    data = {"exists": final_path.exists(), "filename": filename}
    if final_path.exists():
        data["video_url"] = make_static_url_for_context(
            resolved.ctx,
            f"videos/episodes/{filename}",
            local_path=final_path,
        )
    return {"ok": True, "data": data}


# ── TTS 语音 ──────────────────────────────────────────────────────────────────


@router.post("/projects/{project}/episodes/{episode_num}/tts/generate")
async def generate_tts(
    project: str,
    episode_num: int,
    body: TTSGenerateRequest,
    user: dict = Depends(get_api_user),
):
    """Legacy TTS endpoint removed after IndexTTS2 cutover."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Legacy /tts/generate was removed. Use "
            f"/projects/{project}/episodes/{episode_num}/audio/generate for IndexTTS2."
        ),
    )


@router.post("/projects/{project}/tts/preview")
async def preview_tts(
    project: str,
    body: TTSPreviewRequest,
    user: dict = Depends(get_api_user),
):
    """Legacy TTS preview endpoint removed after IndexTTS2 cutover."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Legacy /tts/preview was removed. IndexTTS2 uses configured reference voices.",
    )


@router.get("/projects/{project}/tts/voices")
async def list_tts_voices(project: str, user: dict = Depends(get_api_user)):
    """Legacy voice listing endpoint removed after IndexTTS2 cutover."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Legacy /tts/voices was removed. IndexTTS2 voice options are project assets.",
    )


# ── 草图 ──────────────────────────────────────────────────────────────────────


@router.post("/projects/{project}/episodes/{episode_num}/sketches/generate")
async def generate_sketches(
    project: str,
    episode_num: int,
    body: SketchGenerateRequest,
    user: dict = Depends(require_scope("tasks:submit")),
):
    """生成草图。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir

    proj_config = load_project_config(username, project_name)
    style = body.style or proj_config.get("visual_style", "chinese_period_drama")

    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    if not beats:
        return {"ok": False, "error": f"No beats found for episode {episode_num}"}

    # 提前验证 grid_index，避免异步任务内才报错
    from novelvideo.generators.nanobanana_grid import (
        get_sketch_nxn_modes,
        sketch_grid_split,
        sketch_scene_grid_split,
    )

    use_scene_grouping = body.sketch_scene_grouping
    if use_scene_grouping:
        loc_plan = sketch_scene_grid_split(beats, aspect_ratio=body.aspect_ratio)
        grid_plan = [(p["rows"], p["cols"]) for p in loc_plan]
        billing_mode_keys = [str(p["mode_key"]) for p in loc_plan]
    else:
        loc_plan = None
        grid_plan = sketch_grid_split(len(beats))
        mode_by_shape = {
            (rows, cols): mode_key
            for _capacity, mode_key, rows, cols in get_sketch_nxn_modes(body.aspect_ratio)
        }
        billing_mode_keys = [mode_by_shape[(rows, cols)] for rows, cols in grid_plan]

    generate_all_grids = body.grid_index == -1
    if body.grid_index < -1 or body.grid_index >= len(grid_plan):
        grid_labels = " + ".join(f"{r}x{c}" for r, c in grid_plan)
        return {
            "ok": False,
            "error": (
                f"grid_index={body.grid_index} 超出范围。"
                f"共 {len(beats)} 个 beats，分割方案: {grid_labels}，"
                f"有效 grid_index: 0~{len(grid_plan) - 1}"
            ),
        }

    character_map = await _build_character_map(
        store,
        beats,
        username,
        project_name,
        episode_num=episode_num,
        use_detected_identities=False,
    )
    has_colors = any(
        info.get("identity_sketch_colors") or info.get("sketch_color")
        for info in character_map.values()
    )
    if not has_colors:
        return {"ok": False, "error": "未检测到颜色分配，请先调用 assign-colors 接口"}

    from novelvideo.utils.path_resolver import PathResolver

    PathResolver(output_dir, episode_num).clean_sketches()

    episode_obj = _episode_from_store_or_none(store, episode_num)
    prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, beats)
    sketch_image_selection = _resolve_sketch_image_selection(
        proj_config,
        body.image_generation_selection,
    )
    base_config = {
        "beats": beats,
        "character_map": character_map,
        "style": style,
        "model": body.model,
        "sketch_scene_grouping": use_scene_grouping,
        "aspect_ratio": body.aspect_ratio,
        "image_generation_selection": sketch_image_selection,
        "sketch_colors": store.get_sketch_colors(episode_num) or {},
        "prop_menu": prop_menu,
    }

    dispatch_grid_indices = list(range(len(grid_plan))) if generate_all_grids else [body.grid_index]
    if ctx is not None:
        batch_size = len(dispatch_grid_indices)
        batch_id = (
            f"sketch-grid-{episode_num}-{uuid.uuid4().hex}"
            if generate_all_grids and batch_size > 1
            else ""
        )
        queued_tasks = []
        rejected: list[dict[str, Any]] = []
        for position, grid_index in enumerate(dispatch_grid_indices):
            scope = f"grid_{grid_index}"
            billing = _sketch_regen_billing_metadata(
                sketch_image_selection,
                billing_mode_keys[grid_index],
            )
            try:
                queued = await get_task_backend().enqueue_project_task(
                    ctx,
                    product_surface="mainline",
                    task_type="sketch_grid_generation",
                    queue_kind="default",
                    episode=episode_num,
                    scope=scope,
                    payload={
                        "episode": episode_num,
                        "output_dir": output_dir,
                        "config": {**base_config, "grid_index": grid_index},
                        "billing": billing,
                        **(
                            {
                                "batch_id": batch_id,
                                "batch_size": batch_size,
                                "display_name": f"第 {episode_num} 集草图批次",
                            }
                            if batch_id
                            else {}
                        ),
                    },
                )
            except _FANOUT_ACTIVE_LIMIT_ERRORS as exc:
                if not queued_tasks:
                    # 一个都没投出去＝纯粹超限：裸抛，交 api/app.py 的 handler
                    # 渲染 429 ＋ 正确的 limit_scope（M8 不变量 7）。
                    raise
                _log_partial_dispatch_rejection(exc)
                # 闸是全局的，后面每一个都必然被拒 —— 所以**只投这一次**（break），
                # 但要把「当前这条 ＋ 后面还没尝试的」全部如实记进 rejected：
                # 契约要 N−k 条（M8 :722 / :755），下游按尾段长度对齐
                # （render-plan-dialog.tsx:265 断言 entries.length == rejected.length）。
                for pending_index in dispatch_grid_indices[position:]:
                    rejected.append(
                        _task_limit_rejection(f"grid_{pending_index}", exc)
                    )
                break
            queued_tasks.append(
                {
                    "grid_index": grid_index,
                    "scope": scope,
                    "task_id": queued.task_state.task_id,
                    "task_key": project_task_state_key(
                        "sketch_grid_generation",
                        ctx.project_id,
                        episode_num,
                        scope=scope,
                    ),
                    "backend": queued.backend,
                    "queue": queued.queue,
                }
            )
        if generate_all_grids:
            grid_labels = " + ".join(f"{r}x{c}" for r, c in grid_plan)
            return {
                "ok": True,
                "task_type": "sketch_grid_generation",
                "backend": queued_tasks[0]["backend"] if queued_tasks else "inline",
                "data": {
                    # 实投数，不是意图数（M8 不变量 17）。
                    "dispatched": len(queued_tasks),
                    "tasks": queued_tasks,
                    "scopes": [item["scope"] for item in queued_tasks],
                    "rejected": rejected,
                },
                "message": f"第 {episode_num} 集全集草图生成已进入队列 ({grid_labels})",
            }

        return {
            "ok": True,
            "task_type": "sketch_grid_generation",
            "backend": queued_tasks[0]["backend"],
            "task_id": queued_tasks[0]["task_id"],
            "task_key": queued_tasks[0]["task_key"],
            "queue": queued_tasks[0]["queue"],
            "message": f"第 {episode_num} 集草图生成已进入队列 (网格 {body.grid_index})",
        }

    return {"ok": False, "error": "草图生成需要 project context"}


# ── 语音生成 ──────────────────────────────────────────────────────────────────










@router.post("/projects/{project}/episodes/{episode_num}/audio/system-voices/prepare")
async def prepare_system_voices_for_agent(
    project: str,
    episode_num: int,
    body: SystemVoicePrepareRequest,
    user: dict = Depends(get_api_user),
):
    """Prepare missing system voice references for the outer Hermes assistant only."""

    agent_kind = str(user.get("agent_kind") or "").strip()
    if agent_kind not in {"hermes", "local_mcp"}:
        raise HTTPException(status_code=403, detail="system voice setup is agent-only")
    if not body.confirmed:
        return {
            "ok": False,
            "code": "system_voice_confirmation_required",
            "error": "使用系统声线前需要用户明确确认",
        }

    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    if ctx is None:
        return {
            "ok": False,
            "code": "project_context_required",
            "error": "系统声线准备需要 project context",
        }

    queued = await get_task_backend().enqueue_project_task(
        ctx,
        product_surface="mainline",
        task_type="system_voice_setup",
        queue_kind="default",
        episode=episode_num,
        payload={
            "episode": episode_num,
            "output_dir": resolved.output_dir,
            "state_dir": resolved.state_dir,
        },
    )
    return {
        "ok": True,
        "task_type": "system_voice_setup",
        "task_id": queued.task_state.task_id,
        "task_key": project_task_state_key(
            "system_voice_setup", ctx.project_id, episode_num
        ),
        "backend": queued.backend,
        "queue": queued.queue,
        "message": f"第 {episode_num} 集系统声线准备已进入队列",
    }


@router.post("/projects/{project}/episodes/{episode_num}/audio/billing-quote")
async def audio_generation_billing_quote(
    project: str,
    episode_num: int,
    body: TTSGenerateRequest = TTSGenerateRequest(),
    user: dict = Depends(get_api_user),
):
    """Quote the exact set of Beat audio model calls for an audio action."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(resolved.username, resolved.project_name)
    )
    mode = body.mode or "sync_changed"
    beat_numbers, errors, billable_chars = await _audio_generation_plan(
        store=store,
        username=resolved.username,
        project=resolved.project_name,
        episode=episode_num,
        beat_numbers=body.beat_numbers,
        mode=mode,
    )
    quantity = len(beat_numbers)
    if quantity <= 0:
        return {
            "ok": True,
            "data": {
                "beat_numbers": [],
                "quantity": 0,
                "unit_cost": 0,
                "cost": 0,
                "display": "",
                "prereq_errors": errors,
            },
        }
    quote_args = {
        "kind": "feature",
        "model": "mainline.beat_audio_generation",
        "params": _audio_billing_payload(
            beat_numbers,
            billable_chars=billable_chars,
        ),
        "quantity": quantity,
        "product_surface": "mainline",
    }
    quote = await get_credit_quote().generation_credit_quote(
        **quote_args,
        user_id=str(user.get("id") or user.get("user_id") or ""),
    )
    return {
        "ok": True,
        "data": {
            "beat_numbers": beat_numbers,
            "quantity": quantity,
            "unit_cost": quote.unit_cost,
            "cost": quote.total_cost,
            "display": quote.display,
            "prereq_errors": errors,
            **(
                {
                    "original_cost": getattr(quote, "original_total_cost", None),
                    "discount_amount": getattr(quote, "discount_amount", None),
                    "promotion": getattr(quote, "promotion", None),
                }
                if getattr(quote, "promotion", None)
                else {}
            ),
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/audio/generate")
async def generate_audio(
    project: str,
    episode_num: int,
    body: TTSGenerateRequest = TTSGenerateRequest(),
    user: dict = Depends(get_api_user),
):
    """批量生成语音（IndexTTS2）。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir
    state_dir = str(ctx.state_dir) if ctx else get_state_dir(username, project_name)
    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    if not beats:
        return {"ok": False, "error": f"No beats found for episode {episode_num}"}

    mode = body.mode or "sync_changed"
    billable_beat_numbers, missing_voice, billable_chars = await _audio_generation_plan(
        store=store,
        username=username,
        project=project_name,
        episode=episode_num,
        beat_numbers=body.beat_numbers,
        mode=mode,
    )
    if missing_voice:
        return _voice_prereq_error_response(missing_voice)
    if not billable_beat_numbers:
        return {
            "ok": False,
            "code": "audio_generation_not_required",
            "error": "没有需要生成的音频",
        }

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="audio_generation_indextts2",
            queue_kind="default",
            episode=episode_num,
            payload={
                "episode": episode_num,
                "mode": mode,
                "beat_numbers": billable_beat_numbers,
                "output_dir": output_dir,
                "state_dir": state_dir,
                "billing": _audio_billing_payload(
                    billable_beat_numbers,
                    billable_chars=billable_chars,
                ),
            },
        )
        return {
            "ok": True,
            "task_type": "audio_generation_indextts2",
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key(
                "audio_generation_indextts2", ctx.project_id, episode_num
            ),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集语音批量生成已进入队列",
        }

    return {
        "ok": False,
        "error": "音频生成需要 project context",
    }


# ── 视频优化 ──────────────────────────────────────────────────────────────────




@router.get("/projects/{project}/episodes/{episode_num}/optimize/video-global/billing-quote")
async def global_optimize_video_billing_quote(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """Return the number of Beat prompt generations the batch action can run."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(resolved.username, resolved.project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)
    beat_numbers = _global_optimize_billable_beat_numbers(
        output_dir=resolved.output_dir,
        episode_num=episode_num,
        beats=beats,
    )
    quantity = len(beat_numbers)
    if quantity <= 0:
        return {
            "ok": True,
            "data": {
                "beat_numbers": [],
                "quantity": 0,
                "unit_cost": 0,
                "cost": 0,
                "display": "",
            },
        }
    quote_args = {
        "kind": "feature",
        "model": "mainline.beat_video_prompt",
        "params": {
            "pricing_metrics": {
                "call_count": quantity,
                "item_count": quantity,
            }
        },
        "quantity": quantity,
        "product_surface": "mainline",
    }
    quote = await get_credit_quote().generation_credit_quote(
        **quote_args,
        user_id=str(user.get("id") or user.get("user_id") or ""),
    )
    return {
        "ok": True,
        "data": {
            "beat_numbers": beat_numbers,
            "quantity": quantity,
            "unit_cost": quote.unit_cost,
            "cost": quote.total_cost,
            "display": quote.display,
            **(
                {
                    "original_cost": getattr(quote, "original_total_cost", None),
                    "discount_amount": getattr(quote, "discount_amount", None),
                    "promotion": getattr(quote, "promotion", None),
                }
                if getattr(quote, "promotion", None)
                else {}
            ),
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/optimize/video-global")
async def global_optimize_video(
    project: str,
    episode_num: int,
    body: GlobalOptimizeRequest = GlobalOptimizeRequest(),
    user: dict = Depends(get_api_user),
):
    """全局视频提示词优化（草图 → AI 自由决策每个 beat 的 i2v/k2v 模式）。

    输出语言由作者保存的当前剧本文本决定，不受界面语言影响。
    """
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir

    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    if not beats:
        return {"ok": False, "error": f"No beats found for episode {episode_num}"}

    language = await detect_episode_asset_language(
        store,
        episode_num,
        fallback_text="\n".join(
            str(beat.get(field) or "")
            for beat in beats
            for field in ("narration_segment", "dialogue", "visual_description")
        ),
    )

    # 预检和报价必须使用同一统计口径。
    billable_beat_numbers = _global_optimize_billable_beat_numbers(
        output_dir=output_dir,
        episode_num=episode_num,
        beats=beats,
    )
    billable_beat_count = len(billable_beat_numbers)
    if billable_beat_count <= 0:
        return {"ok": False, "error": "没有草图，请先生成草图再执行全局优化"}

    characters = store.get_all_characters()
    char_list = [
        {
            "name": c.name,
            "gender": c.gender,
            "body_type": getattr(c, "body_type", ""),
            "role": c.role,
            "is_main": getattr(c, "is_main", False),
            "face_prompt": c.face_prompt,
        }
        for c in characters
    ]

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="global_optimize_video",
            queue_kind="default",
            episode=episode_num,
            payload={
                "episode": episode_num,
                "beats": beats,
                "characters": char_list,
                "output_dir": output_dir,
                "language": language,
                "billing": {
                    "items": billable_beat_count,
                    "beat_numbers": billable_beat_numbers,
                    "pricing_metrics": {
                        "call_count": billable_beat_count,
                        "item_count": billable_beat_count,
                    },
                },
                "beat_numbers": billable_beat_numbers,
            },
        )
        return {
            "ok": True,
            "task_type": "global_optimize_video",
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key(
                "global_optimize_video", ctx.project_id, episode_num
            ),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集全局视频优化已进入队列",
        }

    return {"ok": False, "error": "全局视频优化需要 project context"}


# ── 再生 ──────────────────────────────────────────────────────────────────────


@router.post("/projects/{project}/episodes/{episode_num}/grids/{grid_index}/regenerate")
async def regenerate_grid(
    project: str,
    episode_num: int,
    grid_index: int,
    body: GridRegenerateRequest,
    user: dict = Depends(get_api_user),
):
    """重新生成单个网格。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir

    proj_config = load_project_config(username, project_name)
    style = body.style or proj_config.get("visual_style", "chinese_period_drama")
    render_image_selection = _resolve_render_image_selection(
        proj_config,
        body.image_generation_selection,
    )

    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    if not beats:
        return {"ok": False, "error": f"No beats found for episode {episode_num}"}

    # 验证 grid_index 范围
    character_map = await _build_character_map(
        store,
        beats,
        username,
        project_name,
        episode_num=episode_num,
        use_detected_identities=True,
    )

    if body.character_grouping:
        from novelvideo.generators.nanobanana_grid import character_grid_split

        char_plan = character_grid_split(beats, character_map)
        max_grids = len(char_plan)
        if grid_index < 0 or grid_index >= max_grids:
            grid_labels = " + ".join(
                f'{e["rows"]}x{e["cols"]}(comp={e.get("composite_count","?")})' for e in char_plan
            )
            return {
                "ok": False,
                "error": (
                    f"grid_index={grid_index} 超出范围。"
                    f"角色分组方案: {grid_labels}，"
                    f"有效 grid_index: 0~{max_grids - 1}"
                ),
            }
        selected_beat_numbers = [int(beat) for beat in char_plan[grid_index].get("beat_numbers", [])]
        billing_mode_key = str(char_plan[grid_index]["mode_key"])
    elif body.scene_grouping:
        from novelvideo.generators.nanobanana_grid import scene_grid_split

        loc_plan = scene_grid_split(beats, character_map=character_map)
        max_grids = len(loc_plan)
        if grid_index < 0 or grid_index >= max_grids:
            grid_labels = " + ".join(f'{e["rows"]}x{e["cols"]}({e["scene_id"]})' for e in loc_plan)
            return {
                "ok": False,
                "error": (
                    f"grid_index={grid_index} 超出范围。"
                    f"场景分组方案: {grid_labels}，"
                    f"有效 grid_index: 0~{max_grids - 1}"
                ),
            }
        selected_beat_numbers = [int(beat) for beat in loc_plan[grid_index].get("beat_numbers", [])]
        billing_mode_key = str(loc_plan[grid_index]["mode_key"])
    else:
        from novelvideo.generators.nanobanana_grid import (
            perfect_grid_split,
            REGEN_MODE_CONFIGS as _RMC,
        )

        grid_plan = perfect_grid_split(len(beats))
        if grid_index < 0 or grid_index >= len(grid_plan):
            grid_labels = " + ".join(f'{_RMC[mk]["rows"]}x{_RMC[mk]["cols"]}' for mk in grid_plan)
            return {
                "ok": False,
                "error": (
                    f"grid_index={grid_index} 超出范围。"
                    f"共 {len(beats)} 个 beats，分割方案: {grid_labels}，"
                    f"有效 grid_index: 0~{len(grid_plan) - 1}"
                ),
            }
        start_offset = sum(_RMC[mk]["capacity"] for mk in grid_plan[:grid_index])
        capacity = _RMC[grid_plan[grid_index]]["capacity"]
        billing_mode_key = str(grid_plan[grid_index])
        selected_beat_numbers = [
            int(beat.get("beat_number", index + 1))
            for index, beat in enumerate(beats[start_offset : start_offset + capacity], start_offset)
        ]

    selected_beats = pick_beats_by_number(beats, selected_beat_numbers)
    detection_error = render_ai_detection_error(selected_beats)
    if detection_error:
        return {"ok": False, "error": detection_error}

    config = {
        "beats": beats,
        "character_map": character_map,
        "style": style,
        "model": body.model,
        "image_generation_selection": render_image_selection,
        "render_mode": "Render",
        "scene_grouping": body.scene_grouping,
        "character_grouping": body.character_grouping,
        "sketch_aspect_padding": _resolve_render_bool_setting(
            proj_config,
            "sketch_aspect_padding",
            body.sketch_aspect_padding,
            True,
        ),
    }

    scope = f"grid_{grid_index}"
    if ctx is not None:
        billing = _render_regen_billing_metadata(
            render_image_selection,
            billing_mode_key,
        )
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="grid_regenerate",
            queue_kind="default",
            episode=episode_num,
            scope=scope,
            payload={
                "episode": episode_num,
                "grid_index": grid_index,
                "output_dir": output_dir,
                "config": config,
                "billing": billing,
            },
        )
        return {
            "ok": True,
            "task_type": "grid_regenerate",
            "scope": scope,
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key(
                "grid_regenerate", ctx.project_id, episode_num, scope=scope
            ),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集网格 {grid_index} 重新生成已进入队列",
        }

    return {"ok": False, "error": "网格重新生成需要 project context"}


@router.post("/projects/{project}/episodes/{episode_num}/render/plan")
async def render_plan(
    project: str,
    episode_num: int,
    body: RenderPlanRequest,
    user: dict = Depends(get_api_user),
):
    """Return the server-authoritative render plan for selected beats."""
    if _render_plan_feature_disabled():
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": "feature_disabled",
                "data": {"reason": "DISABLE_RENDER_PLAN_V2 is set"},
            },
        )

    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir
    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    all_beats = await store.get_beats_as_dicts(episode_num)
    if not all_beats:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "no_beats", "data": {"episode": episode_num}},
        )

    beat_indices = normalize_beat_indices(body.beat_indices)
    invalid = validate_beat_indices(all_beats, beat_indices)
    if invalid:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "invalid_beats", "data": {"invalid": invalid}},
        )
    selected_beats = pick_beats_by_number(all_beats, beat_indices)

    detection_error = render_ai_detection_error(selected_beats)
    if detection_error:
        return JSONResponse(status_code=400, content={"ok": False, "error": detection_error})

    character_map = await _build_character_map(
        store,
        selected_beats,
        username,
        project_name,
        episode_num=episode_num,
        use_detected_identities=True,
    )
    sketch_colors = store.get_sketch_colors(episode_num) or {}
    project_config = load_project_config(username, project_name)
    render_image_selection = _resolve_render_image_selection(
        project_config,
        body.image_generation_selection,
    )
    plan = build_regen_plan(
        selected_beats=selected_beats,
        strategy=body.strategy,
        aspect_mode=body.aspect_mode,
        character_map=character_map,
        force_one_by_one=body.force_one_by_one,
        image_generation_selection=render_image_selection,
    )

    hasher = RefImageHasher(Path(output_dir) / ".render_plan_cache")
    try:
        fingerprint = compute_input_fingerprint(
            beats=selected_beats,
            character_map=character_map,
            sketch_colors=sketch_colors,
            strategy=body.strategy,
            aspect_mode=body.aspect_mode,
            force_one_by_one=body.force_one_by_one,
            ref_image_hasher=hasher.hash,
        )
    except FileNotFoundError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "invalid_beats",
                "data": {"reason": f"missing ref image: {exc}"},
            },
        )

    return {
        "ok": True,
        "data": RenderPlanResponse(
            plan=[PlanEntryOut(**entry) for entry in _plan_to_dicts(plan)],
            plan_hash=hash_plan(plan),
            input_fingerprint=fingerprint,
            strategy=body.strategy,
            total_beats=len(selected_beats),
            total_grids=len(plan),
        ).model_dump(),
    }


@router.post("/projects/{project}/episodes/{episode_num}/render/execute")
async def render_execute(
    project: str,
    episode_num: int,
    body: RenderPlanExecuteRequest,
    user: dict = Depends(get_api_user),
):
    """Validate and dispatch a render plan through the current selected-regen task path."""
    if _render_plan_feature_disabled():
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": "feature_disabled",
                "data": {"reason": "DISABLE_RENDER_PLAN_V2 is set"},
            },
        )

    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir
    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    all_beats = await store.get_beats_as_dicts(episode_num)
    if not all_beats:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "no_beats", "data": {"episode": episode_num}},
        )

    beat_indices = normalize_beat_indices(body.beat_indices)
    invalid = validate_beat_indices(all_beats, beat_indices)
    if invalid:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "invalid_beats", "data": {"invalid": invalid}},
        )
    selected_beats = pick_beats_by_number(all_beats, beat_indices)

    detection_error = render_ai_detection_error(selected_beats)
    if detection_error:
        return JSONResponse(status_code=400, content={"ok": False, "error": detection_error})

    character_map = await _build_character_map(
        store,
        selected_beats,
        username,
        project_name,
        episode_num=episode_num,
        use_detected_identities=True,
    )
    sketch_colors = store.get_sketch_colors(episode_num) or {}
    project_config = load_project_config(username, project_name)
    render_image_selection = _resolve_render_image_selection(
        project_config,
        body.image_generation_selection,
    )
    hasher = RefImageHasher(Path(output_dir) / ".render_plan_cache")
    try:
        new_fingerprint = compute_input_fingerprint(
            beats=selected_beats,
            character_map=character_map,
            sketch_colors=sketch_colors,
            strategy=body.strategy,
            aspect_mode=body.aspect_mode,
            force_one_by_one=body.force_one_by_one,
            ref_image_hasher=hasher.hash,
        )
    except FileNotFoundError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "invalid_beats",
                "data": {"reason": f"missing ref image: {exc}"},
            },
        )

    if new_fingerprint != body.input_fingerprint:
        new_plan = build_regen_plan(
            selected_beats=selected_beats,
            strategy=body.strategy,
            aspect_mode=body.aspect_mode,
            character_map=character_map,
            force_one_by_one=body.force_one_by_one,
            image_generation_selection=render_image_selection,
        )
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "input_stale",
                "data": {
                    "new_plan": _plan_to_dicts(new_plan),
                    "new_plan_hash": hash_plan(new_plan),
                    "new_input_fingerprint": new_fingerprint,
                },
            },
        )

    if body.custom_plan:
        custom_error = _custom_render_plan_error(body.plan, beat_indices)
        if custom_error:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": "invalid_custom_plan",
                    "data": {"reason": custom_error},
                },
            )
        execution_plan = body.plan
        execution_hash = hash_plan(execution_plan)
        dispatch_strategy = "custom"
    else:
        recomputed = build_regen_plan(
            selected_beats=selected_beats,
            strategy=body.strategy,
            aspect_mode=body.aspect_mode,
            character_map=character_map,
            force_one_by_one=body.force_one_by_one,
            image_generation_selection=render_image_selection,
        )
        recomputed_hash = hash_plan(recomputed)
        if recomputed_hash != body.plan_hash:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error": "plan_stale",
                    "data": {
                        "new_plan": _plan_to_dicts(recomputed),
                        "new_plan_hash": recomputed_hash,
                        "new_input_fingerprint": new_fingerprint,
                    },
                },
            )
        execution_plan = recomputed
        execution_hash = recomputed_hash
        dispatch_strategy = body.strategy

    from novelvideo.task_identity import selection_scope

    style = project_config.get("visual_style") or "chinese_period_drama"
    episode_obj = _episode_from_store_or_none(store, episode_num)
    prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, all_beats)
    base_config = {
        "beats": all_beats,
        "character_map": character_map,
        "style": style,
        "model": "nanobanana",
        "image_generation_selection": render_image_selection,
        "sketch_colors": sketch_colors,
        "prop_menu": prop_menu,
        "sketch_aspect_padding": _resolve_render_bool_setting(
            project_config,
            "sketch_aspect_padding",
            body.sketch_aspect_padding,
            True,
        ),
    }
    scope = f"{dispatch_strategy}__{execution_hash}"
    dispatched_task_ids: list[str] = []
    rejected: list[dict[str, Any]] = []

    if ctx is not None:
        for position, entry in enumerate(execution_plan):
            entry_beats = [int(beat) for beat in entry.beat_numbers]
            entry_scope = selection_scope(entry.mode_key, entry_beats)
            billing = _render_regen_billing_metadata(
                render_image_selection,
                entry.mode_key,
            )
            try:
                queued = await get_task_backend().enqueue_project_task(
                    ctx,
                    product_surface="mainline",
                    task_type="selected_regen",
                    queue_kind="default",
                    episode=episode_num,
                    scope=entry_scope,
                    payload={
                        "episode": episode_num,
                        "mode_key": entry.mode_key,
                        "output_dir": output_dir,
                        "config": {
                            **base_config,
                            "mode_key": entry.mode_key,
                            "selected_beat_numbers": entry_beats,
                        },
                        "billing": billing,
                    },
                )
            except _FANOUT_ACTIVE_LIMIT_ERRORS as exc:
                if not dispatched_task_ids:
                    # k == 0：裸抛交 handler 渲染 429（M8 不变量 7）。
                    raise
                _log_partial_dispatch_rejection(exc)
                # 只投这一次，但把未投的尾段逐条如实上报（N−k 条，M8 :722 / :755）。
                for pending in execution_plan[position:]:
                    pending_beats = [int(beat) for beat in pending.beat_numbers]
                    rejected.append(
                        _task_limit_rejection(
                            selection_scope(pending.mode_key, pending_beats), exc
                        )
                    )
                break
            dispatched_task_ids.append(queued.task_state.task_id)
    else:
        return {
            "ok": False,
            "error": "渲染计划执行需要 project context",
            "data": RenderPlanExecuteResponse(
                task_type="render_plan",
                message="渲染计划未启动",
                scope=scope,
                resolved_grids=[PlanEntryOut(**entry) for entry in _plan_to_dicts(execution_plan)],
            ).model_dump(),
        }

    return {
        "ok": True,
        "data": RenderPlanExecuteResponse(
            task_type="render_plan",
            message=f"渲染已启动 ({len(execution_plan)} 个网格)",
            scope=scope,
            resolved_grids=[PlanEntryOut(**entry) for entry in _plan_to_dicts(execution_plan)],
        ).model_dump()
        | ({"task_ids": dispatched_task_ids} if dispatched_task_ids else {})
        | ({"rejected": rejected} if rejected else {}),
    }


@router.post("/projects/{project}/episodes/{episode_num}/beats/regenerate")
async def regenerate_beats(
    project: str,
    episode_num: int,
    body: BeatsRegenerateRequest,
    user: dict = Depends(get_api_user),
):
    """选中 Beats 再生画面。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir
    proj_config = load_project_config(username, project_name)
    style = body.style or proj_config.get("visual_style", "chinese_period_drama")
    render_image_selection = _resolve_render_image_selection(
        proj_config,
        body.image_generation_selection,
    )

    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    if not beats:
        return {"ok": False, "error": f"No beats found for episode {episode_num}"}

    # 验证 beat_indices
    if not body.beat_indices:
        return {"ok": False, "error": "beat_indices 不能为空"}
    total_beats = len(beats)
    invalid = [i for i in body.beat_indices if i < 1 or i > total_beats]
    if invalid:
        return {
            "ok": False,
            "error": f"beat_indices {invalid} 超出范围（共 {total_beats} 个 beats，有效: 1~{total_beats}）",
        }

    selected_beats = pick_beats_by_number(beats, body.beat_indices)
    detection_error = render_ai_detection_error(selected_beats)
    if detection_error:
        return {"ok": False, "error": detection_error}

    sketch_paths = PathResolver(output_dir, episode_num)
    missing_sketches = [
        beat_num
        for beat_num in body.beat_indices
        if not sketch_paths.sketch(beat_num).is_file()
    ]
    if missing_sketches:
        missing_labels = ", ".join(f"#{beat_num}" for beat_num in missing_sketches)
        raise HTTPException(
            422,
            f"Render 前请先为 beat {missing_labels} 生成或上传草图。",
        )

    character_map = await _build_character_map(
        store,
        selected_beats,
        username,
        project_name,
        episode_num=episode_num,
        use_detected_identities=True,
    )

    # The selected sketch is the source of truth for a single Render. The
    # client mode remains a compatibility fallback for missing legacy assets.
    mode_key = (
        _single_render_mode_from_sketch(output_dir, episode_num, body.beat_indices)
        or body.mode_key
    )
    episode_obj = _episode_from_store_or_none(store, episode_num)
    prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, beats)
    billing = _render_regen_billing_metadata(render_image_selection, mode_key)
    config = {
        "beats": beats,
        "character_map": character_map,
        "style": style,
        "model": body.model,
        "image_generation_selection": render_image_selection,
        "selected_beat_numbers": body.beat_indices,
        "sketch_colors": store.get_sketch_colors(episode_num) or {},
        "prop_menu": prop_menu,
        "sketch_aspect_padding": _resolve_render_bool_setting(
            proj_config,
            "sketch_aspect_padding",
            body.sketch_aspect_padding,
            True,
        ),
    }

    from novelvideo.task_identity import selection_scope

    scope = selection_scope(mode_key, body.beat_indices)

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="selected_regen",
            queue_kind="default",
            episode=episode_num,
            scope=scope,
            payload={
                "episode": episode_num,
                "mode_key": mode_key,
                "output_dir": output_dir,
                "config": {**config, "mode_key": mode_key},
                "billing": billing,
                **({"batch_id": body.batch_id} if body.batch_id else {}),
                **({"batch_size": body.batch_size} if body.batch_size else {}),
            },
        )
        return {
            "ok": True,
            "task_type": "selected_regen",
            "scope": scope,
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key(
                "selected_regen", ctx.project_id, episode_num, scope=scope
            ),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集选中 Beats 画面再生已进入队列",
        }

    return {"ok": False, "error": "选中 Beats 画面再生需要 project context"}


@router.post("/projects/{project}/episodes/{episode_num}/sketches/regenerate")
async def regenerate_sketches(
    project: str,
    episode_num: int,
    body: SketchRegenerateRequest,
    user: dict = Depends(get_api_user),
):
    """选中 Beats 再生草图。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir
    proj_config = load_project_config(username, project_name)
    style = body.style or proj_config.get("visual_style", "chinese_period_drama")

    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    if not beats:
        return {"ok": False, "error": f"No beats found for episode {episode_num}"}

    # 验证 beat_indices
    if not body.beat_indices:
        return {"ok": False, "error": "beat_indices 不能为空"}
    total_beats = len(beats)
    invalid = [i for i in body.beat_indices if i < 1 or i > total_beats]
    if invalid:
        return {
            "ok": False,
            "error": f"beat_indices {invalid} 超出范围（共 {total_beats} 个 beats，有效: 1~{total_beats}）",
        }

    character_map = await _build_character_map(
        store,
        beats,
        username,
        project_name,
        episode_num=episode_num,
        use_detected_identities=False,
    )

    mode_key = body.mode_key
    episode_obj = _episode_from_store_or_none(store, episode_num)
    prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, beats)
    sketch_image_selection = _resolve_sketch_image_selection(
        proj_config,
        body.image_generation_selection,
    )
    billing = _sketch_regen_billing_metadata(sketch_image_selection, mode_key)
    config = {
        "beats": beats,
        "character_map": character_map,
        "style": style,
        "model": body.model,
        "image_generation_selection": sketch_image_selection,
        "selected_beat_numbers": body.beat_indices,
        "sketch_colors": store.get_sketch_colors(episode_num) or {},
        "prop_menu": prop_menu,
    }

    from novelvideo.task_identity import selection_scope

    scope = selection_scope(mode_key, body.beat_indices)

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="sketch_regen",
            queue_kind="default",
            episode=episode_num,
            scope=scope,
            payload={
                "episode": episode_num,
                "mode_key": mode_key,
                "output_dir": output_dir,
                "config": {**config, "mode_key": mode_key},
                "billing": billing,
            },
        )
        return {
            "ok": True,
            "task_type": "sketch_regen",
            "scope": scope,
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key(
                "sketch_regen", ctx.project_id, episode_num, scope=scope
            ),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集选中 Beats 草图再生已进入队列",
        }

    return {"ok": False, "error": "选中 Beats 草图再生需要 project context"}
































@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/pano-background/manifest")
async def get_beat_pano_background_manifest(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Return the typed 360 viewer manifest for Beat selected-background capture."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        scene_name = _beat_scene_name(beat)
        if not scene_name:
            return {"ok": False, "error": "当前 Beat 没有关联场景"}
        manifest = build_pano_viewer_manifest(
            ctx=resolved.ctx,
            project_dir=project_dir,
            scene_name=scene_name,
            mode="beat",
            episode_num=int(episode_num),
            beat_num=int(beat_num),
            beat=beat,
        )
        if manifest is None:
            return {"ok": False, "error": "当前场景没有 360 全景资产"}
        return {"ok": True, "data": manifest.model_dump(exclude_none=True)}
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.get("/projects/{project}/director-stage/palette")
async def get_default_director_stage_palette(
    project: str,
    user: dict = Depends(get_api_user),
):
    """Return the shared director-stage palette used by local/freezone worlds."""
    await _resolve_generation_project(project, user, required_role="viewer")
    return {"ok": True, "data": default_director_stage_palette().model_dump(exclude_none=True)}


@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/director-stage/manifest")
async def get_beat_director_stage_manifest(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Return the typed 3GS director-stage manifest for Beat-level capture."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        scene_name = _beat_scene_name(beat)
        if not scene_name:
            return {"ok": False, "error": "当前 Beat 没有关联场景"}
        beats = await store.get_beats_as_dicts(int(episode_num))
        sketch_colors = {}
        get_sketch_colors = getattr(store, "get_sketch_colors", None)
        if get_sketch_colors is not None:
            sketch_colors = dict(get_sketch_colors(int(episode_num)) or {})
        episode_obj = _episode_from_store_or_none(store, int(episode_num))
        prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, list(beats))
        manifest = build_director_stage_manifest(
            ctx=resolved.ctx,
            project_dir=project_dir,
            scene_name=scene_name,
            mode="beat",
            episode_num=int(episode_num),
            beat_num=int(beat_num),
            beat=beat,
            sketch_colors=sketch_colors,
            prop_marker_colors=_prop_marker_colors_from_menu(prop_menu),
        )
        if manifest is None:
            return {"ok": False, "error": "当前场景没有 3GS 资产"}
        return {"ok": True, "data": manifest.model_dump(exclude_none=True)}
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/director-stage/overlay")
async def get_beat_director_stage_overlay(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Load the current Beat 3GS overlay, or inherit the previous same-scene Beat."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        scene_name = _beat_scene_name(beat)
        if not scene_name:
            return {"ok": False, "error": "当前 Beat 没有关联场景"}
        beats = await store.get_beats_as_dicts(int(episode_num))
        return {
            "ok": True,
            "data": _director_overlay_status_payload(
                project_dir=project_dir,
                episode_num=int(episode_num),
                beat_num=int(beat_num),
                scene_name=scene_name,
                beats=list(beats),
            ),
        }
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/director-stage/overlay")
async def save_beat_director_stage_overlay(
    project: str,
    episode_num: int,
    beat_num: int,
    body: dict[str, Any],
    user: dict = Depends(get_api_user),
):
    """Persist the current Beat 3GS overlay to director_blockings/epNNN/beat_MM.json."""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        scene_name = _beat_scene_name(beat)
        if not scene_name:
            return {"ok": False, "error": "当前 Beat 没有关联场景"}
        payload = _director_overlay_payload(
            episode_num=int(episode_num),
            beat_num=int(beat_num),
            scene_name=scene_name,
            beat=beat,
            body=body,
        )
        from novelvideo.director_world.store import save_beat_blocking

        path = save_beat_blocking(project_dir, int(episode_num), int(beat_num), payload)
        if hasattr(store, "update_beat_asset"):
            overlay_prop_labels = [
                str(item.get("label") or item.get("prop_id") or "").strip()
                for item in payload.get("props", [])
                if isinstance(item, dict)
                and str(item.get("type") or "").strip() != "prop_staging"
                and str(item.get("category") or "").strip() != "staging"
            ]
            merged_props = [
                prop
                for prop in [
                    *payload["beat_context"].get("detected_props", []),
                    *overlay_prop_labels,
                ]
                if prop
            ]
            deduped_props = list(dict.fromkeys(merged_props))
            await store.update_beat_asset(
                episode_number=int(episode_num),
                beat_number=int(beat_num),
                detected_props=deduped_props,
            )
        beats = await store.get_beats_as_dicts(int(episode_num))
        return {
            "ok": True,
            "data": {
                "status": "saved",
                "overlay": payload,
                "path": path.as_posix(),
                "same_scene_beats": _director_same_scene_beats(list(beats), scene_name),
            },
        }
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/director-stage/control-frame")
async def export_beat_director_stage_control_frame(
    project: str,
    episode_num: int,
    beat_num: int,
    body: dict[str, Any],
    user: dict = Depends(get_api_user),
):
    """Persist Director Render control-frame PNG layers and frame_meta.json."""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        scene_name = _beat_scene_name(beat)
        if not scene_name:
            return {"ok": False, "error": "当前 Beat 没有关联场景"}
        try:
            payload = _director_control_frame_export_payload(
                ctx=resolved.ctx,
                project_dir=project_dir,
                scene_name=scene_name,
                episode_num=int(episode_num),
                beat_num=int(beat_num),
                body=body,
            )
        except (ValueError, TypeError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        return {"ok": True, "data": payload}
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/background-anchors")
async def get_beat_background_anchors(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Return NiceGUI-compatible single-beat background anchor options."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        return {
            "ok": True,
            "data": _background_anchors_payload(
                ctx=resolved.ctx,
                username=username,
                project=project_name,
                project_dir=project_dir,
                beat=beat,
                episode_num=episode_num,
                beat_num=beat_num,
            ),
        }
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.patch("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/background-anchor")
async def update_beat_background_anchor(
    project: str,
    episode_num: int,
    beat_num: int,
    body: BeatBackgroundAnchorUpdate,
    user: dict = Depends(get_api_user),
):
    """Persist the single-beat background anchor selection.

    Matches NiceGUI's render-input semantics: master/reverse/director env-only
    are snapshotted into the beat-owned selected_background.png before being
    used, while render_anchor_source_id preserves the UI-visible source.
    """
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        try:
            payload = select_background_anchor(
                project_dir=project_dir,
                username=username,
                project=project_name,
                beat=beat,
                episode_num=int(episode_num),
                beat_num=int(beat_num),
                anchor_id=body.anchor_id,
                reference_url_builder=_api_background_reference_url_builder(resolved.ctx),
                anchor_url_builder=_api_background_anchor_url_builder(resolved.ctx),
            )
        except BackgroundAnchorError as exc:
            return {"ok": False, "error": str(exc)}

        if hasattr(store, "update_beat_asset"):
            await store.update_beat_asset(
                episode_number=int(episode_num),
                beat_number=int(beat_num),
                scene_ref=dict(beat.get("scene_ref") or {}),
            )

        return {"ok": True, "data": payload}
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/background-anchor/crop")
async def crop_beat_background_anchor(
    project: str,
    episode_num: int,
    beat_num: int,
    body: dict[str, Any],
    user: dict = Depends(get_api_user),
):
    """Crop a source background into the beat-owned render background slot."""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        try:
            payload = crop_background_anchor_to_selected(
                project_dir=project_dir,
                username=username,
                project=project_name,
                beat=beat,
                episode_num=int(episode_num),
                beat_num=int(beat_num),
                anchor_id=str(body.get("anchor_id") or ""),
                crop=body,
                reference_url_builder=_api_background_reference_url_builder(resolved.ctx),
                anchor_url_builder=_api_background_anchor_url_builder(resolved.ctx),
            )
        except BackgroundAnchorError as exc:
            return {"ok": False, "error": str(exc)}
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "裁剪参数无效"},
            )
        except Exception as exc:
            return {"ok": False, "error": f"裁剪 Render 背景参考失败: {exc}"}

        if hasattr(store, "update_beat_asset"):
            await store.update_beat_asset(
                episode_number=int(episode_num),
                beat_number=int(beat_num),
                scene_ref=dict(beat.get("scene_ref") or {}),
            )

        return {"ok": True, "data": payload}
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/background-anchor/upload")
async def upload_beat_background_anchor(
    project: str,
    episode_num: int,
    beat_num: int,
    file: UploadFile = File(...),
    user: dict = Depends(get_api_user),
):
    """Upload an external render-background reference for a single Beat.

    This mirrors NiceGUI's Render 背景参考 upload path: the image is stored in
    the beat-owned selected_background.png slot and the beat scene_ref persists
    render_anchor_id=selected_background plus render_anchor_source_id for UI.
    It is a compatibility API for React; render generation still consumes the
    same core scene_ref contract.
    """
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    store, beat = await _episode_beat_from_resolution(resolved, episode_num, beat_num)
    try:
        try:
            image = await _read_uploaded_rgb_image(file)
        except Exception as exc:
            return {"ok": False, "error": f"上传外部参考图失败: {exc}"}

        try:
            payload = save_uploaded_background_anchor_image(
                project_dir=project_dir,
                username=username,
                project=project_name,
                beat=beat,
                episode_num=int(episode_num),
                beat_num=int(beat_num),
                image=image,
                reference_url_builder=_api_background_reference_url_builder(resolved.ctx),
                anchor_url_builder=_api_background_anchor_url_builder(resolved.ctx),
            )
        except BackgroundAnchorError as exc:
            return {"ok": False, "error": str(exc)}

        if hasattr(store, "update_beat_asset"):
            await store.update_beat_asset(
                episode_number=int(episode_num),
                beat_number=int(beat_num),
                scene_ref=dict(beat.get("scene_ref") or {}),
            )

        return {"ok": True, "data": payload}
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()


@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/director-control-frame")
async def get_director_control_frame_status(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Return the NiceGUI director control frame status for one beat."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    return {
        "ok": True,
        "data": _director_control_payload(
            ctx=resolved.ctx,
            project_dir=project_dir,
            episode_num=episode_num,
            beat_num=beat_num,
        ),
    }


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/director-control-to-sketch")
async def director_control_to_sketch(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Start the existing Direct Render combined.png -> canonical sketch task."""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    state_dir = str(ctx.state_dir) if ctx else get_state_dir(username, project_name)
    payload = _director_control_payload(
        ctx=resolved.ctx,
        project_dir=project_dir,
        episode_num=episode_num,
        beat_num=beat_num,
    )
    if not payload["ready"]:
        return {
            "ok": False,
            "error": f"Beat {int(beat_num)} 缺少 Direct Render combined.png，请先从 3GS / Freezone 导出",
            "data": payload,
        }

    project_config = load_project_config(username, project_name)
    sketch_image_selection = _resolve_sketch_image_selection(project_config)
    mode_key = str(payload.get("mode_key") or "1x1_2-3_sketch")
    billing = _sketch_regen_billing_metadata(sketch_image_selection, mode_key)

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="director_control_to_sketch",
            queue_kind="default",
            episode=int(episode_num),
            beat_num=int(beat_num),
            scope=payload["scope"],
            payload={
                "task_kind": "director_control_to_sketch",
                "episode": int(episode_num),
                "beat_num": int(beat_num),
                "output_dir": str(project_dir),
                "state_dir": state_dir,
                "mode_key": mode_key,
                "billing": billing,
            },
        )
        return {
            "ok": True,
            "task_type": "director_control_to_sketch",
            "scope": payload["scope"],
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key(
                "director_control_to_sketch",
                ctx.project_id,
                int(episode_num),
                beat_num=int(beat_num),
                scope=payload["scope"],
            ),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"Beat {int(beat_num)} Direct Render 转草图任务已进入队列",
            "data": payload,
        }

    try:
        start_fn = globals().get("start_control_frame_to_sketch_task")
        if start_fn is None:
            return {
                "ok": False,
                "error": "Direct Render 转草图需要 project context",
                "data": payload,
            }

        start_fn(
            username=username,
            project=project_name,
            episode=int(episode_num),
            beat_num=int(beat_num),
            output_dir=str(project_dir),
            state_dir=state_dir,
            scope=payload["scope"],
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "data": payload}

    return {
        "ok": True,
        "task_type": "director_control_to_sketch",
        "scope": payload["scope"],
        "message": f"Beat {int(beat_num)} Direct Render 转草图任务已启动",
        "data": payload,
    }


@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/sketch/pose-editor")
async def get_sketch_pose_editor(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Return NiceGUI-compatible pose editor payload for a canonical sketch."""
    from PIL import Image
    from novelvideo.services.sketch_pose_service import (
        POSE_PRESETS,
        SKELETON_EDGES,
        build_all_episode_candidates,
        build_pose_candidates,
        _heuristic_pose_from_bbox,
    )

    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    sketch_path = _canonical_sketch_path(project_dir, episode_num, beat_num)
    if not sketch_path.exists():
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"Beat {beat_num} 缺少当前草图"},
        )

    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)
    beat = next((b for b in beats if int(b.get("beat_number", 0) or 0) == beat_num), None)
    if beat is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"Beat {beat_num} 不存在"},
        )

    sketch_colors = store.get_sketch_colors(episode_num) or {}
    candidates = build_pose_candidates(beat, sketch_colors)
    if not candidates:
        candidates = build_all_episode_candidates(sketch_colors)
    if not candidates:
        return {"ok": False, "error": "本集没有分配颜色的身份，请先重新配色"}

    with Image.open(sketch_path) as image:
        width, height = image.size

    skeletons: list[dict[str, Any]] = []
    total = len(candidates)
    margin = width * 0.15
    spacing = (width - 2 * margin) / max(1, total - 1) if total > 1 else 0
    for idx, candidate in enumerate(candidates):
        cx = int(margin + idx * spacing) if total > 1 else width // 2
        bw = max(40, int(width * 0.15))
        bh = max(80, int(height * 0.65))
        cy = int(height * 0.1)
        bbox = (cx - bw // 2, cy, cx + bw // 2, cy + bh)
        pose_data = _heuristic_pose_from_bbox(bbox, (width, height))
        skeletons.append(
            {
                "identityId": candidate.identity_id,
                "colorHex": candidate.color_hex,
                "colorName": candidate.color_name,
                "joints": pose_data["joints"],
                "lineWidth": pose_data.get("line_width", 3),
                "headRadius": pose_data.get("head_radius", 12),
                "visible": False,
                "active": idx == 0,
            }
        )

    return {
        "ok": True,
        "data": {
            "beat_num": beat_num,
            "sketch_url": _canonical_sketch_url(resolved.ctx, project_dir, episode_num, beat_num),
            "width": width,
            "height": height,
            "candidates": [
                {
                    "identity_id": candidate.identity_id,
                    "color_hex": candidate.color_hex,
                    "color_name": candidate.color_name,
                }
                for candidate in candidates
            ],
            "skeleton_edges": SKELETON_EDGES,
            "pose_presets": POSE_PRESETS,
            "skeletons": skeletons,
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/sketch/pose-editor")
async def save_sketch_pose_editor(
    project: str,
    episode_num: int,
    beat_num: int,
    body: dict[str, Any],
    user: dict = Depends(get_api_user),
):
    """Persist pose editor strokes/skeletons back to the canonical sketch."""
    from novelvideo.services.sketch_pose_service import save_pose_editor_state

    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir
    sketch_path = _canonical_sketch_path(project_dir, episode_num, beat_num)
    if not sketch_path.exists():
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"Beat {beat_num} 缺少当前草图"},
        )

    try:
        save_pose_editor_state(str(sketch_path), body)
    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"保存草图编辑失败: {exc}"},
        )

    return {
        "ok": True,
        "data": {
            "beat_num": beat_num,
            "sketch_url": _canonical_sketch_url(resolved.ctx, project_dir, episode_num, beat_num),
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/sketch/crop")
async def crop_current_sketch(
    project: str,
    episode_num: int,
    beat_num: int,
    body: dict[str, Any],
    user: dict = Depends(get_api_user),
):
    """Crop and overwrite the canonical sketch, matching NiceGUI current-image crop."""
    from PIL import Image

    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir
    sketch_path = _canonical_sketch_path(project_dir, episode_num, beat_num)
    if not sketch_path.exists():
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"Beat {beat_num} 缺少当前草图"},
        )

    try:
        x = int(body.get("x", 0))
        y = int(body.get("y", 0))
        width = int(body.get("width", 0))
        height = int(body.get("height", 0))
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "裁剪参数无效"},
        )
    if width <= 0 or height <= 0:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "裁剪宽高必须大于 0"},
        )

    with Image.open(sketch_path).convert("RGBA") as image:
        crop_x = max(0, min(x, image.width - 1))
        crop_y = max(0, min(y, image.height - 1))
        right = min(crop_x + width, image.width)
        bottom = min(crop_y + height, image.height)
        cropped = image.crop((crop_x, crop_y, right, bottom))
        cropped.save(sketch_path, format="PNG")

    return {
        "ok": True,
        "data": {
            "beat_num": beat_num,
            "sketch_url": _canonical_sketch_url(resolved.ctx, project_dir, episode_num, beat_num),
            "width": cropped.width,
            "height": cropped.height,
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/sketches/generate-missing-manual")
async def generate_missing_manual_sketches(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """Dispatch sketch regen only for manually inserted beats missing sketches.

    This scans `is_manual_shot=True` beats whose canonical sketch file does not
    exist, groups adjacent missing manual beats by scene, and dispatches one
    `sketch_regen` task per group. Normal beats are never regenerated here.
    """
    from novelvideo.manual_shots import (
        choose_manual_sketch_mode_key,
        missing_manual_shot_segments,
        storyboard_beats_for_manual_sketches,
    )
    from novelvideo.task_identity import selection_scope

    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    output_dir = resolved.output_dir
    sketches_dir = project_dir / "sketches" / f"ep{episode_num:03d}"

    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)
    if not beats:
        return {"ok": False, "error": f"第 {episode_num} 集没有 beats"}

    storyboard_beats = storyboard_beats_for_manual_sketches(beats)
    segments = missing_manual_shot_segments(storyboard_beats, sketches_dir)
    if not segments:
        return {
            "ok": True,
            "data": {"dispatched": 0, "scopes": [], "segments": []},
            "message": "没有缺草图的手工分镜",
        }

    proj_config = load_project_config(username, project_name)
    style = proj_config.get("visual_style", "chinese_period_drama")
    sketch_image_selection = _resolve_sketch_image_selection(proj_config)
    character_map = await _build_character_map(
        store,
        beats,
        username,
        project_name,
        episode_num=episode_num,
        use_detected_identities=False,
    )
    sketch_colors = store.get_sketch_colors(episode_num) or {}

    dispatched_scopes: list[str] = []
    dispatched_segments: list[list[int]] = []
    rejected: list[dict[str, Any]] = []
    for position, beat_numbers in enumerate(segments):
        beat_indices = [int(n) for n in beat_numbers]
        mode_key = choose_manual_sketch_mode_key(len(beat_indices))
        config = {
            "beats": beats,
            "character_map": character_map,
            "style": style,
            "model": None,
            "image_generation_selection": sketch_image_selection,
            "selected_beat_numbers": beat_indices,
            "composite_key": f"{mode_key}:sketch",
            "sketch_colors": sketch_colors,
        }
        scope = selection_scope(mode_key, beat_indices)
        if ctx is not None:
            try:
                await get_task_backend().enqueue_project_task(
                    ctx,
                    product_surface="mainline",
                    task_type="sketch_regen",
                    queue_kind="default",
                    episode=episode_num,
                    scope=scope,
                    payload={
                        "episode": episode_num,
                        "mode_key": mode_key,
                        "output_dir": output_dir,
                        "config": {**config, "mode_key": mode_key},
                    },
                )
            except _FANOUT_ACTIVE_LIMIT_ERRORS as exc:
                if not dispatched_scopes:
                    # k == 0：裸抛交 handler 渲染 429（M8 不变量 7）。
                    raise
                _log_partial_dispatch_rejection(exc)
                # 只投这一次，但把未投的尾段逐条如实上报（N−k 条，M8 :722 / :755）。
                for pending in segments[position:]:
                    pending_beats = [int(n) for n in pending]
                    pending_mode_key = choose_manual_sketch_mode_key(len(pending_beats))
                    rejected.append(
                        _task_limit_rejection(
                            selection_scope(pending_mode_key, pending_beats), exc
                        )
                    )
                break
            dispatched_scopes.append(scope)
            dispatched_segments.append(beat_indices)
            continue

        return {
            "ok": False,
            "error": f"分段 {beat_indices} 派发失败: 需要 project context",
            "data": {
                "dispatched": len(dispatched_scopes),
                "scopes": dispatched_scopes,
                "segments": dispatched_segments,
            },
        }

    return {
        "ok": True,
        "task_type": "sketch_regen",
        "data": {
            "dispatched": len(dispatched_segments),
            "scopes": dispatched_scopes,
            "segments": dispatched_segments,
            "rejected": rejected,
        },
        "message": f"已启动 {len(dispatched_segments)} 组新增分镜草图生成",
    }


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/video")
async def generate_single_video(
    project: str,
    episode_num: int,
    beat_num: int,
    body: SingleVideoRequest,
    user: dict = Depends(get_api_user),
):
    """单 Beat 视频再生。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    return await enqueue_single_video_task(
        ctx=ctx,
        store=store,
        username=username,
        project_name=project_name,
        output_dir=resolved.output_dir,
        episode_num=episode_num,
        beat_num=beat_num,
        body=body,
    )


async def enqueue_single_video_task(
    *,
    ctx: Any,
    store: Any,
    username: str,
    project_name: str,
    output_dir: str,
    episode_num: int,
    beat_num: int,
    body: SingleVideoRequest,
) -> dict[str, Any]:
    """Enqueue one beat's video task with the exact payload the REST route uses.

    Extracted so the deterministic auto pipeline can start beat videos without an
    agent turn; both callers therefore build the same payload the ``single_video``
    runner expects, and the fast path cannot drift from the product surface.
    """

    # 未显式指定视频后端时，跟随项目配置的默认视频模型（其本身回落到实例的
    # VIDEO_BACKEND）。请求模型写死会让单镜/批量工具落到与实例网关不匹配的模型。
    # 走模块级 ``load_project_config``，既有测试用它注入项目配置。
    if "video_backend" not in body.model_fields_set:
        default_video_backend = str(
            load_project_config(username, project_name).get("video_backend") or ""
        ).strip()
        if default_video_backend:
            body.video_backend = default_video_backend

    # 加载 beat 数据
    beats = await store.get_beats_as_dicts(episode_num)
    beat = next((b for b in beats if b.get("beat_number") == beat_num), None)
    if not beat:
        return {"ok": False, "error": f"Beat {beat_num} not found"}
    backend_error = _validate_seedance_pro_dialogue_only([beat], body.video_backend)
    if backend_error:
        return {"ok": False, "error": backend_error}
    is_seedance2 = _is_seedance2_backend(body.video_backend)
    is_happyhorse = _is_happyhorse_backend(body.video_backend)
    is_grok_video = _is_grok_video_backend(body.video_backend)

    # 首帧路径
    from novelvideo.utils.path_resolver import PathResolver

    paths = PathResolver(output_dir, episode_num)
    frame_path = paths.first_frame_for_video(
        beat_num,
        use_director_render=bool(body.use_director_render),
    )
    if not frame_path.exists():
        return {"ok": False, "error": f"Beat {beat_num} 首帧不存在，请先生成预览"}

    # 视频模式与提示词
    video_mode = beat.get("video_mode", "first_frame")
    prompt = _legacy_video_prompt_for_mode(beat, video_mode)

    # 音频时长
    from novelvideo.manual_shots import resolve_target_video_duration

    audio_duration = await _api_audio_duration_seconds(output_dir, episode_num, beat_num)
    video_duration = resolve_target_video_duration(beat, audio_duration)

    # 尾帧路径 (keyframe 模式)
    last_frame_path = None
    if video_mode == "keyframe":
        next_frame = paths.first_frame_for_video(
            beat_num + 1,
            use_director_render=bool(body.use_director_render),
        )
        if next_frame.exists():
            last_frame_path = str(next_frame)
        else:
            video_mode = "first_frame"  # 回退
            prompt = _legacy_video_prompt_for_mode(beat, video_mode)

    seedance2_config_json = None
    single_video_resolution: str | None = None
    happyhorse_references: list[dict[str, str]] = []
    happyhorse_ratio: str | None = None
    grok_video_references: list[dict[str, str]] = []
    grok_video_ratio: str | None = None
    if is_seedance2:
        try:
            request_config_json = _merge_seedance2_request_config(
                beat,
                seedance2_config_json=body.seedance2_config_json,
                config_overrides=_seedance2_request_config_overrides(body),
            )
            if request_config_json and hasattr(store, "update_beat_asset"):
                await store.update_beat_asset(
                    episode_number=episode_num,
                    beat_number=beat_num,
                    seedance2_config_json=request_config_json,
                )
            beat_index = beats.index(beat)
            episode_obj = _episode_from_store_or_none(store, episode_num)
            prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, beats)
            prepared = await _prepare_seedance2_api_beat(
                store=store,
                output_dir=output_dir,
                episode=episode_num,
                beat=beat,
                all_beats=beats,
                index=beat_index,
                video_backend=body.video_backend,
                resolution=body.resolution if "resolution" in body.model_fields_set else None,
                ratio=body.ratio if "ratio" in body.model_fields_set else None,
                prop_menu=prop_menu,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        prompt = prepared.prompt
        video_duration = prepared.duration
        frame_path = Path(prepared.image_path) if prepared.image_path else frame_path
        last_frame_path = prepared.last_frame_path
        seedance2_config_json = prepared.seedance2_config_json
        from novelvideo.seedance2_i2v.models import parse_seedance2_config

        single_video_resolution = parse_seedance2_config(
            seedance2_config_json
        ).resolution
        video_mode = "keyframe" if prepared.last_frame_path else "first_frame"
    elif is_happyhorse:
        try:
            request_config_json = _merge_seedance2_request_config(
                beat,
                seedance2_config_json=body.seedance2_config_json,
                config_overrides=_seedance2_request_config_overrides(body),
            )
            if request_config_json and hasattr(store, "update_beat_asset"):
                await store.update_beat_asset(
                    episode_number=episode_num,
                    beat_number=beat_num,
                    seedance2_config_json=request_config_json,
                )
            beat_index = beats.index(beat)
            episode_obj = _episode_from_store_or_none(store, episode_num)
            prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, beats)
            prepared = await _prepare_happyhorse_api_beat(
                output_dir=output_dir,
                state_dir=Path(store.state_dir),
                episode=episode_num,
                beat=beat,
                next_beat=beats[beat_index + 1] if beat_index + 1 < len(beats) else None,
                frame_path=frame_path,
                video_mode=video_mode,
                prompt=prompt,
                duration=video_duration,
                resolution=body.resolution if "resolution" in body.model_fields_set else None,
                ratio=body.ratio if "ratio" in body.model_fields_set else None,
                prop_menu=prop_menu,
            )
            if prepared["config_json"] and hasattr(store, "update_beat_asset"):
                await store.update_beat_asset(
                    episode_number=episode_num,
                    beat_number=beat_num,
                    seedance2_config_json=str(prepared["config_json"]),
                )
            prompt = str(prepared["prompt"])
            video_duration = float(prepared["duration"])
            frame_path = Path(str(prepared["image_path"])) if prepared["image_path"] else None
            last_frame_path = None
            seedance2_config_json = str(prepared["config_json"])
            single_video_resolution = str(prepared["resolution"])
            happyhorse_ratio = str(prepared["ratio"])
            happyhorse_references = list(prepared.get("references") or [])
            video_mode = "first_frame"
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    elif is_grok_video:
        try:
            request_config_json = _merge_seedance2_request_config(
                beat,
                seedance2_config_json=body.seedance2_config_json,
                config_overrides=_seedance2_request_config_overrides(body),
            )
            if request_config_json and hasattr(store, "update_beat_asset"):
                await store.update_beat_asset(
                    episode_number=episode_num,
                    beat_number=beat_num,
                    seedance2_config_json=request_config_json,
                )
            beat_index = beats.index(beat)
            episode_obj = _episode_from_store_or_none(store, episode_num)
            prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, beats)
            prepared = await _prepare_grok_video_api_beat(
                output_dir=output_dir,
                state_dir=Path(store.state_dir),
                episode=episode_num,
                beat=beat,
                next_beat=beats[beat_index + 1] if beat_index + 1 < len(beats) else None,
                frame_path=frame_path,
                video_mode=video_mode,
                prompt=prompt,
                duration=video_duration,
                resolution=body.resolution if "resolution" in body.model_fields_set else None,
                ratio=body.ratio if "ratio" in body.model_fields_set else None,
                prop_menu=prop_menu,
            )
            if prepared["config_json"] and hasattr(store, "update_beat_asset"):
                await store.update_beat_asset(
                    episode_number=episode_num,
                    beat_number=beat_num,
                    seedance2_config_json=str(prepared["config_json"]),
                )
            prompt = str(prepared["prompt"])
            video_duration = float(prepared["duration"])
            frame_path = Path(str(prepared["image_path"])) if prepared["image_path"] else None
            last_frame_path = None
            seedance2_config_json = str(prepared["config_json"])
            single_video_resolution = str(prepared["resolution"])
            grok_video_ratio = str(prepared["ratio"])
            grok_video_references = list(prepared.get("references") or [])
            video_mode = "first_frame"
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    else:
        if not prompt.strip():
            return {"ok": False, "error": _missing_video_prompt_error(beat_num)}
        # 非 seedance2 后端（含 seedance-1.5-pro）：透传用户选择的时长/清晰度，
        # 并保证视频时长不短于音频（与 1.0 的 duration_floor 行为一致；
        # 生成器侧再按模型上限 4-12 夹紧并向上取整）。
        import math

        if body.duration is not None:
            try:
                video_duration = float(body.duration)
            except (TypeError, ValueError):
                pass
        if audio_duration:
            video_duration = max(float(video_duration), float(math.ceil(float(audio_duration))))
        if "resolution" in body.model_fields_set:
            single_video_resolution = _seedance2_resolution_for_backend(
                body.video_backend, body.resolution
            )

    from novelvideo.video_duration import normalize_video_duration_for_backend

    video_duration = normalize_video_duration_for_backend(
        body.video_backend,
        video_duration,
    )

    config = {
        "beat": dict(beat),
        "frame_path": str(frame_path) if frame_path else None,
        "video_mode": video_mode,
        "prompt": prompt,
        "video_duration": video_duration,
        "video_backend": body.video_backend,
        "use_director_render": bool(body.use_director_render),
        "last_frame_path": last_frame_path,
        "cognee_store_project": f"{username}/{project_name}",
    }
    if seedance2_config_json:
        config["seedance2_config"] = seedance2_config_json
    if single_video_resolution:
        config["resolution"] = single_video_resolution
    if is_happyhorse:
        config["ratio"] = _happyhorse_ratio_for_backend(happyhorse_ratio)
        config["references"] = happyhorse_references
        if body.audio_setting is not None:
            config["audio_setting"] = body.audio_setting
    if is_grok_video:
        config["ratio"] = _grok_video_ratio_for_backend(grok_video_ratio)
        config["references"] = grok_video_references

    billing_resolution = single_video_resolution or _seedance2_resolution_for_backend(
        body.video_backend,
        body.resolution,
    )
    billing = _single_video_billing_metadata(
        body.video_backend,
        resolution=billing_resolution,
        duration=video_duration,
    )

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="single_video",
            queue_kind="video",
            episode=episode_num,
            beat_num=beat_num,
            payload={
                "config": config,
                "output_dir": output_dir,
                "billing": billing,
                **({"batch_id": body.batch_id} if body.batch_id else {}),
                **({"batch_size": body.batch_size} if body.batch_size else {}),
            },
        )
        return {
            "ok": True,
            "task_type": "single_video",
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key(
                "single_video",
                ctx.project_id,
                episode_num,
                beat_num=beat_num,
            ),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集 Beat {beat_num} 视频生成已入队",
        }

    return {"ok": False, "error": "单条视频生成需要 project context"}


# ── 视频池查看 & 选择 ─────────────────────────────────────────────────────────


@router.get("/projects/{project}/episodes/{episode_num}/video-pool")
async def list_video_pool(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir

    from novelvideo.generators.video_pool_indexer import load_video_pool_index

    videos_ep_dir = project_dir / "videos" / "beats" / f"ep{episode_num:03d}"
    pool = load_video_pool_index(videos_ep_dir)
    if not pool:
        return {"ok": True, "data": None}

    videos = []
    for entry in pool.videos:
        item = entry.model_dump()
        if item.get("generated_at"):
            item["generated_at"] = entry.generated_at.isoformat()
        rel_path = f"videos/beats/ep{episode_num:03d}/pool/{entry.video_path}"
        item["video_url"] = make_static_url_for_context(
            resolved.ctx,
            rel_path,
            local_path=project_dir / rel_path,
        )
        videos.append(item)

    return {
        "ok": True,
        "data": {
            "episode": pool.episode,
            "videos": videos,
            "beat_assignments": pool.beat_assignments,
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/video-pool-select")
async def select_video_pool(
    project: str,
    episode_num: int,
    beat_num: int,
    body: VideoPoolSelectRequest,
    user: dict = Depends(get_api_user),
):
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir

    from novelvideo.generators.video_pool_indexer import assign_video_to_beat

    videos_ep_dir = project_dir / "videos" / "beats" / f"ep{episode_num:03d}"
    ok = assign_video_to_beat(videos_ep_dir, beat_num, body.pool_id)
    if not ok:
        return {
            "ok": False,
            "error": f"Pool entry '{body.pool_id}' not found or file missing",
        }

    rel_path = f"videos/beats/ep{episode_num:03d}/beat_{beat_num:02d}.mp4"
    video_url = make_static_url_for_context(
        resolved.ctx,
        rel_path,
        local_path=project_dir / rel_path,
    )
    return {
        "ok": True,
        "data": {
            "beat_num": beat_num,
            "pool_id": body.pool_id,
            "video_url": video_url,
        },
    }


# ── 图片池查看 & 选择 ─────────────────────────────────────────────────────────


@router.get("/projects/{project}/episodes/{episode_num}/grids")
async def list_grids(project: str, episode_num: int, user: dict = Depends(get_api_user)):
    """查看网格预览和图片池。"""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir

    from novelvideo.generators.pool_indexer import (
        compute_beat_content_hash,
        is_pool_image_stale,
        load_pool_index,
    )

    grids_dir = project_dir / "grids" / f"ep{episode_num:03d}"
    pool = await run_asset_upload_operation(load_pool_index, grids_dir)
    if not pool:
        return {"ok": True, "data": None}

    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(username, project_name)
    )
    script_data = await store.get_script_as_dict(episode_num) or {}
    sketch_colors = script_data.get("sketch_colors", {}) or {}
    script_mt = None
    beat_hashes: dict[int, str] = {}
    for beat in script_data.get("beats", []):
        beat_num = beat.get("beat_number")
        if beat_num is not None:
            beat_hashes[beat_num] = compute_beat_content_hash(beat, sketch_colors=sketch_colors)

    images = []
    for img in pool.images:
        entry = img.model_dump()
        # datetime → ISO string
        if entry.get("generated_at"):
            entry["generated_at"] = entry["generated_at"].isoformat()
        # cell URL
        if img.cell_path:
            cell_path = grids_dir / img.cell_path
            entry["cell_url"] = make_static_url_for_context(
                resolved.ctx,
                f"grids/ep{episode_num:03d}/{img.cell_path}",
                local_path=cell_path,
            )
        else:
            entry["cell_url"] = ""
        # grid URL
        if img.grid_path:
            grid_path = grids_dir / img.grid_path
            entry["grid_url"] = make_static_url_for_context(
                resolved.ctx,
                f"grids/ep{episode_num:03d}/{img.grid_path}",
                local_path=grid_path,
            )
        else:
            entry["grid_url"] = ""
        entry["stale"] = is_pool_image_stale(img, beat_hashes, script_mt)
        images.append(entry)

    return {
        "ok": True,
        "data": {
            "episode": pool.episode,
            "modes": pool.modes,
            "images": images,
            "beat_assignments": pool.beat_assignments,
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/grids/rebuild-pool")
async def rebuild_grids_pool_index(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """Rebuild the episode image pool index using the same helper as NiceGUI."""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir

    from novelvideo.generators.pool_indexer import rebuild_pool_index

    grids_dir = project_dir / "grids" / f"ep{episode_num:03d}"
    grids_dir.mkdir(parents=True, exist_ok=True)
    pool = rebuild_pool_index(
        episode_grids_dir=grids_dir,
        episode=episode_num,
        split_cells=True,
    )
    return {
        "ok": True,
        "data": {
            "episode": pool.episode,
            "image_count": len(pool.images),
            "mode_count": len(pool.modes),
        },
    }


@router.get("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/sketch-candidates")
async def get_beat_sketch_candidates(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """Return sketch pool candidates for a beat without treating them as the current sketch."""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir

    from novelvideo.generators.pool_indexer import (
        compute_beat_content_hash,
        is_pool_image_stale,
        load_pool_index,
    )

    grids_dir = project_dir / "grids" / f"ep{episode_num:03d}"
    current_path = project_dir / "sketches" / f"ep{episode_num:03d}" / f"beat_{beat_num:02d}.png"
    current_sketch_url = ""
    if current_path.exists():
        current_sketch_url = make_static_url_for_context(
            resolved.ctx,
            f"sketches/ep{episode_num:03d}/beat_{beat_num:02d}.png",
            local_path=current_path,
        )

    pool = await run_asset_upload_operation(load_pool_index, grids_dir)
    if not pool:
        return {
            "ok": True,
            "data": {
                "episode": episode_num,
                "beat": beat_num,
                "current_sketch_url": current_sketch_url,
                "candidate_count": 0,
                "candidates": [],
            },
        }

    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(username, project_name)
    )
    script_data = await store.get_script_as_dict(episode_num) or {}
    sketch_colors = script_data.get("sketch_colors", {}) or {}
    beat_hashes: dict[int, str] = {}
    for beat in script_data.get("beats", []) or []:
        raw_beat_num = beat.get("beat_number")
        try:
            parsed_beat_num = int(raw_beat_num)
        except (TypeError, ValueError):
            continue
        beat_hashes[parsed_beat_num] = compute_beat_content_hash(
            beat,
            sketch_colors=sketch_colors,
        )

    candidates = []
    for img in pool.images:
        if img.type != "sketch" or int(img.original_beat or 0) != int(beat_num):
            continue
        if not img.cell_path:
            continue
        cell_path = grids_dir / img.cell_path
        if not cell_path.exists():
            continue
        generated_at = img.generated_at.isoformat() if img.generated_at else ""
        candidates.append(
            {
                "id": img.id,
                "type": "sketch",
                "mode": img.mode,
                "cell_path": img.cell_path,
                "url": make_static_url_for_context(
                    resolved.ctx,
                    f"grids/ep{episode_num:03d}/{img.cell_path}",
                    local_path=cell_path,
                ),
                "grid_path": img.grid_path,
                "grid_index": img.grid_index,
                "cell_index": img.cell_index,
                "row": img.row,
                "col": img.col,
                "original_beat": img.original_beat,
                "generated_at": generated_at,
                "stale": is_pool_image_stale(img, beat_hashes, None),
            }
        )
    candidates.sort(
        key=lambda item: (str(item.get("generated_at") or ""), str(item.get("id") or "")),
        reverse=True,
    )

    return {
        "ok": True,
        "data": {
            "episode": episode_num,
            "beat": beat_num,
            "current_sketch_url": current_sketch_url,
            "candidate_count": len(candidates),
            "candidates": candidates,
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/pool-select")
async def select_pool_image(
    project: str,
    episode_num: int,
    beat_num: int,
    body: PoolSelectRequest,
    user: dict = Depends(get_api_user),
):
    """选择 pool 图片，按类型设为 beat 首帧或草图。"""
    import shutil

    resolved = await _resolve_generation_project(project, user, required_role="editor")
    username = resolved.username
    project_name = resolved.project_name
    project_dir = resolved.project_dir

    from novelvideo.generators.pool_indexer import (
        compute_beat_content_hash,
        is_pool_image_stale,
        load_pool_index,
        assign_beat_image,
    )

    grids_dir = project_dir / "grids" / f"ep{episode_num:03d}"
    pool = await run_asset_upload_operation(load_pool_index, grids_dir)
    if not pool:
        return {"ok": False, "error": "No pool index found. Generate grids first."}

    pool_img = pool.get_image(body.pool_id)
    if not pool_img:
        return {"ok": False, "error": f"Pool ID '{body.pool_id}' not found in pool index"}

    if pool_img and pool_img.type == "sketch":
        store = (
            await make_sqlite_store_for_context(resolved.ctx)
            if resolved.ctx
            else await make_sqlite_store(username, project_name)
        )
        script_data = await store.get_script_as_dict(episode_num) or {}
        sketch_colors = script_data.get("sketch_colors", {}) or {}
        beats = script_data.get("beats", [])
        script_mt = None
        beat_hashes: dict[int, str] = {}
        beat_index = pool_img.original_beat - 1
        if 0 <= beat_index < len(beats):
            beat_hashes[pool_img.original_beat] = compute_beat_content_hash(
                beats[beat_index], sketch_colors=sketch_colors
            )
        if is_pool_image_stale(pool_img, beat_hashes, script_mt) and not body.force:
            return {
                "ok": False,
                "stale": True,
                "error": "该草图已过期，请先重新生成。如确认仍要使用，请传 force=true。",
            }

    cell_path = pool_img.cell_path
    if not cell_path:
        return {"ok": False, "error": f"Pool ID '{body.pool_id}' not found in pool index"}

    # 完整路径
    cell_full = grids_dir / cell_path
    if not cell_full.exists():
        return {"ok": False, "error": f"Cell image not found at {cell_path}"}

    image_type = pool_img.type or "render"
    data = {
        "beat_num": beat_num,
        "pool_id": body.pool_id,
        "image_type": image_type,
    }

    if image_type == "sketch":
        sketches_dir = project_dir / "sketches" / f"ep{episode_num:03d}"
        sketches_dir.mkdir(parents=True, exist_ok=True)
        dest = sketches_dir / f"beat_{beat_num:02d}.png"
        shutil.copy2(str(cell_full), str(dest))
        rel = f"sketches/ep{episode_num:03d}/beat_{beat_num:02d}.png"
        data["sketch_url"] = make_static_url_for_context(
            resolved.ctx,
            rel,
            local_path=dest,
        )
    else:
        frames_dir = project_dir / "frames" / f"ep{episode_num:03d}"
        dest = frames_dir / f"beat_{beat_num:02d}.png"
        await run_asset_upload_operation(
            assign_beat_image, grids_dir, beat_num, cell_path, image_type
        )
        rel = f"frames/ep{episode_num:03d}/beat_{beat_num:02d}.png"
        data["frame_url"] = make_static_url_for_context(
            resolved.ctx,
            rel,
            local_path=dest,
        )

    return {
        "ok": True,
        "data": data,
    }


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/sketch/upload")
async def upload_beat_sketch(
    project: str,
    episode_num: int,
    beat_num: int,
    file: UploadFile = File(...),
    user: dict = Depends(get_api_user),
):
    """Upload a beat sketch, store the canonical sketch file, and add it to the pool."""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir
    try:
        image = await _read_uploaded_rgb_image(file)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    sketches_dir = project_dir / "sketches" / f"ep{episode_num:03d}"
    sketch_path = sketches_dir / f"beat_{beat_num:02d}.png"

    pool_id = await run_asset_upload_operation(
        _register_uploaded_pool_image,
        project_dir=project_dir,
        episode_num=episode_num,
        beat_num=beat_num,
        image=image,
        image_type="sketch",
    )
    rel = f"sketches/ep{episode_num:03d}/beat_{beat_num:02d}.png"
    sketch_url = make_static_url_for_context(
        resolved.ctx,
        rel,
        local_path=sketch_path,
    )
    return {
        "ok": True,
        "data": {
            "beat_num": beat_num,
            "pool_id": pool_id,
            "sketch_url": sketch_url,
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/render/upload")
async def upload_beat_render(
    project: str,
    episode_num: int,
    beat_num: int,
    file: UploadFile = File(...),
    user: dict = Depends(get_api_user),
):
    """Upload a beat render first frame, promote it, and add it to the pool."""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir
    try:
        image = await _read_uploaded_rgb_image(file)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    frames_dir = project_dir / "frames" / f"ep{episode_num:03d}"
    frame_path = frames_dir / f"beat_{beat_num:02d}.png"

    pool_id = await run_asset_upload_operation(
        _register_uploaded_pool_image,
        project_dir=project_dir,
        episode_num=episode_num,
        beat_num=beat_num,
        image=image,
        image_type="render",
    )
    rel = f"frames/ep{episode_num:03d}/beat_{beat_num:02d}.png"
    frame_url = make_static_url_for_context(
        resolved.ctx,
        rel,
        local_path=frame_path,
    )
    return {
        "ok": True,
        "data": {
            "beat_num": beat_num,
            "pool_id": pool_id,
            "frame_url": frame_url,
        },
    }


# ── 单 Beat 音频重生 ─────────────────────────────────────────────────────────


@router.post("/projects/{project}/episodes/{episode_num}/beats/{beat_num}/audio")
async def regenerate_beat_audio(
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = Depends(get_api_user),
):
    """重新生成单个 beat 的 IndexTTS2 语音。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    ctx = resolved.ctx
    username = resolved.username
    project_name = resolved.project_name
    output_dir = resolved.output_dir
    state_dir = resolved.state_dir
    store = (
        await make_sqlite_store_for_context(ctx)
        if ctx
        else await make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    beat = next((b for b in beats if b.get("beat_number") == beat_num), None)
    if not beat:
        # 按索引回退
        if 1 <= beat_num <= len(beats):
            beat = beats[beat_num - 1]
        else:
            return {"ok": False, "error": f"Beat {beat_num} not found"}

    billable_beat_numbers, missing_voice, billable_chars = await _audio_generation_plan(
        store=store,
        username=username,
        project=project_name,
        episode=episode_num,
        beat_numbers=[beat_num],
        mode="redo_selected",
    )
    if missing_voice:
        return _voice_prereq_error_response(missing_voice)
    if not billable_beat_numbers:
        return {
            "ok": False,
            "code": "audio_generation_not_required",
            "error": "当前 Beat 没有需要生成的音频",
        }

    if ctx is not None:
        queued = await get_task_backend().enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="audio_generation_indextts2",
            queue_kind="default",
            episode=episode_num,
            payload={
                "episode": episode_num,
                "mode": "redo_selected",
                "beat_numbers": billable_beat_numbers,
                "output_dir": output_dir,
                "state_dir": state_dir,
                "billing": _audio_billing_payload(
                    billable_beat_numbers,
                    billable_chars=billable_chars,
                ),
            },
        )
        return {
            "ok": True,
            "task_type": "audio_generation_indextts2",
            "task_id": queued.task_state.task_id,
            "task_key": project_task_state_key(
                "audio_generation_indextts2", ctx.project_id, episode_num
            ),
            "backend": queued.backend,
            "queue": queued.queue,
            "message": f"第 {episode_num} 集 Beat {beat_num} 语音生成已进入队列",
        }

    return {
        "ok": False,
        "error": "音频生成需要 project context",
    }


# ── SRT 字幕导出 ─────────────────────────────────────────────────────────────


@router.get("/projects/{project}/episodes/{episode_num}/export/srt")
async def export_srt(project: str, episode_num: int, user: dict = Depends(get_api_user)):
    """导出 SRT 字幕文件。"""
    from fastapi.responses import PlainTextResponse

    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir

    # 从图谱读取 beats
    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(resolved.username, resolved.project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    if not beats:
        return {"ok": False, "error": "No beats in script"}

    from novelvideo.export.episode_export import build_srt_content

    srt_content = await build_srt_content(project_dir, episode_num, beats)
    if not srt_content:
        return {"ok": False, "error": "No subtitles to export"}

    return PlainTextResponse(
        content=srt_content,
        media_type="text/srt",
        headers={
            "Content-Disposition": f'attachment; filename="ep{episode_num:03d}.srt"',
        },
    )


@router.get("/projects/{project}/episodes/{episode_num}/export/video")
async def export_final_video(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """Download the composed final episode video."""
    from fastapi.responses import FileResponse

    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    filename = f"ep{episode_num:03d}_final.mp4"
    final_path = project_dir / "videos" / "episodes" / filename
    if not final_path.exists():
        raise HTTPException(status_code=404, detail="Final video not found")
    return FileResponse(
        path=str(final_path),
        filename=filename,
        media_type="video/mp4",
    )


# ── 网格上传 / Prompt 导出 / 切割 ─────────────────────────────────────────────


@router.post("/projects/{project}/episodes/{episode_num}/grids/{grid_index}/upload")
async def upload_grid(
    project: str,
    episode_num: int,
    grid_index: int,
    file: UploadFile = File(...),
    grid_type: str = Form("render"),
    mode_key: str = Form(""),
    beat_numbers: str = Form(""),
    user: dict = Depends(get_api_user),
):
    """上传单张网格整图并更新 pool index 中同 scope 的 grid_path。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir

    grid_type = grid_type.strip() or "render"
    if grid_type not in {"render", "sketch"}:
        return {"ok": False, "error": "grid_type must be render or sketch"}
    try:
        parsed_beats = _parse_grid_beat_numbers(beat_numbers)
    except Exception as exc:
        return {"ok": False, "error": f"invalid beat_numbers: {exc}"}
    mode_key = mode_key.strip() or "upload"

    content = await file.read()
    if not content:
        return {"ok": False, "error": "uploaded file is empty"}

    suffix = Path(file.filename or "").suffix.lower().lstrip(".")
    if suffix not in {"png", "jpg", "jpeg", "webp"}:
        suffix = "png"
    if suffix == "jpeg":
        suffix = "jpg"

    from novelvideo.api.upload_workers import (
        asset_resource_lock,
        run_asset_upload_operation,
    )
    from novelvideo.generators.pool_indexer import persist_uploaded_grid

    grids_dir = project_dir / "grids" / f"ep{episode_num:03d}"
    filename = _uploaded_grid_filename(grid_type, mode_key, parsed_beats, suffix)
    # All scopes in this episode share the same index. Keep the async lock until
    # the worker has committed or rolled back, including repeated cancellation.
    async with asset_resource_lock(("grid-upload", str(grids_dir))):
        grid_path = await run_asset_upload_operation(
            persist_uploaded_grid,
            grids_dir,
            episode_num,
            grid_index,
            grid_type,
            mode_key,
            parsed_beats,
            filename,
            content,
        )
    grid_rel = grid_path.relative_to(grids_dir).as_posix()

    return {
        "ok": True,
        "data": {
            "grid_index": grid_index,
            "grid_type": grid_type,
            "mode_key": mode_key,
            "beat_numbers": parsed_beats,
            "grid_path": grid_rel,
            "grid_url": make_static_url_for_context(
                resolved.ctx,
                f"grids/ep{episode_num:03d}/{grid_rel}",
                local_path=grid_path,
            ),
        },
    }


@router.get("/projects/{project}/episodes/{episode_num}/grids/{grid_index}/prompt")
async def export_grid_prompt(
    project: str,
    episode_num: int,
    grid_index: int,
    grid_type: str = Query("render"),
    mode_key: str = Query(""),
    beat_numbers: str = Query(""),
    user: dict = Depends(get_api_user),
):
    """读取 pool index 中记录的单张网格 prompt 文本。"""
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_dir = resolved.project_dir
    grid_type = grid_type.strip() or "render"
    if grid_type not in {"render", "sketch"}:
        return {"ok": False, "error": "grid_type must be render or sketch"}
    try:
        parsed_beats = _parse_grid_beat_numbers(beat_numbers)
    except Exception as exc:
        return {"ok": False, "error": f"invalid beat_numbers: {exc}"}
    mode_key = mode_key.strip()

    from novelvideo.generators.pool_indexer import load_pool_index

    grids_dir = project_dir / "grids" / f"ep{episode_num:03d}"
    pool = await run_asset_upload_operation(load_pool_index, grids_dir)
    if not pool:
        return {"ok": False, "error": "No pool index found. Generate grids first."}

    entry = _find_pool_grid_entry(
        pool,
        grid_type=grid_type,
        mode_key=mode_key or None,
        beat_numbers=parsed_beats,
        grid_index=grid_index,
    )
    if entry is None:
        return {"ok": False, "error": "Grid prompt metadata not found"}

    prompt_candidates: list[str] = []
    if entry.prompt_path:
        prompt_candidates.append(entry.prompt_path)
    if parsed_beats and entry.mode_key:
        beats_slug = "-".join(str(beat) for beat in parsed_beats)
        prompt_candidates.append(
            f"{entry.preset}/{grid_type}_{entry.mode_key}_{beats_slug}_prompt.txt"
        )

    for relative in prompt_candidates:
        prompt_path = _safe_grids_file(grids_dir, relative)
        if prompt_path and prompt_path.exists():
            return {
                "ok": True,
                "data": {
                    "grid_index": grid_index,
                    "grid_type": grid_type,
                    "mode_key": entry.mode_key,
                    "beat_numbers": list(entry.beat_nums),
                    "prompt": prompt_path.read_text(encoding="utf-8"),
                    "prompt_path": prompt_path.relative_to(grids_dir).as_posix(),
                },
            }

    return {"ok": False, "error": "Prompt file not found for this grid"}


@router.post("/projects/{project}/episodes/{episode_num}/grids/{grid_index}/sketch-preview")
async def sketch_grid_preview(
    project: str,
    episode_num: int,
    grid_index: int,
    body: GridSketchPreviewRequest,
    user: dict = Depends(get_api_user),
):
    """Return the same sketch-thumbnail preview NiceGUI shows for planned grids.

    This API exposes NiceGUI's `_get_sketch_thumbnail_url` behavior to React:
    it stitches existing beat sketches into a temporary preview image without
    changing the generation pipeline.
    """
    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    output_dir = Path(resolved.output_dir)
    ep_grids_dir = output_dir / "grids" / f"ep{episode_num:03d}"

    from novelvideo.generators.nanobanana_grid import crop_sketch_panels
    from novelvideo.generators.pool_indexer import build_beat_sketch_paths, load_pool_index

    beat_numbers = [int(beat) for beat in body.beat_numbers if int(beat) > 0]
    if not beat_numbers:
        return {"ok": False, "error": "beat_numbers is required"}

    paths = build_beat_sketch_paths(ep_grids_dir, beat_numbers)
    pool = await run_asset_upload_operation(load_pool_index, ep_grids_dir)
    if pool:
        latest_pool_paths: dict[int, tuple[float, str]] = {}
        for img in pool.images:
            if img.type != "sketch" or not img.cell_path:
                continue
            beat_num = int(img.original_beat)
            if beat_num not in beat_numbers:
                continue
            cell_path = ep_grids_dir / img.cell_path
            if not cell_path.exists():
                continue
            generated_at = img.generated_at.timestamp() if img.generated_at else 0.0
            current = latest_pool_paths.get(beat_num)
            if current is None or generated_at > current[0]:
                latest_pool_paths[beat_num] = (generated_at, str(cell_path))
        paths = {
            **{beat: path for beat, (_generated_at, path) in latest_pool_paths.items()},
            **paths,
        }
    if not paths:
        return {"ok": False, "error": "No sketch images found for requested beats"}

    beats_slug = "_".join(str(beat) for beat in beat_numbers[:8])
    out_file = (
        ep_grids_dir / f"sketch_thumb_grid{grid_index}_{beats_slug}_{body.rows}x{body.cols}.jpg"
    )
    sketch_out = Path(
        crop_sketch_panels(
            str(ep_grids_dir),
            beat_numbers,
            body.rows,
            body.cols,
            str(out_file),
            beat_sketch_paths=paths,
        )
    )
    try:
        rel = sketch_out.relative_to(ep_grids_dir)
    except ValueError:
        return {"ok": False, "error": "Sketch preview path escaped episode grids directory"}

    return {
        "ok": True,
        "data": {
            "grid_index": grid_index,
            "rows": body.rows,
            "cols": body.cols,
            "beat_numbers": beat_numbers,
            "preview_path": str(rel),
            "preview_url": make_static_url_for_context(
                resolved.ctx,
                f"grids/ep{episode_num:03d}/{rel}",
                local_path=sketch_out,
            ),
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/grids/{grid_index}/cut")
async def cut_grid(
    project: str,
    episode_num: int,
    grid_index: int,
    body: GridCutRequest,
    user: dict = Depends(get_api_user),
):
    """将网格切割为单个 beat 图片入池。"""
    resolved = await _resolve_generation_project(project, user, required_role="editor")
    project_dir = resolved.project_dir

    from datetime import datetime
    from novelvideo.generators.pool_indexer import save_grid_and_split

    episode_grids_dir = project_dir / "grids" / f"ep{episode_num:03d}"
    if not episode_grids_dir.exists():
        return {"ok": False, "error": f"No grids directory for episode {episode_num}"}

    beat_nums = (
        [int(beat) for beat in body.beat_numbers]
        if body.beat_numbers
        else list(range(body.beat_start, body.beat_end + 1))
    )
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    mode_key = body.mode_key or f"{body.rows}x{body.cols}"

    grid_image_path = None
    from novelvideo.generators.pool_indexer import load_pool_index

    pool = await run_asset_upload_operation(load_pool_index, episode_grids_dir)
    entry = _find_pool_grid_entry(
        pool,
        grid_type=body.grid_type,
        mode_key=body.mode_key,
        beat_numbers=beat_nums,
        grid_index=grid_index,
    )
    if entry is not None:
        entry_path = _safe_grids_file(episode_grids_dir, entry.grid_path)
        if entry_path and entry_path.exists():
            grid_image_path = str(entry_path)

    if grid_image_path is None:
        # 兼容旧版根目录 grid_XX.png / jpg 文件。
        grid_files = sorted(episode_grids_dir.glob("*.png")) + sorted(
            episode_grids_dir.glob("*.jpg")
        )
        if grid_index < 0 or grid_index >= len(grid_files):
            return {
                "ok": False,
                "error": f"Grid index {grid_index} out of range (total: {len(grid_files)})",
            }
        grid_image_path = str(grid_files[grid_index])

    if body.grid_type == "render":
        promote_dir = project_dir / "frames" / f"ep{episode_num:03d}"
    else:
        promote_dir = project_dir / "sketches"
    promote_dir.mkdir(parents=True, exist_ok=True)

    result = save_grid_and_split(
        grid_image_path=grid_image_path,
        episode_grids_dir=str(episode_grids_dir),
        grid_type=body.grid_type,
        mode_key=mode_key,
        beat_nums=beat_nums,
        preset="custom",
        rows=body.rows,
        cols=body.cols,
        ts=ts,
        promote_dir=promote_dir,
        force_promote=body.grid_type == "render",
    )

    return {
        "ok": True,
        "data": {
            "grid_index": grid_index,
            "added": result.get("added", 0),
            "skipped": result.get("skipped", 0),
        },
    }


# ── ZIP 导出 ─────────────────────────────────────────────────────────────────


@router.post("/projects/{project}/episodes/{episode_num}/export/zip")
async def export_zip(project: str, episode_num: int, user: dict = Depends(get_api_user)):
    """打包指定集的所有资源为 ZIP 文件下载。"""
    import asyncio
    import zipfile
    import tempfile

    from novelvideo.export.episode_export import build_srt_content
    from novelvideo.utils.async_ops import call_blocking, wait_for_task_completion
    from novelvideo.utils.path_resolver import PathResolver

    resolved = await _resolve_generation_project(project, user, required_role="viewer")
    project_name = resolved.project_name
    project_dir = resolved.project_dir
    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(resolved.username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)

    ep_tag = f"ep{episode_num:03d}"
    paths = PathResolver(str(project_dir), episode_num)

    files_to_pack: list[tuple[Path, str]] = []
    for beat in beats:
        beat_num = int(beat.get("beat_number", 0) or 0)
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

    # Keep existing extra project assets in the API ZIP; NiceGUI's core export
    # is beat audio/video + final + SRT, but frames/grids are useful inspection
    # artifacts and were already part of the React API surface.
    extra_dirs = {
        "frames": project_dir / "frames" / ep_tag,
        "grids": project_dir / "grids" / ep_tag,
    }
    for folder_name, folder in extra_dirs.items():
        if folder.exists():
            for file_path in sorted(folder.iterdir()):
                if file_path.is_file():
                    files_to_pack.append((file_path, f"{folder_name}/{file_path.name}"))

    srt_content = await build_srt_content(project_dir, episode_num, beats)

    def build_zip_file() -> str:
        tmp_path = ""
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            tmp_path = tmp.name
            tmp.close()
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
                for file_path, arc_name in files_to_pack:
                    zf.write(file_path, arc_name)
                if srt_content:
                    zf.writestr(
                        f"{ep_tag}.srt",
                        srt_content,
                        compress_type=zipfile.ZIP_DEFLATED,
                    )
            return tmp_path
        except Exception:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
            raise

    build_task = asyncio.create_task(call_blocking(build_zip_file))
    tmp_path, cancellation = await wait_for_task_completion(build_task)
    if cancellation is not None:
        Path(tmp_path).unlink(missing_ok=True)
        raise cancellation
    try:
        return _TemporaryFileResponse(
            path=tmp_path,
            filename=f"{project_name}_{ep_tag}.zip",
            media_type="application/zip",
        )
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# 草图配色 + AI 颜色检测
# ---------------------------------------------------------------------------


@router.post("/projects/{project}/episodes/{episode_num}/sketches/assign-colors")
async def assign_sketch_colors(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """为本集出场身份和全局道具分配共享颜色。"""
    from novelvideo.generators.episode_optimizer import EpisodeOptimizer
    from novelvideo.generators.nanobanana_grid import _global_prop_marker_colors

    resolved = await _resolve_generation_project(project, user, required_role="editor")
    username = resolved.username
    project_name = resolved.project_name

    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(username, project_name)
    )

    beats = await store.get_beats_as_dicts(episode_num)
    if not beats:
        return {"ok": False, "error": f"No beats found for episode {episode_num}"}

    characters = store.get_all_characters()
    char_dicts = [
        {
            "name": c.name,
            "identities": [
                {"identity_id": id_.identity_id, "identity_name": id_.identity_name}
                for id_ in (c.identities or [])
            ],
        }
        for c in characters
    ]

    previous_colors = dict(store.get_sketch_colors(episode_num) or {})
    colors = EpisodeOptimizer.assign_sketch_colors(
        char_dicts,
        episode_beats=beats,
        existing_colors=previous_colors,
    )

    episode_obj = _episode_from_store_or_none(store, episode_num)
    runtime_prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, beats)
    previous_prop_marker_colors = _global_prop_marker_colors(
        beats,
        prop_menu=runtime_prop_menu,
        sketch_colors=previous_colors,
    )
    prop_marker_colors = _global_prop_marker_colors(
        beats,
        prop_menu=runtime_prop_menu,
        sketch_colors=colors,
        assign_missing=True,
    )
    if not colors and not prop_marker_colors:
        return {"ok": False, "error": "No identity or global prop markers found in beats"}

    try:
        if colors:
            await store.set_sketch_colors(episode_num, colors)
        if prop_marker_colors and runtime_prop_menu:
            for item in runtime_prop_menu:
                if not isinstance(item, dict):
                    continue
                prop_id = str(item.get("prop_id") or item.get("name") or "").strip()
                if prop_id in prop_marker_colors:
                    item["marker_color"] = prop_marker_colors[prop_id]
            # Marker colours are written back into the menu this request loaded
            # earlier, so a whole-row write here discards whatever scene, prop
            # or identity planning stored meanwhile.
            await store.patch_episode(episode_num, prop_menu=runtime_prop_menu)
    except Exception:
        pass

    previous_marker_colors = {
        **{f"identity:{key}": value for key, value in previous_colors.items()},
        **{f"prop:{key}": value for key, value in previous_prop_marker_colors.items()},
    }
    current_marker_colors = {
        **{f"identity:{key}": value for key, value in colors.items()},
        **{f"prop:{key}": value for key, value in prop_marker_colors.items()},
    }
    should_clean_sketches = _color_assignment_requires_full_sketch_clean(
        previous_marker_colors,
        current_marker_colors,
    )
    if should_clean_sketches:
        from novelvideo.utils.path_resolver import PathResolver

        output_dir = resolved.output_dir
        PathResolver(output_dir, episode_num).clean_sketches()

    return {
        "ok": True,
        "data": {
            "colors": colors,
            "count": len(colors),
            "prop_colors": prop_marker_colors,
            "prop_count": len(prop_marker_colors),
        },
    }


@router.post("/projects/{project}/episodes/{episode_num}/sketches/detect-identities")
async def detect_sketch_identities(
    project: str,
    episode_num: int,
    user: dict = Depends(get_api_user),
):
    """AI 视觉识别草图中出现的身份/道具颜色标记。"""
    from novelvideo.agents.global_video_optimizer import detect_identities_by_ai
    from novelvideo.generators.grid_splitter import combine_to_grid
    from novelvideo.generators.nanobanana_grid import _global_prop_marker_colors
    from novelvideo.models import (
        NO_CHARACTER_MARKER,
        NO_PROP_MARKER,
        real_detected_identities,
        real_detected_props,
        split_detected_marker_keys,
    )

    resolved = await _resolve_generation_project(project, user, required_role="editor")
    username = resolved.username
    project_name = resolved.project_name

    store = (
        await make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await make_sqlite_store(username, project_name)
    )

    beats = await store.get_beats_as_dicts(episode_num)
    if not beats:
        return {"ok": False, "error": f"No beats found for episode {episode_num}"}

    color_map = dict(store.get_sketch_colors(episode_num) or {})
    script_data_for_fallback = None
    if not color_map:
        try:
            script_data_for_fallback = await store.get_script_as_dict(episode_num)
            color_map = dict((script_data_for_fallback or {}).get("sketch_colors") or {})
        except Exception:
            script_data_for_fallback = None
    if not color_map:
        return {"ok": False, "error": "No sketch colors assigned. Call assign-colors first"}

    episode_obj = _episode_from_store_or_none(store, episode_num)
    runtime_prop_menu = await _runtime_prop_menu_with_global_props(store, episode_obj, beats)
    if not runtime_prop_menu:
        if script_data_for_fallback is None:
            try:
                script_data_for_fallback = await store.get_script_as_dict(episode_num)
            except Exception:
                script_data_for_fallback = None
        runtime_prop_menu = list((script_data_for_fallback or {}).get("prop_menu") or [])
    prop_color_map = _global_prop_marker_colors(
        beats,
        prop_menu=runtime_prop_menu,
        sketch_colors=color_map,
    )

    # 反转: "#HEX COLOR_NAME" → marker_id
    color_identity_map = {v: k for k, v in color_map.items()}
    color_identity_map.update({v: k for k, v in prop_color_map.items()})

    # 收集草图文件
    project_dir = resolved.project_dir
    sketches_dir = project_dir / "sketches" / f"ep{episode_num:03d}"
    frame_items: list[tuple[int, str]] = []
    known_beats = {
        int(b.get("beat_number", 0)) for b in beats if int(b.get("beat_number", 0) or 0) > 0
    }
    beat_pattern = re.compile(r"beat_(\d+)\.(png|jpg)$", re.IGNORECASE)
    if sketches_dir.exists():
        for candidate in sorted(sketches_dir.iterdir()):
            if not candidate.is_file():
                continue
            match = beat_pattern.match(candidate.name)
            if not match:
                continue
            beat_number = int(match.group(1))
            if known_beats and beat_number not in known_beats:
                continue
            frame_items.append((beat_number, str(candidate)))

    if not frame_items:
        return {"ok": False, "error": "No sketches found"}

    def _grid_shape(count: int) -> tuple[int, int]:
        if count <= 1:
            return 1, 1
        if count <= 4:
            return 2, 2
        if count <= 9:
            return 3, 3
        if count <= 16:
            return 4, 4
        return 5, 5

    grid_dir = project_dir / "grids" / f"ep{episode_num:03d}" / "sketch"
    grid_dir.mkdir(parents=True, exist_ok=True)

    usage_meter = get_usage_meter()
    ctx = getattr(resolved, "ctx", None)
    project_id = str(getattr(ctx, "project_id", "") or "")
    reservation = await usage_meter.reserve_feature_start_credits(
        user_id=_requester_user_id_for_billing(resolved, user),
        feature_key=AI_IDENTITY_DETECTION_FEATURE_KEY,
        product_surface="mainline",
        project_id=project_id,
        resource_kind="sketch",
        task_type=AI_IDENTITY_DETECTION_TASK_TYPE,
        metadata={
            "source": "sync_api",
            "endpoint": "detect_sketch_identities",
            "episode": episode_num,
            "sketch_count": len(frame_items),
        },
        require_price_rule=True,
        require_positive_cost=True,
    )
    reservation_id = str(reservation.get("id") or "")
    billing_metadata: dict[str, Any] = {
        "model_call_credit_policy": MODEL_CALL_CREDIT_POLICY_FEATURE_INCLUDED,
        "feature_key": AI_IDENTITY_DETECTION_FEATURE_KEY,
        "source": "sync_api",
    }
    if reservation_id:
        billing_metadata.update(
            {
                "feature_credit_reservation_id": reservation_id,
                "feature_credit_charge_id": reservation_id,
                "feature_credit_cost": str(reservation.get("cost") or 0),
            }
        )

    detections: dict[int, list[str]] = {}
    try:
        usage_meter.set_llm_usage_context(
            _requester_user_id_for_billing(resolved, user),
            project_id=project_id,
            resource_kind="sketch",
            billing_metadata=billing_metadata,
        )
        # 视觉模型对「一次表格里出 N 条结构化结果」不稳定：实测 12 个 panel 起就会
        # 出现 500 / 结构化校验失败，9 个（3x3）稳定成功。所以按 9 个一批，并且每个
        # 网格先重试，仍失败就二分继续拆小后合并，只有最小粒度（单 panel）都失败才
        # 向上抛错，把「整集识别失败」收窄成极少数单 beat 失败。
        batch_size = 9
        detection_attempts = 2

        async def _detect_panels(
            items: list[tuple[int, str]],
            *,
            label: str,
        ) -> dict[int, list[str]]:
            rows, cols = _grid_shape(len(items))
            grid_path = grid_dir / f"_ai_detect_grid_{rows}x{cols}_{label}.png"
            combine_to_grid([path for _, path in items], grid_path, rows=rows, cols=cols)
            last_error: Exception | None = None
            for attempt in range(1, detection_attempts + 1):
                try:
                    result = await detect_identities_by_ai(
                        sketch_image_paths=[str(grid_path)],
                        color_identity_map=color_identity_map,
                        total_beats=len(items),
                    )
                    return {int(key): list(value or []) for key, value in (result or {}).items()}
                except Exception as error:  # noqa: BLE001 - 退化为更小批次继续识别
                    last_error = error
                    logger.warning(
                        "AI 出场身份识别失败（grid=%s panels=%s attempt=%s）: %s",
                        label,
                        len(items),
                        attempt,
                        error,
                    )
            if len(items) <= 1:
                raise last_error if last_error is not None else RuntimeError(
                    "AI 出场身份识别失败"
                )
            half = len(items) // 2
            left = await _detect_panels(items[:half], label=f"{label}_a")
            right = await _detect_panels(items[half:], label=f"{label}_b")
            merged = dict(left)
            for panel_index, marker_ids in right.items():
                merged[panel_index + half] = marker_ids
            return merged

        for batch_idx in range(0, len(frame_items), batch_size):
            batch = frame_items[batch_idx : batch_idx + batch_size]
            try:
                batch_result = await _detect_panels(
                    batch, label=f"part{batch_idx // batch_size + 1}"
                )
            except Exception as error:
                failed_beats = "、".join(
                    str(beat_number) for beat_number, _ in sorted(batch)
                )
                raise RuntimeError(
                    f"AI 识别失败（beat {failed_beats}）: {error}"
                ) from error
            ordered_batch = sorted(batch, key=lambda item: item[0])
            for panel_index, marker_ids in (batch_result or {}).items():
                if 1 <= panel_index <= len(ordered_batch):
                    beat_number = ordered_batch[panel_index - 1][0]
                    detections[beat_number] = list(marker_ids or [])

        for beat_number, _path in frame_items:
            detections.setdefault(beat_number, [])

        characters = store.get_all_characters()
        identity_detections: dict[int, list[str]] = {}
        prop_detections: dict[int, list[str]] = {}
        allowed_prop_ids = set(prop_color_map)
        for beat_number, keys in detections.items():
            det_ids, det_props = split_detected_marker_keys(
                keys,
                beats,
                characters,
                allowed_prop_ids=allowed_prop_ids,
            )
            identity_detections[beat_number] = det_ids or [NO_CHARACTER_MARKER]
            prop_detections[beat_number] = det_props or [NO_PROP_MARKER]

        # 持久化
        await store.set_beat_detected_identities(episode_num, identity_detections)
        await store.set_beat_detected_props(episode_num, prop_detections)

    except Exception as e:
        if reservation_id:
            try:
                await usage_meter.settle_cancelled_feature_credit_reservation(
                    reservation_id,
                    metadata={
                        "source": "sync_api",
                        "endpoint": "detect_sketch_identities",
                        "episode": episode_num,
                        "error": str(e),
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to settle interrupted AI identity detection feature credit reservation"
                )
        return {"ok": False, "error": f"AI detection failed: {e}"}
    finally:
        usage_meter.clear_llm_usage_context()

    if reservation_id:
        try:
            await usage_meter.settle_feature_credit_reservation(
                reservation_id,
                action="confirm",
                metadata={
                    "source": "sync_api",
                    "endpoint": "detect_sketch_identities",
                    "episode": episode_num,
                    "sketch_count": len(frame_items),
                    "detected_identity_count": sum(
                        len(real_detected_identities(v))
                        for v in identity_detections.values()
                    ),
                    "detected_prop_count": sum(
                        len(real_detected_props(v)) for v in prop_detections.values()
                    ),
                },
            )
        except Exception:
            logger.exception(
                "AI identity detection succeeded but credit confirmation remains pending"
            )

    # 转换 key 为字符串（JSON 兼容）
    str_identity_detections = {str(k): v for k, v in identity_detections.items()}
    str_prop_detections = {str(k): v for k, v in prop_detections.items()}
    total_ids = sum(len(real_detected_identities(v)) for v in identity_detections.values())
    total_props = sum(len(real_detected_props(v)) for v in prop_detections.values())

    return {
        "ok": True,
        "data": {
            "detections": str_identity_detections,
            "identity_detections": str_identity_detections,
            "prop_detections": str_prop_detections,
            "total_beats": len(beats),
            "total_identities": total_ids,
            "total_props": total_props,
            "review_message": (
                "AI 已完成出场身份/道具识别，请核对每个 beat；"
                "漏识别可在“更多”的出场身份/出场道具中补选。"
            ),
        },
    }


# Helper implementations live in a submodule (dynamic refs preserve
# monkeypatching); keep them importable here for handlers and tests.
from novelvideo.api.routes.generation_helpers import (  # noqa: E402, F401
    _single_render_mode_from_sketch,
    _resolve_generation_project,
    _requester_user_id_for_billing,
    _color_assignment_requires_full_sketch_clean,
    normalize_beat_indices,
    validate_beat_indices,
    _render_plan_feature_disabled,
    _resolve_render_image_selection,
    _resolve_sketch_image_selection,
    _image_selection_billing_metadata,
    _sketch_regen_billing_metadata,
    _render_regen_billing_metadata,
    _single_video_billing_metadata,
    _resolve_render_bool_setting,
    _render_settings_payload,
    _sketch_settings_payload,
    _plan_entry_to_dict,
    _plan_to_dicts,
    _parse_grid_beat_numbers,
    _safe_grid_token,
    _uploaded_grid_filename,
    _safe_grids_file,
    _find_pool_grid_entry,
    _task_limit_reason,
    _log_partial_dispatch_rejection,
    _task_limit_rejection,
    _custom_render_plan_error,
    _read_uploaded_rgb_image,
    _register_uploaded_pool_image,
    _runtime_prop_menu_with_global_props,
    _prop_marker_colors_from_menu,
    _episode_from_store_or_none,
    _build_character_map,
    _validate_seedance_pro_dialogue_only,
    _is_seedance2_backend,
    _is_happyhorse_backend,
    _is_grok_video_backend,
    _seedance2_api_resolution,
    _seedance2_model_from_backend,
    _seedance2_resolution_options_for_backend,
    _seedance2_resolution_for_backend,
    _happyhorse_resolution_for_backend,
    _happyhorse_ratio_for_backend,
    _grok_video_resolution_for_backend,
    _grok_video_ratio_for_backend,
    _seedance2_initial_prompt,
    _legacy_video_prompt_for_mode,
    _missing_video_prompt_error,
    _seedance2_request_config_overrides,
    _merge_seedance2_request_config,
    _api_audio_duration_seconds,
    _prepare_seedance2_api_beat,
    _prepare_happyhorse_api_beat,
    _prepare_grok_video_api_beat,
    _seedance2_asset_status_payload,
    _seedance2_returned_last_frame_status_payload,
    _seedance2_voice_status_payload,
    _seedance2_panel_context,
    _seedance2_status_response,
    _api_video_backend_options,
    _sketch_regen_queue_key,
    _is_react_sketch_regen_queue_items,
    _react_sketch_regen_queues,
    _sketch_regen_queue_payload,
    _image_generation_guard_payload,
    _collect_audio_prereq_errors,
    _audio_generation_plan,
    _audio_billing_payload,
    _voice_prereq_error_response,
    _global_optimize_billable_beat_numbers,
    _canonical_sketch_path,
    _canonical_sketch_url,
    _director_control_scope,
    _director_control_payload,
    _director_overlay_beat_context,
    _director_same_scene_beats,
    _director_overlay_payload,
    _director_overlay_status_payload,
    _decode_png_data_url,
    _director_control_frame_export_payload,
    _episode_beat_from_resolution,
    _beat_scene_name,
    _api_background_reference_url_builder,
    _api_background_anchor_url_builder,
    _background_anchors_payload,
)
