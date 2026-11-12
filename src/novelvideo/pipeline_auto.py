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
import hashlib
import logging
import time
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

logger = logging.getLogger(__name__)

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
#: 同时在飞的**首帧块数**（块内的图片并发另由 ``NANOBANANA_REGEN_CONCURRENCY``
#: 决定，默认 3），所以真实并发是「块数 × 块内并发」。实测这一段是全流水线最大
#: 的热点：1656 beats 逐块串行铺了 12.7h 墙钟，而每块 9 张图里只有 3 路真的在飞。
#: 上限留在 3 是因为 CE 的 default lane 全局只有 6 个执行槽位（见
#: ``task_backend/limits.py``），块数再往上抬会被 lane-full 挡回。
AUTO_FIRST_FRAME_STAGE = "first_frames"
AUTO_FIRST_FRAME_CONCURRENCY_ENV = "FIRST_FRAMES_CONCURRENCY"
AUTO_FIRST_FRAME_CONCURRENCY_DEFAULT = 3
#: 2.0 提示词一条 beat 一个任务（任务是协调器记账的抓手）。每条提示词只吃这条
#: beat 自己的数据 + 下一条 beat 的首尾帧信息，彼此没有先后依赖——真正需要按顺序
#: 传下去的是 ``global_optimize_video`` 的上一镜提示词链，那一步在这之前已经按 beat
#: 串行跑完了。所以一轮发 ``AUTO_SEEDANCE2_PROMPT_BATCH`` 条：任务键按
#: (task_type, episode, beat_num) 幂等，窗口重叠不会重复生成；一次只发一条会把
#: 39 条提示词排成 39 个前后相接的 LLM 往返（实测 ~32s/条 ≈ 21min）。
AUTO_SEEDANCE2_PROMPT_BATCH = 4
#: 剧本写作「任务级按集并发」的上限：同项目同时在飞的**按集预取**任务数（每集身份
#: 规划 + 本集剧本）。两者都按集彼此独立（每集只吃本集的身份/场景/道具菜单），过去
#: 调度器一轮只派 ``current_episode`` 一集、等它写完才轮下一集，而单集剧本是一次
#: 「若干分钟~数小时」的 LLM 往返（护花铃_2 单集 1656 行实测 ~5.5h，含失败重派），
#: 多集项目串起来就是十小时级。默认 3；设 1 即回到逐集串行的老行为（逃生舱）。
AUTO_SCRIPT_WRITER_STAGE = "script_writer"
AUTO_SCRIPT_WRITER_CONCURRENCY_ENV = "SCRIPT_WRITER_CONCURRENCY"
AUTO_SCRIPT_WRITER_CONCURRENCY_DEFAULT = 3
#: 每集身份规划。它同样只按 ``current_episode`` 派发，而「本集身份还没规划」正是
#: 剧本的前置闸门（``pipeline_status`` 的 ``has_identity_plan`` 与
#: ``enqueue_script_writer_task`` 都看本集 ``identity_ids``），所以要和剧本一起预取。
AUTO_IDENTITY_PLANNER_STAGE = "identity_planner"


async def dispatch_next_stage(
    *,
    ctx: Any,
    username: str,
    store: Any,
    episode: int,
    target_episodes: int = 0,
    attempted: set[str] | None = None,
    voice_policy: str = "",
) -> tuple[str | None, str | None]:
    """Enqueue the next deterministically-dispatchable stage.

    Returns ``(task_type, signature)``. ``task_type`` is ``None`` when the
    stage has no deterministic handler *or* its signature was already attempted
    in this run — the caller should then fall back to the agent for that step.
    The signature lets the caller persist "already tried" across restarts so a
    stage whose artifact never materialises cannot loop forever.

    ``voice_policy`` 是该 run 的声线选择（``system``／``custom``／空）。空档由闸门
    先停在 ``voice_setup`` 让用户拍板；只有 ``system`` 才在这里确定性入队。
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

    # 按集预取：本集剧本已经轮到过之后，趁每一轮调度把后面几集的**按集前置链**
    # （每集身份规划 → 本集剧本）也发出去（见 :func:`_prefetch_episode_chain`）。
    # 探到可预取的一集就先返回它，否则照旧走下面的逐阶段阶梯——主路径永远先照顾本集。
    prefetched = await _prefetch_episode_chain(
        ctx=ctx,
        store=store,
        project_dir=project_dir,
        attempted=attempted,
        current_episode=current_episode,
        current_script_done=bool((data.get("episode_status") or {}).get("script")),
    )
    if prefetched[0] is not None:
        return prefetched

    if next_step == "screenplay_preprocess":
        return await _enqueue_once(
            ctx,
            attempted,
            task_type="screenplay_preprocess",
            queue_kind="default",
            episode=0,
            signature="dispatch:screenplay_preprocess",
            payload={"output_dir": str(project_dir)},
        )
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
        # 默认按“每集≈目标时长”分集；结构化项目仍走确定性的章节映射。
        state_dir = getattr(ctx, "state_dir", ctx.output_dir)
        from novelvideo.knowledge_pipeline import is_structured_pipeline

        mode = "chapters"
        if not is_structured_pipeline(state_dir):
            mode = "duration"
            try:
                from novelvideo.project_config import (
                    load_project_config_from_state_dir,
                )

                pcfg = load_project_config_from_state_dir(
                    state_dir,
                    username=getattr(ctx, "owner_username", None)
                    or getattr(ctx, "requester_username", None),
                    project=getattr(ctx, "project_name", None),
                )
                mode = str(pcfg.get("episode_planning_mode") or "duration")
            except Exception:
                mode = "duration"
        return await _enqueue_once(
            ctx,
            attempted,
            task_type="build_episodes",
            queue_kind="default",
            episode=0,
            signature="dispatch:build_episodes",
            payload={
                "config": {"target_episodes": target, "planning_mode": mode},
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
            task_type=AUTO_IDENTITY_PLANNER_STAGE,
            queue_kind="default",
            episode=current_episode,
            signature=f"dispatch:{AUTO_IDENTITY_PLANNER_STAGE}:{current_episode}",
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
    if next_step == "voice_setup":
        return await _dispatch_voice_setup(
            ctx, current_episode, project_dir, attempted, voice_policy
        )
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
    """Enqueue the episode's script through the REST payload builder.

    本集之外还允许同时在飞几集（``SCRIPT_WRITER_CONCURRENCY``，见
    :func:`_prefetch_episode_chain`）；这一函数只负责「本集」这一份，签名按集去重。
    """

    from novelvideo.api.routes.scripts import enqueue_script_writer_task

    signature = f"dispatch:{AUTO_SCRIPT_WRITER_STAGE}:{episode}"
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
    return AUTO_SCRIPT_WRITER_STAGE, signature


def _script_writer_concurrency() -> int:
    """``SCRIPT_WRITER_CONCURRENCY``（热调参）——按集预取同时在飞的任务数上限。"""

    from novelvideo.shared.stage_runtime import stage_int

    return max(
        1,
        stage_int(
            AUTO_SCRIPT_WRITER_STAGE,
            AUTO_SCRIPT_WRITER_CONCURRENCY_ENV,
            "concurrency",
            AUTO_SCRIPT_WRITER_CONCURRENCY_DEFAULT,
        ),
    )


async def _in_flight_task_episodes(
    ctx: Any, task_types: tuple[str, ...]
) -> dict[str, set[int]]:
    """本项目里还在飞的那几种任务，各自派给了哪几集。"""

    from novelvideo.task_state import ACTIVE_PROJECT_TASK_STATUSES

    wanted = set(task_types)

    def _collect() -> dict[str, set[int]]:
        found: dict[str, set[int]] = {task_type: set() for task_type in wanted}
        try:
            tasks = get_task_manager().list_tasks_for_project(ctx)
        except Exception:  # noqa: BLE001 - 查不到就不拦：同集还有签名去重兜底
            return found
        for task in tasks:
            task_type = str(getattr(task, "task_type", "") or "")
            if task_type not in wanted:
                continue
            if str(getattr(task, "status", "") or "") not in ACTIVE_PROJECT_TASK_STATUSES:
                continue
            try:
                number = int(getattr(task, "episode", 0) or 0)
            except (TypeError, ValueError):
                continue
            if number > 0:
                found[task_type].add(number)
        return found

    return await asyncio.to_thread(_collect)


def _last_dispatched_episode(attempted: set[str], stage: str) -> int:
    """``recovery_keys`` 里这个阶段派过的最大集号 —— 按集预取的游标。"""

    numbers: list[int] = []
    for key in attempted:
        text = str(key)
        if not text.startswith(f"dispatch:{stage}:"):
            continue
        try:
            numbers.append(int(text.rsplit(":", 1)[1]))
        except (TypeError, ValueError):
            continue
    return max(numbers) if numbers else 0


async def _prefetch_episode_chain(
    ctx: Any,
    store: Any,
    project_dir: Path,
    attempted: set[str],
    current_episode: int,
    current_script_done: bool,
) -> tuple[str | None, str | None]:
    """把后面几集的**按集前置链**也发出去：先是每集身份规划，再是本集剧本。

    剧本写作过去是逐集串行：调度器一轮只派 ``current_episode`` 一集，等它写完才轮到
    下一集。单集剧本是一次「若干分钟~数小时」的 LLM 往返（护花铃_2 单集 1656 行实测
    ≈5.5h 墙钟），多集项目串起来就是十小时级；而各集剧本彼此独立（只吃本集的身份 /
    场景 / 道具菜单），没有先后依赖。

    但「本集身份还没规划」正是剧本的前置闸门：``pipeline_status`` 的
    ``has_identity_plan`` 看的是本集 ``identity_ids``，``enqueue_script_writer_task``
    缺它时直接返回 ``identity_plan_required``，而身份规划本身也只按 ``current_episode``
    派发。所以这里把两者一起预取：候选集的剧本派不动就先把它的身份规划派出去，等规划
    回来再由下一轮调度接力派它的剧本。身份规划只做规划、不生成图片（图片仍按集在成片
    流程里生成），所以不会抢图片 lane。

    只有本集剧本**已经轮到过**（已派发过一次或已经写好）才预取，所以调度主路径永远
    先照顾本集；老行为（逐集串行）只是并发=1 的特例。游标取**剧本**已派签名里的最大
    集号：一集剧本只派一次、按集号往前推，身份规划也不发散到窗口之外。并发上限同时数
    身份规划与剧本两种在飞任务，因此扇出天然有界。

    「还没剧本」与 ``pipeline_status`` 同口径（``_all_or_empty``）：**没有分镜也算没
    剧本**——剧本阶段本身会把原文切成行、建成 beats（护花铃_2 的 1656 条就是它建的），
    所以不能把「beats 还空着」当成「无事可做」而跳过。
    """

    limit = _script_writer_concurrency()
    if limit <= 1:
        return None, None
    current_signature = f"dispatch:{AUTO_SCRIPT_WRITER_STAGE}:{current_episode}"
    if not (current_script_done or current_signature in attempted):
        return None, None

    in_flight = await _in_flight_task_episodes(
        ctx, (AUTO_SCRIPT_WRITER_STAGE, AUTO_IDENTITY_PLANNER_STAGE)
    )
    in_flight_scripts = in_flight[AUTO_SCRIPT_WRITER_STAGE]
    in_flight_identities = in_flight[AUTO_IDENTITY_PLANNER_STAGE]
    if len(in_flight_scripts) + len(in_flight_identities) >= limit:
        return None, None

    from novelvideo.api.routes.scripts import enqueue_script_writer_task
    from novelvideo.pipeline_status import _beat_has_script_content

    cursor = max(
        _last_dispatched_episode(attempted, AUTO_SCRIPT_WRITER_STAGE), current_episode
    )
    for candidate in range(cursor + 1, cursor + limit + 1):
        script_signature = f"dispatch:{AUTO_SCRIPT_WRITER_STAGE}:{candidate}"
        if script_signature in attempted or candidate in in_flight_scripts:
            continue
        if store.get_episode(candidate) is None:
            continue
        beats = await store.get_beats_as_dicts(candidate)
        if beats and all(_beat_has_script_content(beat) for beat in beats):
            # 剧本已经写好的集不能因为签名被阶段重试清掉就整集重写（那是数小时的
            # LLM 往返）。
            continue
        result = await enqueue_script_writer_task(
            ctx=ctx,
            store=store,
            episode_num=candidate,
            output_dir=str(project_dir),
        )
        if result.get("ok"):
            return AUTO_SCRIPT_WRITER_STAGE, script_signature
        if result.get("code") != "identity_plan_required":
            continue
        identity_signature = f"dispatch:{AUTO_IDENTITY_PLANNER_STAGE}:{candidate}"
        if identity_signature in attempted or candidate in in_flight_identities:
            continue
        return await _enqueue_once(
            ctx,
            attempted,
            task_type=AUTO_IDENTITY_PLANNER_STAGE,
            queue_kind="default",
            episode=candidate,
            signature=identity_signature,
            payload={"episode": candidate},
        )
    return None, None


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


def _first_frame_chunks(beat_numbers: Any) -> list[list[int]]:
    """按 beat 号把整集切成固定块——与「谁已经就绪」无关。

    固定块是「多块并发」的前提：块的签名只由它在集里的位置决定，所以一块在飞时
    另一块的计算结果不会跟着变，两块永远不会覆盖同一批 beat（旧版按「缺帧的
    beat 取前 N 个」切，窗口会随帧落盘整体右移，重叠派发必然重复渲染）。
    """

    ordered = sorted({int(number) for number in beat_numbers if int(number) > 0})
    size = max(1, int(AUTO_FIRST_FRAME_CHUNK))
    return [ordered[index : index + size] for index in range(0, len(ordered), size)]


def _first_frame_signature(
    episode: int, chunk: list[int], missing: list[int]
) -> str:
    """块的派发签名：``dispatch:selected_regen:{ep}:{首}-{尾}:{缺哪几个}``。

    光有区间不够：块跑完通常会留下极少数被上游挑掉的 beat，缺集变了必须能再派
    一次（签名里带上缺集就自洽了——同一缺集只派一次，成功补上后缺集消失，仍失败
    则由协调器的失败重试/降级兜底）。也刻意与 ``_block_shot_and_continue`` 登记的
    单镜跳过键 ``dispatch:selected_regen:{ep}:{beat}`` 区分开（后者没有区间）。
    """

    span = f"{chunk[0]}-{chunk[-1]}"
    detail = "all" if list(missing) == list(chunk) else ",".join(str(n) for n in missing)
    return f"dispatch:selected_regen:{episode}:{span}:{detail}"


def _blocked_first_frame_beats(attempted: Any, episode: int) -> set[int]:
    """``_block_shot_and_continue`` 登记过「这一镜已降级、别再派」的 beat 号。"""

    prefix = f"dispatch:selected_regen:{episode}:"
    blocked: set[int] = set()
    for key in attempted:
        text = str(key)
        if not text.startswith(prefix):
            continue
        tail = text[len(prefix) :]
        if tail.isdigit():
            blocked.add(int(tail))
    return blocked


async def _dispatch_first_frames(
    ctx: Any,
    store: Any,
    episode: int,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    """Enqueue the ready beats' first frames (Render) via the REST payload.

    "Ready" means the beat has a sketch and still no first frame — mirroring
    ``_dispatch_single_videos``. Chunks are **fixed beat ranges** of
    ``AUTO_FIRST_FRAME_CHUNK`` beats, and several chunks may be in flight at once
    (``FIRST_FRAMES_CONCURRENCY``, default 3): first frames are independent of one
    another, so nothing but upstream throughput forced the old strict
    single-flight, and that single-flight was the pipeline's biggest piece of
    wall clock (9 renders per ~190s chunk = 12.7h for 1656 beats, while only
    ``NANOBANANA_REGEN_CONCURRENCY`` = 3 of them were ever in flight).

    Only the beats of the chunk that are *still* missing are shipped, so a chunk
    that already produced most of its frames is re-dispatched as the few beat
    left over (a chunk-level retry, not a re-render of the whole chunk). That is
    also what lets a beat rejected by upstream moderation fall back to the
    coordinator's per-shot handling: a lone leftover beat becomes a one-beat
    task, which carries ``beat_num``, so the failure can be localised and either
    repaired or degraded to its sketch instead of stalling the run.
    """

    from novelvideo.api.routes.generation import (
        _single_render_mode_from_sketch,
        enqueue_selected_regen_task,
    )
    from novelvideo.shared.stage_runtime import stage_int
    from novelvideo.utils.path_resolver import PathResolver

    concurrency = max(
        1,
        stage_int(
            AUTO_FIRST_FRAME_STAGE,
            AUTO_FIRST_FRAME_CONCURRENCY_ENV,
            "concurrency",
            AUTO_FIRST_FRAME_CONCURRENCY_DEFAULT,
        ),
    )
    if await _count_in_flight_tasks(ctx, "selected_regen") >= concurrency:
        return None, None

    beats = await store.get_beats_as_dicts(episode)
    if not beats:
        return None, None
    paths = PathResolver(str(project_dir), episode)
    blocked = _blocked_first_frame_beats(attempted, episode)

    def _ready(beat_num: int) -> bool:
        return (
            beat_num not in blocked
            and paths.sketch(beat_num).is_file()
            and not paths.first_frame_for_video(beat_num).exists()
        )

    for chunk in _first_frame_chunks(
        int(beat.get("beat_number") or 0) for beat in beats
    ):
        missing = [beat_num for beat_num in chunk if _ready(beat_num)]
        if not missing:
            continue
        signature = _first_frame_signature(episode, chunk, missing)
        if signature in attempted:
            continue
        result = await enqueue_selected_regen_task(
            ctx=ctx,
            store=store,
            username=ctx.requester_username,
            project_name=ctx.project_name,
            output_dir=str(project_dir),
            episode_num=episode,
            beat_indices=missing,
            # The batch's own sketch aspect decides the render mode; the client
            # default would silently letterbox a 16:9 sketch as 2:3.
            mode_key=_single_render_mode_from_sketch(str(project_dir), episode, [missing[0]]),
            # 单镜块带上 beat_num：失败报文才定位得到「是哪一镜」，协调器才能
            # 改写提示词重试或把该镜降级成草图顶替（否则只能把整条 run 停下来）。
            beat_num=missing[0] if len(missing) == 1 else None,
        )
        if not result.get("ok"):
            return None, None
        return "selected_regen", signature
    return None, None


async def _dispatch_voice_setup(
    ctx: Any,
    episode: int,
    project_dir: Path,
    attempted: set[str],
    voice_policy: str,
) -> tuple[str | None, str | None]:
    """系统声线准备：纯确定性任务，不必让捏导来写。

    ``voice_setup`` 早先没有确定性处理器，只能回退到 agent；LLM 在这一步常选错写
    任务（2026-11 现场：停在 ``voice_setup`` 时反复重出早已齐备的草图网格，每格 ~4
    分钟、每格都有 1 个 beat 触上游内容策略，白烧钱还占住阶段）。声线策略选
    ``system`` 时，「准备缺失的系统声线」本身就是一个确定性任务
    （``system_voice_setup``，与 agent 工具走同一条 runner，完成后回钩续跑），这里
    直接入队。

    ``custom`` 档不走这里：自定义音色要用户上传/录制，仍交给 agent 与工作台。
    """

    if str(voice_policy or "") != "system":
        return None, None
    return await _enqueue_once(
        ctx,
        attempted,
        task_type="system_voice_setup",
        queue_kind="default",
        episode=episode,
        signature=f"dispatch:system_voice_setup:{episode}",
        payload={
            "episode": episode,
            "output_dir": str(project_dir),
            "state_dir": str(getattr(ctx, "state_dir", "") or ""),
        },
    )


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


async def _count_in_flight_tasks(ctx: Any, task_type: str) -> int:
    """本项目里 ``task_type`` 尚在飞的任务数（「同阶段最多几路」的闸门）。

    查不到任务列表时返回 0（不拦）：宁可多派一次——同一块按签名幂等，重复入队
    不会重复生成——也不要因为一次查询抖动把整条流水线停住。
    """

    from novelvideo.task_state import ACTIVE_PROJECT_TASK_STATUSES

    def _collect() -> int:
        try:
            tasks = get_task_manager().list_tasks_for_project(ctx)
        except Exception:  # noqa: BLE001
            return 0
        return sum(
            1
            for task in tasks
            if str(getattr(task, "task_type", "") or "") == task_type
            and str(getattr(task, "status", "") or "") in ACTIVE_PROJECT_TASK_STATUSES
        )

    return await asyncio.to_thread(_collect)


async def _has_in_flight_task(ctx: Any, task_type: str) -> bool:
    """该项目是否已有 ``task_type`` 的任务在飞（「同一阶段单飞」的闸门）。

    查不到任务列表时不拦（宁可多派一次：同一条缺口/同一块按 scope 幂等，重复入队
    不会重复生成），比起因为一次查询抖动把整条流水线停住更安全。
    """

    return await _count_in_flight_tasks(ctx, task_type) > 0


#: 同一条 beat 在准备阶段（提示词/声线校验）被挡下多少次后暂时停止重试。
#: 协调器约 1.5s 一轮，无上限重试会让一条坏 beat 把整集卡成「一直在制作中」——
#: 线上实测 8 小时累计 ~2900 次拒绝、815 个 lane-full 失败任务、4350 次重开 SQLite，
#: 用户端只看到「一直在生成」却永远不出片。给一个有限预算 + 冷却；提示词/台词被
#: 改写（指纹变化）时立即重置，声线补传也最多等一个冷却周期就会自动恢复。
PREP_REJECTION_RETRY_LIMIT = 5
PREP_REJECTION_COOLDOWN_SECONDS = 1800.0

#: ``(project_dir, episode, beat_num)`` ->
#: ``(fingerprint, rejection_count, blocked_until_monotonic)``
_PREP_REJECTION_STATE: dict[tuple[str, int, int], tuple[str, int, float]] = {}


def _prep_rejection_fingerprint(beat: dict[str, Any]) -> str:
    """改动提示词/台词后指纹变化 → 立刻重新尝试这条 beat。"""

    parts = [
        str(beat.get("audio_type") or ""),
        str(beat.get("speaker") or ""),
        str(beat.get("narration") or ""),
        str(beat.get("narration_segment") or ""),
        str(beat.get("dialogue") or ""),
        str(beat.get("seedance2_config_json") or ""),
    ]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def _prep_rejection_in_cooldown(
    key: tuple[str, int, int],
    fingerprint: str,
    *,
    now: float | None = None,
) -> bool:
    """这条 beat 是否正处于「被拒冷却」中，应当跳过。"""

    entry = _PREP_REJECTION_STATE.get(key)
    if entry is None:
        return False
    recorded, _count, blocked_until = entry
    if recorded != fingerprint:
        # 提示词/台词已改写：视为新内容，立刻允许重试。
        _PREP_REJECTION_STATE.pop(key, None)
        return False
    clock = time.monotonic() if now is None else now
    return blocked_until > clock


def _note_prep_rejection(
    key: tuple[str, int, int],
    fingerprint: str,
    *,
    now: float | None = None,
) -> int:
    """记一次准备阶段拒绝，返回该 beat 的连续拒绝次数。"""

    clock = time.monotonic() if now is None else now
    entry = _PREP_REJECTION_STATE.get(key)
    if entry is None or entry[0] != fingerprint:
        _PREP_REJECTION_STATE[key] = (fingerprint, 1, 0.0)
        return 1
    _recorded, count, _blocked_until = entry
    count += 1
    blocked_until = (
        clock + PREP_REJECTION_COOLDOWN_SECONDS
        if count >= PREP_REJECTION_RETRY_LIMIT
        else 0.0
    )
    _PREP_REJECTION_STATE[key] = (entry[0], count, blocked_until)
    return count


def _clear_prep_rejection(key: tuple[str, int, int]) -> None:
    _PREP_REJECTION_STATE.pop(key, None)


async def _dispatch_single_videos(
    ctx: Any,
    store: Any,
    episode: int,
    project_dir: Path,
    attempted: set[str],
) -> tuple[str | None, str | None]:
    """Enqueue one ready beat's video in the same payload the REST route uses.

    "Ready" means the promoted first frame exists and the beat still has no
    video, **and no video task for it is already in flight** — otherwise the
    beats being generated keep occupying the head of the list and newly finished
    frames never get dispatched.

    Each call ships exactly one beat and returns that beat's own signature
    (``dispatch:single_video:{episode}:{beat}``). A per-beat signature keeps one
    rejected beat from marking its whole batch "attempted" and starving the
    others; beats already in ``attempted`` are skipped so the loop walks on to
    the next one. A beat whose enqueue comes back ``ok: False`` (prompt/voice
    validation) is *not* treated as dispatched, so it is retried instead of
    silently vanishing.
    """

    from novelvideo.api.schemas import SingleVideoRequest
    from novelvideo.api.routes.generation import enqueue_single_video_task
    from novelvideo.utils.path_resolver import PathResolver

    beats = await store.get_beats_as_dicts(episode)
    if not beats:
        return None, None
    beats_by_number = {int(beat.get("beat_number") or 0): beat for beat in beats}
    paths = PathResolver(str(project_dir), episode)
    in_flight = await _in_flight_beat_numbers(ctx, "single_video")
    ready = sorted(
        beat_num
        for beat_num in (int(beat.get("beat_number") or 0) for beat in beats)
        if beat_num > 0
        and beat_num not in in_flight
        and paths.first_frame_for_video(beat_num).exists()
        and not _single_video_has_trusted_clip(paths.video(beat_num))
    )
    if not ready:
        return None, None
    for beat_num in ready:
        # 签名按 beat 记：一批里某条 beat 被上游拒绝/校验挡下时，不能让同批其它
        # beat 跟着一起卡死（旧版用 ``{first}-{last}`` 整批签名，一条坏 beat 就把
        # 整批标成「已派发」而永不重试，别的 beat 明明能出片也发不出去）。每轮
        # 只发一条，已 attempted 的跳过；协调器 1.5s 一轮，一个 episode 也就十几秒。
        signature = f"dispatch:single_video:{episode}:{beat_num}"
        if signature in attempted:
            continue
        rejection_key = (str(project_dir), int(episode), int(beat_num))
        fingerprint = _prep_rejection_fingerprint(beats_by_number.get(beat_num) or {})
        if _prep_rejection_in_cooldown(rejection_key, fingerprint):
            # 已连续多次准备阶段拒绝：进入冷却，不再每轮空转重试（静默跳过）。
            continue
        result = await enqueue_single_video_task(
            ctx=ctx,
            store=store,
            username=ctx.requester_username,
            project_name=ctx.project_name,
            output_dir=str(project_dir),
            episode_num=episode,
            beat_num=beat_num,
            body=SingleVideoRequest(),
        )
        if not _single_video_enqueued(result):
            # 准备阶段就被挡下（提示词/声线校验等）：路由以 HTTP 200 +
            # ``{"ok": False}`` 返回，绝不能记成「已派发」，否则这条 beat 永不重试、
            # 还会连坐同批。留日志；连续被拒到上限后进入冷却，避免整集空转。
            count = _note_prep_rejection(rejection_key, fingerprint)
            detail = _single_video_dispatch_error(result)
            if count >= PREP_REJECTION_RETRY_LIMIT:
                logger.error(
                    "single_video dispatch rejected user=%s project=%s episode=%s "
                    "beat=%s error=%s（已连续 %d 次，暂停重试 %d 分钟；"
                    "改写提示词/台词或补传声线后自动恢复）",
                    ctx.requester_username,
                    ctx.project_name,
                    episode,
                    beat_num,
                    detail,
                    count,
                    int(PREP_REJECTION_COOLDOWN_SECONDS // 60),
                )
            else:
                logger.warning(
                    "single_video dispatch rejected user=%s project=%s episode=%s "
                    "beat=%s error=%s",
                    ctx.requester_username,
                    ctx.project_name,
                    episode,
                    beat_num,
                    detail,
                )
            continue
        _clear_prep_rejection(rejection_key)
        return "single_video", signature
    return None, None


def _single_video_has_trusted_clip(video_path: Path) -> bool:
    """该镜是否已有**可信**成片（工作台可据此跳过重渲）。

    降级兜底产物（本地静帧短片）与真成片同目录同文件名，光看文件存在会把兜底
    当成做完了、那一镜再也不会重生成。降级时会在产物旁挂 ``.degraded.json``
    标记（见 ``chat.director_auto._block_shot_and_continue``）：带标记的镜头
    此处判为「还得重渲」。被阻塞的镜头另有 dispatch 跳过签名挡着，所以这不会
    变成无限重渲；用户重做（``clear_blocked_shot``）摘掉标记和签名后才回到队列。
    """

    path = Path(video_path)
    if not path.exists():
        return False
    from novelvideo.generators.fallback_clip import has_degraded_marker

    return not has_degraded_marker(path)


def _single_video_enqueued(result: Any) -> bool:
    """Whether ``enqueue_single_video_task`` actually queued the render.

    The route reports preparation failures (prompt/voice validation, moderation…)
    as ``{"ok": False, "error": ...}`` with HTTP 200, so the payload — not the
    absence of an exception — decides whether the beat may be marked dispatched.
    """

    if isinstance(result, dict):
        return bool(result.get("ok", True))
    return True


def _single_video_dispatch_error(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("error") or result.get("detail") or "")
    return ""


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
