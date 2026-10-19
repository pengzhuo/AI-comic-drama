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
round enqueues every *ready* missing scene/prop image (they are independent, and
the task keys are idempotent), while gaps whose ``master`` anchor has not landed
yet — and gaps already tried in this run — are skipped. It can be switched off
with ``DRAMACLAW_AUTO_ASSET_REFERENCES``.

The one deliberate exception is ``coloring`` (sketch identity/prop detection):
it is a synchronous, credit-billed vision call with no task type, so it does not
fit the coordinator's task bookkeeping and is still driven by the agent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from novelvideo.pipeline_status import (
    beat_seedance2_prompt_ready,
    character_needs_portrait,
    compute_pipeline_status,
)
from novelvideo.task_backend.client import enqueue_project_task
from novelvideo.task_state import get_task_manager
from novelvideo.utils.path_resolver import compute_identity_path, compute_portrait_path

DEFAULT_EPISODE_TARGET = 10
DEFAULT_COMPOSE_RESOLUTION = "720x1280"
# ``SingleVideoRequest`` validates ``batch_size <= 9``; the deterministic path
# ships the same chunk size so a 40-beat episode goes out in the same batches the
# workbench tool would send.
MAX_SINGLE_VIDEO_BATCH = 9
#: First frames are dispatched in chunks. The runtime caps ``batch_size`` at 9 and
#: a whole episode is ~40 beats, so 9 means "one round per 9 renders" instead of
#: 13 rounds of 3. A beat rejected by upstream moderation still only costs the
#: retry of its own render (the batch is re-derived from what is still missing),
#: and the signature carries the chunk, so the next call after this chunk
#: completes reads as progress rather than a retry.
AUTO_FIRST_FRAME_CHUNK = 9
#: 2.0 提示词一条 beat 一个任务（任务是协调器记账的抓手）。每条提示词只吃这条
#: beat 自己的数据 + 下一条 beat 的首尾帧信息，彼此没有先后依赖——真正需要按顺序
#: 传下去的是 ``global_optimize_video`` 的上一镜提示词链，那一步在这之前已经按 beat
#: 串行跑完了。所以一轮发 ``AUTO_SEEDANCE2_PROMPT_BATCH`` 条：任务键按
#: (task_type, episode, beat_num) 幂等，窗口重叠不会重复生成；一次只发一条会把
#: 39 条提示词排成 39 个前后相接的 LLM 往返（实测 ~32s/条 ≈ 21min）。
AUTO_SEEDANCE2_PROMPT_BATCH = 4


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
    """Write the next beats' Seedance 2.0 final prompts (one task per beat).

    Seedance 2.0 will not render a beat whose 2.0 prompt was never composed, and
    composing it is an LLM round-trip per beat. Dispatching one beat at a time as
    a real task keeps the coordinator's task bookkeeping intact (a taskless
    continuation would drift into the "no new task" pause).

    提示词之间**没有**先后依赖（见 ``AUTO_SEEDANCE2_PROMPT_BATCH``），所以一轮发
    满一批：窗口 = 「还没就位的最前面 N 条」，已经发过的按任务键/签名跳过、继续往后
    看，所以在飞的那几条不会挡住后面的 beat，失败过的那一条也不会把整段卡死。
    """

    beats = await store.get_beats_as_dicts(episode)
    dispatched: tuple[str | None, str | None] | None = None
    first_skipped: str | None = None
    pending = 0
    for beat in sorted(beats, key=lambda item: int(item.get("beat_number") or 0)):
        beat_num = int(beat.get("beat_number") or 0)
        if beat_num <= 0 or beat_seedance2_prompt_ready(beat):
            continue
        if pending >= AUTO_SEEDANCE2_PROMPT_BATCH:
            break
        pending += 1
        result = await _enqueue_once(
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
        if result[0] is not None and dispatched is None:
            dispatched = result
        elif first_skipped is None:
            first_skipped = result[1]
    if dispatched is not None:
        return dispatched
    return None, first_skipped


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
    """补齐项目里缺失的场景 / 道具参考图：**一轮把就绪的缺口都发出去**。

    关卡排在「脚本生成」与「草图生成」之间：草图只吃场景图、首帧同时吃场景图与
    道具图，所以这些图必须先存在。存量项目与后期新增的场景变体都按「缺哪张补
    哪张」处理。

    原先每轮只入队一个缺口，缺口之间被迫串行；它们彼此独立、没有先后依赖，串行
    只是白白拉长前置阶段。任务键按 (task_type, episode, scope) 幂等，同一条缺口
    重复入队不会产生重复生成。

    整批并行只适用于**真正独立**的缺口：同一个场景里 ``reverse_master`` /
    ``spatial_layout`` / 360 全景都以 ``master`` 为视觉锚，master 还没落盘就发出去
    只会立刻失败（或把全景降级成文生想象）。所以同一轮里每个场景先只发 ``master``，
    等它落盘后的下一轮再发依赖它的那几张——跨场景照旧整批并行，同一场景内只有两波，
    而不再把注定的失败发出去（见 :func:`gap_dependency_ready`）。

    已尝试过的缺口仍然跳过，让同一个坏条目不至于挡住其余条目；缺口全部记账之后
    这一阶段自然交出控制权。

    payload 逐字段复刻 REST 端点（``scenes.py`` / ``props.py``），不改模型、网关
    与生成参数。
    """

    from novelvideo.asset_references import (
        asset_reference_gaps_for_store,
        gap_dependency_ready,
    )

    gaps = await asset_reference_gaps_for_store(store, project_dir)
    if not gaps:
        return None, None
    pending = [
        gap
        for gap in gaps
        if gap.signature not in attempted and gap_dependency_ready(project_dir, gap)
    ]
    if not pending:
        return None, None

    first_task_type: str | None = None
    for gap in pending:
        dispatched = await _enqueue_asset_reference(ctx, store, project_dir, gap)
        if dispatched is not None and first_task_type is None:
            first_task_type = dispatched
    if first_task_type is None:
        return None, None
    # 返回值保持原来的形状（首个缺口 + 它的签名）：「已尝试过的缺口本轮跳过、
    # 全部尝试过就交给 agent」的终止语义因此完全不变——签名逐个记账，缺口全部
    # 记完自然收敛，坏条目也不会把这一阶段拖成死循环。
    return first_task_type, pending[0].signature


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
            "timeout_seconds": 900,
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

    **一轮一块（单飞）**：就绪窗口是「缺首帧的 beat 取前 9 个」，而帧是在块跑完
    时才落盘的，所以窗口会随帧落盘整体右移——[18..26] 里 18 号补上后，下一次算
    出来的是 [19..27]，区间变了签名就不是「同一块」，重叠派发会把 19-26 再渲染
    一遍（同一批 beat 两份任务重复生成，后落盘的那份覆盖先前那份）。块内并发由
    ``NANOBANANA_REGEN_CONCURRENCY`` 提供，所以这里要求同时只有一块在飞：这一块
    终止后（含失败），下一轮按最新的就绪集合继续派发，失败 beat 的重试语义不变。
    """

    from novelvideo.api.routes.generation import (
        _single_render_mode_from_sketch,
        enqueue_selected_regen_task,
    )
    from novelvideo.utils.path_resolver import PathResolver

    if await _has_in_flight_task(ctx, "selected_regen"):
        return None, None

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


async def _in_flight_beat_numbers(ctx: Any, task_type: str) -> set[int]:
    """Return beat numbers with a task of ``task_type`` still in flight.

    「就绪」是看产物算的，而产物在任务跑完之前一直不存在——所以「首帧已就绪、还没
    视频」的 beat 在它的视频任务正在生成时**依然算就绪**，会一直占着批次头部，让后
    面真正新增的就绪 beat 永远排不进来。剔除在飞的 beat，批次头部才会向前走，
    各阶段才能真正重叠。
    """

    from novelvideo.task_state import ACTIVE_PROJECT_TASK_STATUSES

    def _collect() -> set[int]:
        try:
            tasks = get_task_manager().list_tasks_for_project(ctx)
        except Exception:  # noqa: BLE001 - 查不到就别剔，宁可多派一次（幂等）
            return set()
        numbers: set[int] = set()
        for task in tasks:
            if str(getattr(task, "task_type", "") or "") != task_type:
                continue
            if str(getattr(task, "status", "") or "") not in ACTIVE_PROJECT_TASK_STATUSES:
                continue
            try:
                number = int(getattr(task, "beat_num", 0) or 0)
            except (TypeError, ValueError):
                continue
            if number > 0:
                numbers.add(number)
        return numbers

    return await asyncio.to_thread(_collect)


async def _has_in_flight_task(ctx: Any, task_type: str) -> bool:
    """该项目是否已有 ``task_type`` 的任务在飞（「同一阶段单飞」的闸门）。

    查不到任务列表时不拦（宁可多派一次：同一条缺口/同一块按 scope 幂等，重复入队
    不会重复生成），比起因为一次查询抖动把整条流水线停住更安全。
    """

    from novelvideo.task_state import ACTIVE_PROJECT_TASK_STATUSES

    def _collect() -> bool:
        try:
            tasks = get_task_manager().list_tasks_for_project(ctx)
        except Exception:  # noqa: BLE001
            return False
        return any(
            str(getattr(task, "task_type", "") or "") == task_type
            and str(getattr(task, "status", "") or "") in ACTIVE_PROJECT_TASK_STATUSES
            for task in tasks
        )

    return await asyncio.to_thread(_collect)


async def _dispatch_single_videos(
    ctx: Any,
    store: Any,
    episode: int,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    """Enqueue the ready beats' videos in the same payload the REST route uses.

    "Ready" means the promoted first frame exists and the beat still has no
    video, **and no video task for it is already in flight** — otherwise the
    beats being generated keep occupying the head of the list and newly finished
    frames never get dispatched. The chunk is capped at the runtime's own
    ``batch_size <= 9`` limit, and the signature carries the chunk so the next
    call (after this chunk completes) is recognised as progress rather than a
    retry.
    """

    from novelvideo.api.schemas import SingleVideoRequest
    from novelvideo.api.routes.generation import enqueue_single_video_task
    from novelvideo.utils.path_resolver import PathResolver

    beats = await store.get_beats_as_dicts(episode)
    if not beats:
        return None, None
    paths = PathResolver(str(project_dir), episode)
    in_flight = await _in_flight_beat_numbers(ctx, "single_video")
    ready = sorted(
        beat_num
        for beat_num in (int(beat.get("beat_number") or 0) for beat in beats)
        if beat_num > 0
        and beat_num not in in_flight
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
