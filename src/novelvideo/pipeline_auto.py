"""Deterministic mainline auto-dispatch — the M5 one-click fast path.

Instead of spending one LLM turn per stage, this module reads the shared
``compute_pipeline_status`` and directly enqueues the next stage through the
same ``enqueue_project_task`` payloads the REST handlers use, so the fast path
and the product surface cannot drift silently.

Only stages whose payload is fully derivable from project state are handled.
Anything else returns ``None``; the coordinator then falls back to the
agent-driven continuation for that single step. This keeps the fast path
strictly additive — an unknown stage never blocks production.

Handled mainline stages: film-process-pack script processing, character/episode
building, portraits, identity plan/images, script, asset references (missing
scene/prop reference images), sketches, global video optimization, first frames
(Render), TTS, Seedance 2.0 prompts, per-beat video and episode compose.

Asset references is a blocking stage between ``script`` and ``sketches``: one
round enqueues exactly one missing scene/prop image so a single broken entry can
never stall the rest, and gaps already tried in this run are skipped. It can be
switched off with ``DRAMACLAW_AUTO_ASSET_REFERENCES``.

The one deliberate exception is ``coloring`` (sketch identity/prop detection):
it is a synchronous, credit-billed vision call with no task type, so it does not
fit the coordinator's task bookkeeping and is still driven by the agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from novelvideo.pipeline_status import (
    beat_seedance2_prompt_ready,
    character_needs_portrait,
    compute_pipeline_status,
)
from novelvideo.task_backend.client import enqueue_project_task
from novelvideo.utils.path_resolver import compute_identity_path, compute_portrait_path

DEFAULT_EPISODE_TARGET = 10
DEFAULT_COMPOSE_RESOLUTION = "720x1280"
# ``SingleVideoRequest`` validates ``batch_size <= 9``; the deterministic path
# ships the same chunk size so a 40-beat episode goes out in the same batches the
# workbench tool would send.
MAX_SINGLE_VIDEO_BATCH = 9
#: First frames are dispatched in smaller chunks on purpose. One beat can be
#: rejected by upstream content moderation while its neighbours render fine, and
#: a smaller chunk keeps that failure from discarding a whole batch's renders
#: when the coordinator has to retry the stage.
AUTO_FIRST_FRAME_CHUNK = 3


async def dispatch_next_stage(
    *,
    ctx: Any,
    username: str,
    store: Any,
    episode: int,
    target_episodes: int = 0,
    attempted: set[str] | None = None,
) -> tuple[str | None, str | None]:
    """Enqueue the next deterministically-dispatchable stage.

    Returns ``(task_type, signature)``. ``task_type`` is ``None`` when the
    stage has no deterministic handler *or* its signature was already attempted
    in this run — the caller should then fall back to the agent for that step.
    The signature lets the caller persist "already tried" across restarts so a
    stage whose artifact never materialises cannot loop forever.
    """

    attempted = attempted or set()
    project_dir = Path(ctx.output_dir)
    data = await compute_pipeline_status(
        store=store,
        username=username,
        project_name=ctx.project_name,
        project_dir=project_dir,
        ctx=ctx,
        episode=episode if episode > 0 else None,
    )
    next_step = str(data.get("next_step") or "")
    current_episode = int(data.get("current_episode") or episode or 1)

    if next_step == "script_process":
        return await _enqueue_once(
            ctx,
            attempted,
            task_type="script_process",
            queue_kind="default",
            episode=0,
            signature="dispatch:script_process",
            payload={"output_dir": str(project_dir)},
        )
    if next_step == "build_characters":
        return await _enqueue_once(
            ctx,
            attempted,
            task_type="build_characters",
            queue_kind="default",
            episode=0,
            signature="dispatch:build_characters",
            payload={"output_dir": str(project_dir)},
        )
    if next_step == "build_episodes":
        target = int(target_episodes or 0) or DEFAULT_EPISODE_TARGET
        return await _enqueue_once(
            ctx,
            attempted,
            task_type="build_episodes",
            queue_kind="default",
            episode=0,
            signature="dispatch:build_episodes",
            payload={
                "config": {"target_episodes": target, "planning_mode": "chapters"},
                "output_dir": str(project_dir),
                "state_dir": str(ctx.state_dir),
            },
        )
    if next_step == "portraits":
        return await _dispatch_portrait(ctx, store, project_dir, attempted)
    if next_step == "identity_plan":
        return await _enqueue_once(
            ctx,
            attempted,
            task_type="identity_planner",
            queue_kind="default",
            episode=current_episode,
            signature=f"dispatch:identity_planner:{current_episode}",
            payload={"episode": current_episode},
        )
    if next_step == "identity_images":
        return await _dispatch_identity_image(ctx, store, current_episode, project_dir, attempted)
    if next_step == "script_writer":
        return await _dispatch_script_writer(ctx, store, current_episode, project_dir, attempted)
    if next_step == "asset_references":
        return await _dispatch_asset_references(ctx, store, project_dir, attempted)
    if next_step == "sketch_generation":
        return await _dispatch_sketches(ctx, store, current_episode, project_dir, attempted)
    if next_step == "global_optimize_video":
        return await _dispatch_global_optimize(ctx, store, current_episode, project_dir, attempted)
    if next_step == "selected_regen":
        return await _dispatch_first_frames(ctx, store, current_episode, project_dir, attempted)
    if next_step == "tts":
        return await _dispatch_tts(ctx, store, current_episode, project_dir, attempted)
    if next_step == "seedance2_prompt":
        return await _dispatch_seedance2_prompt(ctx, store, current_episode, project_dir, attempted)
    if next_step == "single_video":
        return await _dispatch_single_videos(ctx, store, current_episode, project_dir, attempted)
    if next_step == "compose_episode":
        return await _dispatch_compose(ctx, store, current_episode, attempted)
    return None, None


async def _enqueue_once(
    ctx: Any,
    attempted: set[str],
    *,
    task_type: str,
    queue_kind: str,
    episode: int,
    signature: str,
    payload: dict[str, Any],
    scope: str | None = None,
    beat_num: int | None = None,
) -> tuple[str | None, str]:
    if signature in attempted:
        return None, signature
    await enqueue_project_task(
        ctx,
        task_type=task_type,
        product_surface="mainline",
        queue_kind=queue_kind,
        episode=episode,
        beat_num=beat_num,
        scope=scope,
        payload=payload,
    )
    return task_type, signature


def _character_image_payload_bits(username: str, project_name: str, image_role: str) -> tuple[str, dict]:
    """Mirror the character-image handlers' style/model/billing resolution."""

    from novelvideo.api.routes.characters import (
        _character_image_billing_metadata,
        _resolve_character_image_model,
    )
    from novelvideo.project_config import load_project_config

    config = load_project_config(username, project_name)
    style = config.get("visual_style", "chinese_period_drama")
    model = _resolve_character_image_model(username, project_name, None)
    billing = _character_image_billing_metadata(model, image_role=image_role)
    return style, {"style": style, "model": model, "billing": billing}


async def _dispatch_portrait(
    ctx: Any,
    store: Any,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    for char in store.get_all_characters():
        if not character_needs_portrait(char):
            continue
        if compute_portrait_path(project_dir, char.name):
            continue
        scope = f"character:{char.name}:portrait"
        _, bits = _character_image_payload_bits(
            ctx.requester_username, ctx.project_name, "character"
        )
        return await _enqueue_once(
            ctx,
            attempted,
            task_type="character_portrait",
            queue_kind="default",
            episode=0,
            scope=scope,
            signature=f"dispatch:character_portrait:{char.name}",
            payload={
                "mode": "portrait",
                "task_type": "character_portrait",
                "character_name": char.name,
                "scope": scope,
                "output_dir": str(project_dir),
                **bits,
            },
        )
    return None, None


async def _dispatch_identity_image(
    ctx: Any,
    store: Any,
    episode: int,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    target_episode = store.get_episode(episode)
    identity_ids = set(getattr(target_episode, "identity_ids", []) or [])
    if not identity_ids:
        return None, None
    for char in store.get_all_characters():
        for identity in getattr(char, "identities", []) or []:
            if identity.identity_id not in identity_ids:
                continue
            if compute_identity_path(project_dir, char.name, identity.identity_name):
                continue
            scope = f"character:{char.name}:identity:{identity.identity_name}"
            _, bits = _character_image_payload_bits(
                ctx.requester_username, ctx.project_name, "identity"
            )
            return await _enqueue_once(
                ctx,
                attempted,
                task_type="identity_image",
                queue_kind="default",
                episode=0,
                scope=scope,
                signature=f"dispatch:identity_image:{char.name}:{identity.identity_id}",
                payload={
                    "mode": "identity_image",
                    "task_type": "identity_image",
                    "character_name": char.name,
                    "identity_id": identity.identity_id,
                    "identity_name": identity.identity_name,
                    "scope": scope,
                    "output_dir": str(project_dir),
                    **bits,
                },
            )
    return None, None


async def _dispatch_seedance2_prompt(
    ctx: Any,
    store: Any,
    episode: int,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    """Write the next beat's Seedance 2.0 final prompt.

    Seedance 2.0 will not render a beat whose 2.0 prompt was never composed, and
    composing it is an LLM round-trip per beat. Dispatching one beat at a time as
    a real task keeps the coordinator's task bookkeeping intact (a taskless
    continuation would drift into the "no new task" pause).
    """

    beats = await store.get_beats_as_dicts(episode)
    for beat in sorted(beats, key=lambda item: int(item.get("beat_number") or 0)):
        beat_num = int(beat.get("beat_number") or 0)
        if beat_num <= 0 or beat_seedance2_prompt_ready(beat):
            continue
        return await _enqueue_once(
            ctx,
            attempted,
            task_type="seedance2_prompt",
            queue_kind="default",
            episode=episode,
            beat_num=beat_num,
            signature=f"dispatch:seedance2_prompt:{episode}:{beat_num}",
            payload={
                "output_dir": str(project_dir),
                "episode": episode,
                "beat_num": beat_num,
            },
        )
    return None, None


async def _dispatch_script_writer(
    ctx: Any,
    store: Any,
    episode: int,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    """Enqueue the episode's script through the REST payload builder."""

    from novelvideo.api.routes.scripts import enqueue_script_writer_task

    signature = f"dispatch:script_writer:{episode}"
    if signature in attempted:
        return None, signature
    result = await enqueue_script_writer_task(
        ctx=ctx,
        store=store,
        episode_num=episode,
        output_dir=str(project_dir),
    )
    if not result.get("ok"):
        return None, None
    return "script_writer", signature


async def _dispatch_asset_references(
    ctx: Any,
    store: Any,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    """补齐项目里缺失的场景 / 道具参考图，每轮只入队一个任务。

    关卡排在「脚本生成」与「草图生成」之间：草图只吃场景图、首帧同时吃场景图与
    道具图，所以这些图必须先存在。存量项目与后期新增的场景变体都按「缺哪张补
    哪张」处理。

    payload 逐字段复刻 REST 端点（``scenes.py`` / ``props.py``），不改模型、网关
    与生成参数；已尝试过的缺口跳过，让同一个坏条目不至于挡住其余条目。
    """

    from novelvideo.asset_references import asset_reference_gaps_for_store

    gaps = await asset_reference_gaps_for_store(store, project_dir)
    if not gaps:
        return None, None
    for gap in gaps:
        if gap.signature in attempted:
            continue
        dispatched = await _enqueue_asset_reference(ctx, store, project_dir, gap)
        if dispatched is not None:
            return dispatched, gap.signature
    return None, None


async def _enqueue_asset_reference(
    ctx: Any,
    store: Any,
    project_dir: Path,
    gap: Any,
) -> str | None:
    """按缺口类型复刻对应 REST 端点的入队 payload。"""

    from novelvideo.api.routes.props import _project_style
    from novelvideo.task_scopes import (
        prop_reference_asset_scope,
        scene_reference_asset_scope,
        stage_asset_scope,
    )

    output_dir = str(project_dir)
    style = _project_style(ctx.requester_username, ctx.project_name)

    if gap.kind == "scene":
        from novelvideo.api.routes.scenes import _scene_reference_billing_metadata

        scope = scene_reference_asset_scope(gap.name, gap.detail)
        await enqueue_project_task(
            ctx,
            task_type="scene_reference_asset",
            product_surface="mainline",
            queue_kind="default",
            episode=0,
            scope=scope,
            payload={
                "scene_name": gap.name,
                "kind": gap.detail,
                "model": "",
                "style": style,
                "output_dir": output_dir,
                "billing": _scene_reference_billing_metadata(""),
            },
        )
        return "scene_reference_asset"

    if gap.kind == "pano":
        from novelvideo.api.routes.scenes import (
            _scene_360_description,
            _scene_pano_billing_metadata,
        )
        from novelvideo.utils.path_resolver import compute_scene_master_path

        scene = await store.get_scene(gap.name)
        if scene is None:
            return None
        source = "master" if compute_scene_master_path(project_dir, gap.name) else "text"
        step = f"pano_from_{source}"
        params: dict[str, Any] = {
            "description": _scene_360_description(scene),
            "style": style,
            "timeout_seconds": 1800,
        }
        scope = stage_asset_scope(gap.name, step)
        await enqueue_project_task(
            ctx,
            task_type="scene_pano_generation",
            product_surface="mainline",
            queue_kind="world",
            episode=0,
            scope=scope,
            payload={
                "scene_name": gap.name,
                "step": step,
                "params": params,
                "project_dir": output_dir,
                "billing": _scene_pano_billing_metadata(params),
            },
        )
        return "scene_pano_generation"

    from novelvideo.api.routes.model_credits import _fixed_image_billing_params
    from novelvideo.generators.nanobanana_prop import (
        _prop_reference_image_source,
        resolve_prop_reference_image_model,
    )

    _, selected_model = _prop_reference_image_source("")
    pricing_model = selected_model or resolve_prop_reference_image_model()
    await enqueue_project_task(
        ctx,
        task_type="prop_reference_asset",
        product_surface="mainline",
        queue_kind="default",
        episode=0,
        scope=prop_reference_asset_scope(gap.name),
        payload={
            "prop_name": gap.name,
            "style": style,
            "model": "",
            "output_dir": output_dir,
            "billing": {
                "pricing_kind": "image",
                "pricing_model": pricing_model,
                "pricing_params": _fixed_image_billing_params(
                    "prop_reference", model=pricing_model
                ),
            },
        },
    )
    return "prop_reference_asset"


async def _dispatch_sketches(
    ctx: Any,
    store: Any,
    episode: int,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    """Enqueue every sketch grid for the episode.

    Sketch generation is the stage an agent mis-read as a single-image render
    (and paused the run on), so it is dispatched straight from project state.
    The auto path also runs the colour assignment this stage requires
    (``ensure_colors``), because generation refuses to start without it.
    """

    from novelvideo.api.routes.generation import enqueue_sketch_grid_tasks
    from novelvideo.api.schemas import SketchGenerateRequest

    signature = f"dispatch:sketch_generation:{episode}"
    if signature in attempted:
        return None, signature
    result = await enqueue_sketch_grid_tasks(
        ctx=ctx,
        store=store,
        username=ctx.requester_username,
        project_name=ctx.project_name,
        output_dir=str(project_dir),
        episode_num=episode,
        body=SketchGenerateRequest(grid_index=-1),
        # The auto path assigns the colours the stage requires; the REST route
        # deliberately keeps its "call assign-colors first" contract.
        ensure_colors=True,
    )
    if not result.get("ok"):
        return None, None
    return "sketch_grid_generation", signature


async def _dispatch_global_optimize(
    ctx: Any,
    store: Any,
    episode: int,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    """Enqueue the episode's global video optimization."""

    from novelvideo.api.routes.generation import enqueue_global_optimize_video_task

    signature = f"dispatch:global_optimize_video:{episode}"
    if signature in attempted:
        return None, signature
    result = await enqueue_global_optimize_video_task(
        ctx=ctx,
        store=store,
        output_dir=str(project_dir),
        episode_num=episode,
    )
    if not result.get("ok"):
        return None, None
    return "global_optimize_video", signature


async def _dispatch_first_frames(
    ctx: Any,
    store: Any,
    episode: int,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    """Enqueue the ready beats' first frames (Render) via the REST payload.

    "Ready" means the beat has a sketch and still no first frame — mirroring
    ``_dispatch_single_videos``. The chunk is capped at
    ``AUTO_FIRST_FRAME_CHUNK`` (well below the runtime's ``batch_size <= 9``
    limit) so a beat rejected by upstream moderation only costs the retry of a
    few renders, and the signature carries the chunk, so the next call after
    this chunk completes reads as progress rather than a retry.
    """

    from novelvideo.api.routes.generation import (
        _single_render_mode_from_sketch,
        enqueue_selected_regen_task,
    )
    from novelvideo.utils.path_resolver import PathResolver

    beats = await store.get_beats_as_dicts(episode)
    if not beats:
        return None, None
    paths = PathResolver(str(project_dir), episode)
    ready = sorted(
        beat_num
        for beat_num in (int(beat.get("beat_number") or 0) for beat in beats)
        if beat_num > 0
        and paths.sketch(beat_num).is_file()
        and not paths.first_frame_for_video(beat_num).exists()
    )
    if not ready:
        return None, None
    batch = ready[:AUTO_FIRST_FRAME_CHUNK]
    signature = f"dispatch:selected_regen:{episode}:{batch[0]}-{batch[-1]}"
    if signature in attempted:
        return None, signature
    result = await enqueue_selected_regen_task(
        ctx=ctx,
        store=store,
        username=ctx.requester_username,
        project_name=ctx.project_name,
        output_dir=str(project_dir),
        episode_num=episode,
        beat_indices=batch,
        # The batch's own sketch aspect decides the render mode; the client
        # default would silently letterbox a 16:9 sketch as 2:3.
        mode_key=_single_render_mode_from_sketch(str(project_dir), episode, [batch[0]]),
    )
    if not result.get("ok"):
        return None, None
    return "selected_regen", signature


async def _dispatch_tts(
    ctx: Any,
    store: Any,
    episode: int,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    """Enqueue the episode's speech batch through the REST payload builder."""

    from novelvideo.api.routes.generation import enqueue_audio_generation_task

    signature = f"dispatch:audio_generation:{episode}"
    if signature in attempted:
        return None, signature
    result = await enqueue_audio_generation_task(
        ctx=ctx,
        store=store,
        username=ctx.requester_username,
        project_name=ctx.project_name,
        output_dir=str(project_dir),
        state_dir=str(getattr(ctx, "state_dir", "") or ""),
        episode_num=episode,
    )
    if not result.get("ok"):
        return None, None
    return "audio_generation_indextts2", signature


async def _dispatch_single_videos(
    ctx: Any,
    store: Any,
    episode: int,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    """Enqueue the ready beats' videos in the same payload the REST route uses.

    "Ready" means the promoted first frame exists and the beat still has no
    video; beats whose frame never materialised are left to the agent (it can
    explain what is missing) instead of being dispatched into a guaranteed
    failure. The chunk is capped at the runtime's own ``batch_size <= 9`` limit,
    and the signature carries the chunk so the next call (after this chunk
    completes) is recognised as progress rather than a retry.
    """

    from novelvideo.api.schemas import SingleVideoRequest
    from novelvideo.api.routes.generation import enqueue_single_video_task
    from novelvideo.utils.path_resolver import PathResolver

    beats = await store.get_beats_as_dicts(episode)
    if not beats:
        return None, None
    paths = PathResolver(str(project_dir), episode)
    ready = sorted(
        beat_num
        for beat_num in (int(beat.get("beat_number") or 0) for beat in beats)
        if beat_num > 0
        and paths.first_frame_for_video(beat_num).exists()
        and not paths.video(beat_num).exists()
    )
    if not ready:
        return None, None
    batch = ready[:MAX_SINGLE_VIDEO_BATCH]
    signature = f"dispatch:single_video:{episode}:{batch[0]}-{batch[-1]}"
    if signature in attempted:
        return None, signature
    for beat_num in batch:
        await enqueue_single_video_task(
            ctx=ctx,
            store=store,
            username=ctx.requester_username,
            project_name=ctx.project_name,
            output_dir=str(project_dir),
            episode_num=episode,
            beat_num=beat_num,
            body=SingleVideoRequest(),
        )
    return "single_video", signature


async def _dispatch_compose(
    ctx: Any,
    store: Any,
    episode: int,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    beats = await store.get_beats_as_dicts(episode)
    if not beats:
        return None, None
    return await _enqueue_once(
        ctx,
        attempted,
        task_type="compose_episode",
        queue_kind="ffmpeg",
        episode=episode,
        signature=f"dispatch:compose_episode:{episode}",
        payload={
            "beats": beats,
            "add_subtitles": True,
            "add_bgm": False,
            "episode": episode,
            "output_dir": str(ctx.output_dir),
            "resolution": _compose_resolution(ctx),
        },
    )


def _compose_resolution(ctx: Any) -> str:
    from novelvideo.project_config import load_project_config

    config = load_project_config(ctx.requester_username, ctx.project_name)
    return str(config.get("video_resolution") or DEFAULT_COMPOSE_RESOLUTION)
