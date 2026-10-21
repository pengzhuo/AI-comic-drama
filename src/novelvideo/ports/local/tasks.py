"""Local CE task and cancellation port implementations."""

from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
import logging
import os
import random
import threading
import time
from typing import Any

from novelvideo.authz_retry import retry_authz_read
from novelvideo.ports import get_cancellation_store
from novelvideo.ports.authz import AuthzError, detach_authz_error
from novelvideo.ports.tasks import QueuedTask, cancel_key, display_metadata_for_task
from novelvideo.project_context import require_project_home_node
from novelvideo.task_backend.envelope import (
    InvalidTaskEnvelope,
    TaskAuthorityFault,
    TaskAuthorityUnavailable,
)
from novelvideo.task_backend.limits import (
    GlobalLaneQueueLimitExceeded,
    global_lane_concurrency,
    global_lane_queue_limit,
    project_lane_effective_active_limit,
)
from novelvideo.task_backend.queues import QUEUE_KINDS, normalize_queue_kind
from novelvideo.task_backend.run_core import run_project_task_core_sync
from novelvideo.task_backend.subprocesses import kill_task_processes
from novelvideo.task_state import ACTIVE_PROJECT_TASK_STATUSES, get_task_manager

logger = logging.getLogger(__name__)

_AUTHZ_RETRY_MAX_RETRIES = 5
_AUTHZ_RETRY_BASE_DELAY = 2.0
_AUTHZ_RETRY_CAP_DELAY = 60.0
_AUTHZ_RETRY_SLEEP = asyncio.sleep
_AUTHZ_RETRY_RANDOM = random.random


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


#: 「DB 说在途、本进程却没有对应 job」的任务行,超过它才允许回收/落失败。给并发
#: 投递留出 reserve → 入队之间的窗口;真僵尸(接力入队落在已关闭的临时事件循环上)
#: 的时间尺度远大于它。见 ``_reclaim_orphan_state`` 与 ``reap_orphan_tasks``。
INLINE_TASK_ORPHAN_GRACE_SECONDS = _float_env(
    "DRAMACLAW_INLINE_TASK_ORPHAN_GRACE_SECONDS", 120.0
)
#: 兜底清扫周期(秒);<=0 关闭。
INLINE_TASK_REAP_INTERVAL_SECONDS = _float_env(
    "DRAMACLAW_INLINE_TASK_REAP_INTERVAL_SECONDS", 300.0
)
INLINE_TASK_ORPHAN_MESSAGE = "任务未能进入执行队列,已中断,请重新发起"


def _task_updated_at_age_seconds(updated_at: str | None) -> float | None:
    """任务行 ``updated_at`` 距今秒数;解析不了就返回 None(按「不知道」处理)。"""
    raw = str(updated_at or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


@dataclass
class _InlineLaneJob:
    envelope: dict[str, Any]
    ctx: Any
    manager: Any
    run_task_id: str
    metadata: dict[str, Any]

    @property
    def project_id(self) -> str:
        return str(self.envelope.get("project_id") or "")


@dataclass
class _InlineLane:
    name: str
    concurrency: int
    queue_limit: int
    executor: ThreadPoolExecutor
    queued: deque[_InlineLaneJob] = field(default_factory=deque)
    active: int = 0
    last_started_project_id: str | None = None


class InlineTaskBackend:
    def __init__(self, *, producer=None, consumer=None) -> None:
        self._producer = producer
        self._consumer = consumer
        self._background_tasks: set[Any] = set()
        #: lane job 只允许派到的那个事件循环,由 ``bind_event_loop`` 在 startup 绑定。
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        #: 本进程持有在飞 job(排队中或执行中)的 task_id。
        self._live_task_ids: set[str] = set()
        self._live_lock = threading.Lock()
        #: 本进程入队过的项目任务库,给低频兜底清扫用(有序去重)。
        self._known_task_dbs: dict[str, None] = {}
        self._reaper_started = False
        self._reaper_lock = threading.Lock()
        self._lanes: dict[str, _InlineLane] = {
            lane: _InlineLane(
                name=lane,
                concurrency=global_lane_concurrency(lane),
                queue_limit=global_lane_queue_limit(lane),
                executor=ThreadPoolExecutor(
                    max_workers=global_lane_concurrency(lane),
                    thread_name_prefix=f"inline-{lane}",
                ),
            )
            for lane in sorted(QUEUE_KINDS)
        }

    async def enqueue_project_task(
        self,
        ctx,
        *,
        task_type: str,
        product_surface: str,
        queue_kind: str = "default",
        episode: int = 0,
        beat_num: int | None = None,
        scope: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> QueuedTask:
        require_project_home_node(ctx, operation="enqueue project task")
        manager = get_task_manager()
        payload = payload or {}
        lane_name = normalize_queue_kind(queue_kind)
        metadata = {
            "backend": "inline",
            "queue_kind": lane_name,
            "project_id": ctx.project_id,
            "product_surface": product_surface,
            **display_metadata_for_task(task_type, payload),
        }
        project_lane_limit = project_lane_effective_active_limit(
            lane_name,
            eligible_user_count=1,
        )
        self._register_task_db(ctx)
        state, reserved = self._reserve_project_task(
            manager,
            ctx,
            task_type,
            episode,
            beat_num=beat_num,
            scope=scope,
            lane_name=lane_name,
            project_lane_limit=project_lane_limit,
        )
        if not reserved and state.status in ACTIVE_PROJECT_TASK_STATUSES:
            # 「DB 说在途、本进程却没有 job」的假排队行必须回收:它会被幂等锁一直
            # 挡住,之后每次重试都被原样吞掉,UI 永远停在「处理中 0%」。
            if self._reclaim_orphan_state(
                manager,
                ctx,
                task_type,
                episode,
                beat_num=beat_num,
                scope=scope,
                state=state,
            ):
                state, reserved = self._reserve_project_task(
                    manager,
                    ctx,
                    task_type,
                    episode,
                    beat_num=beat_num,
                    scope=scope,
                    lane_name=lane_name,
                    project_lane_limit=project_lane_limit,
                )
        if not reserved and state.status in ACTIVE_PROJECT_TASK_STATUSES:
            existing_metadata = state.metadata or {}
            return QueuedTask(
                task_state=state,
                backend=str(existing_metadata.get("backend") or "inline"),
                queue=None,
                celery_id=None,
            )

        envelope = {
            "project_id": ctx.project_id,
            "requester_user_id": ctx.requester_user_id,
            "task_type": task_type,
            "episode": episode,
            "beat_num": beat_num,
            "scope": scope,
            "queue_kind": lane_name,
            "payload": payload,
        }
        if self._producer is not None:
            authz_failure: AuthzError | None = None
            envelope_failed = False
            try:
                signed = await self._producer.sign_top_level(
                    user_id=ctx.requester_user_id,
                    root_task_id=state.task_id,
                    task_type=task_type,
                    project_id=ctx.project_id,
                    payload={
                        "episode": episode,
                        "beat_num": beat_num,
                        "scope": scope,
                        "queue_kind": lane_name,
                        "payload": payload,
                    },
                )
            except AuthzError as exc:
                authz_failure = exc
                signed = None
            except Exception:
                envelope_failed = True
                signed = None
            if authz_failure is not None or envelope_failed or signed is None:
                safe_code = (
                    authz_failure.code
                    if authz_failure is not None
                    else "TASK_ENVELOPE_INVALID"
                )
                manager.fail_task_for_project(
                    ctx,
                    task_type,
                    episode,
                    beat_num=beat_num,
                    scope=scope,
                    error=(
                        str(authz_failure)
                        if authz_failure is not None
                        else "invalid task envelope"
                    ),
                    metadata={"error_code": safe_code},
                    expected_task_id=state.task_id,
                )
                if authz_failure is not None:
                    raise detach_authz_error(authz_failure) from None
                raise InvalidTaskEnvelope from None
            envelope["task_envelope_v2"] = signed.to_dict()
        self._submit_lane_job(
            _InlineLaneJob(
                envelope=envelope,
                ctx=ctx,
                manager=manager,
                run_task_id=state.task_id,
                metadata=metadata,
            )
        )
        manager.update_progress_for_project(
            ctx,
            task_type,
            episode,
            beat_num=beat_num,
            scope=scope,
            progress=0.0,
            current_task="任务已进入队列",
            metadata=metadata,
            status="queued",
            expected_task_id=state.task_id,
        )
        return QueuedTask(task_state=state, backend="inline")

    def bind_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定 lane job 必须运行的那个应用事件循环。

        任务 runner 在 worker 线程里执行,而且各自再套一层 ``asyncio.run``
        (见 ``run_project_task_core_sync``),所以 runner 内部接力入队时「当前
        循环」是一条马上要关闭的临时循环。若直接 ``create_task``,job 会被挂到
        那条临时循环上,runner 一返回循环即被回收、job 被静默取消——任务行却永远
        停在 ``queued``。派单必须跨线程回到这里绑定的主循环。
        """
        self._owner_loop = loop

    def _owner_loop_or_current(self) -> asyncio.AbstractEventLoop:
        loop = self._owner_loop
        if loop is not None and not loop.is_closed():
            return loop
        current = asyncio.get_running_loop()
        if loop is None and threading.current_thread() is threading.main_thread():
            # 没有 startup 钩子的嵌入场景(测试):主线程首次派单时认领。
            self._owner_loop = current
        return current

    def _mark_live(self, task_id: str) -> None:
        with self._live_lock:
            self._live_task_ids.add(task_id)

    def _clear_live(self, task_id: str) -> None:
        with self._live_lock:
            self._live_task_ids.discard(task_id)

    def live_task_ids(self) -> set[str]:
        """本进程在飞 job 的快照,给假排队判定与兜底清扫用。"""
        with self._live_lock:
            return set(self._live_task_ids)

    @staticmethod
    def _reserve_project_task(
        manager: Any,
        ctx: Any,
        task_type: str,
        episode: int,
        *,
        beat_num: int | None,
        scope: str | None,
        lane_name: str,
        project_lane_limit: int,
    ) -> tuple[Any, bool]:
        return manager.reserve_task_for_project(
            ctx,
            task_type,
            episode,
            beat_num=beat_num,
            scope=scope,
            metadata={},
            queue_kind=lane_name,
            project_lane_limit=project_lane_limit,
        )

    def _reclaim_orphan_state(
        self,
        manager: Any,
        ctx: Any,
        task_type: str,
        episode: int,
        *,
        beat_num: int | None,
        scope: str | None,
        state: Any,
    ) -> bool:
        """清掉「DB 说在途、本进程没有 job」的行,让调用方重新 reserve。

        只有既有活 job 之外、又超过宽限期的行才动,所以仍在 ``reserve`` 途中的
        并发投递不会被误伤。
        """
        if str(getattr(state, "task_id", "") or "") in self.live_task_ids():
            return False
        age = _task_updated_at_age_seconds(getattr(state, "updated_at", ""))
        if age is None or age < INLINE_TASK_ORPHAN_GRACE_SECONDS:
            return False
        logger.warning(
            "reclaiming orphan inline task: project=%s task_type=%s task_id=%s "
            "status=%s age_seconds=%s",
            getattr(ctx, "project_id", ""),
            task_type,
            getattr(state, "task_id", ""),
            getattr(state, "status", ""),
            None if age is None else round(age, 1),
        )
        try:
            cleared = manager.clear_task_for_project(
                ctx, task_type, episode, beat_num=beat_num, scope=scope
            )
        except Exception:  # noqa: BLE001 - 回收失败就退回幂等语义
            logger.exception("failed to clear orphan inline task row")
            return False
        return bool(cleared)

    def _register_task_db(self, ctx: Any) -> None:
        """记下本进程碰过的项目任务库,并确保兜底清扫线程在跑。"""
        try:
            from novelvideo.task_state import get_project_task_db_path_for_context

            path = str(get_project_task_db_path_for_context(ctx).resolve())
        except Exception:  # noqa: BLE001 - 记账失败不影响投递
            return
        if path in self._known_task_dbs:
            return
        self._known_task_dbs[path] = None
        self._start_orphan_reaper()

    def _start_orphan_reaper(self) -> None:
        if INLINE_TASK_REAP_INTERVAL_SECONDS <= 0:
            return
        with self._reaper_lock:
            if self._reaper_started:
                return
            self._reaper_started = True
        threading.Thread(
            target=self._orphan_reaper_loop,
            name="inline-task-reaper",
            daemon=True,
        ).start()

    def _orphan_reaper_loop(self) -> None:
        while True:
            # 下限保护:间隔被配成 0 或负数时也不至于变成忙循环。
            time.sleep(max(INLINE_TASK_REAP_INTERVAL_SECONDS, 30.0))
            try:
                self.reap_orphan_tasks()
            except Exception:  # noqa: BLE001 - 兜底线程绝不能死
                logger.exception("inline orphan task reap failed")

    def reap_orphan_tasks(self) -> int:
        """把「DB 说在途、本进程没有 job」的行落为 failed(低频兜底)。

        服务重启遗留的在途行由 ``_sweep_stale_tasks_on_connection`` 按进程启动时刻
        回收;这一条补的是**进程存活期间**产生的假排队行(它晚于进程启动,原有的
        清扫看不到它)。
        """
        manager = get_task_manager()
        live = self.live_task_ids()
        reaped = 0
        for path in list(self._known_task_dbs):
            if not os.path.isfile(path):
                # 项目已删除:SQLite 连接会把这个文件重新造出来,直接忘掉它。
                self._known_task_dbs.pop(path, None)
                continue
            try:
                changed = manager.sweep_orphan_inline_tasks(
                    path,
                    live_task_ids=live,
                    grace_seconds=INLINE_TASK_ORPHAN_GRACE_SECONDS,
                )
            except Exception:  # noqa: BLE001 - 单个库失败不影响其它库
                logger.exception("inline orphan sweep failed for %s", path)
                continue
            reaped += int(changed or 0)
        return reaped

    def lane_snapshot(self) -> dict[str, dict[str, int]]:
        return {
            name: {
                "active": lane.active,
                "queued": len(lane.queued),
                "concurrency": lane.concurrency,
            }
            for name, lane in sorted(self._lanes.items())
        }

    def _submit_lane_job(self, job: _InlineLaneJob) -> None:
        lane = self._lanes[normalize_queue_kind(job.envelope.get("queue_kind"))]
        if lane.active < lane.concurrency:
            self._start_lane_job(lane, job)
            return
        if len(lane.queued) >= lane.queue_limit:
            job.manager.fail_task_for_project(
                job.ctx,
                str(job.envelope["task_type"]),
                int(job.envelope.get("episode") or 0),
                beat_num=job.envelope.get("beat_num"),
                scope=job.envelope.get("scope"),
                error=f"{lane.name} lane queue is full",
                metadata=job.metadata,
                expected_task_id=job.run_task_id,
            )
            raise GlobalLaneQueueLimitExceeded(
                project_id=job.project_id,
                queue_kind=lane.name,
                limit=lane.queue_limit,
                queued=len(lane.queued),
            )
        lane.queued.append(job)
        self._mark_live(job.run_task_id)

    def _start_lane_job(self, lane: _InlineLane, job: _InlineLaneJob) -> None:
        lane.active += 1
        lane.last_started_project_id = job.project_id
        self._mark_live(job.run_task_id)
        self._schedule_lane_job(lane, job)

    def _schedule_lane_job(self, lane: _InlineLane, job: _InlineLaneJob) -> None:
        """把 lane job 派到应用主循环上执行,不管当前在哪个循环/线程。"""
        try:
            owner = self._owner_loop_or_current()
            try:
                current: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
            except RuntimeError:
                current = None

            def _done(
                task: Any,
                lane_name: str = lane.name,
                task_id: str = job.run_task_id,
            ) -> None:
                self._on_background_task_done(task, lane_name, task_id)

            if current is owner:
                handle: Any = owner.create_task(self._run_inline(lane, job))
            else:
                # 从 runner 的临时循环 / 普通线程入队:必须跨线程回到主循环,否则
                # job 会随调用方那条马上关闭的循环一起被回收。
                handle = asyncio.run_coroutine_threadsafe(
                    self._run_inline(lane, job), owner
                )
            self._background_tasks.add(handle)
            handle.add_done_callback(_done)
        except Exception as exc:  # noqa: BLE001 - 派单失败必须落库,不能留假排队
            logger.exception(
                "failed to schedule inline lane job: project=%s task_type=%s",
                job.project_id,
                job.envelope.get("task_type"),
            )
            lane.active = max(lane.active - 1, 0)
            self._clear_live(job.run_task_id)
            self._fail_unscheduled_job(job, exc)
            self._drain_lane(lane.name)

    def _fail_unscheduled_job(
        self, job: _InlineLaneJob, exc: BaseException
    ) -> None:
        try:
            job.manager.fail_task_for_project(
                job.ctx,
                str(job.envelope.get("task_type") or ""),
                int(job.envelope.get("episode") or 0),
                beat_num=job.envelope.get("beat_num"),
                scope=job.envelope.get("scope"),
                error=INLINE_TASK_ORPHAN_MESSAGE,
                metadata={
                    **dict(job.metadata or {}),
                    "error_code": "INLINE_SCHEDULE_FAILED",
                    "schedule_error": str(exc),
                },
                expected_task_id=job.run_task_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to mark unschedulable inline task as failed")

    def _pop_next_lane_job(self, lane: _InlineLane) -> _InlineLaneJob | None:
        if not lane.queued:
            return None
        if len(lane.queued) == 1:
            return lane.queued.popleft()
        for index, job in enumerate(lane.queued):
            if job.project_id != lane.last_started_project_id:
                del lane.queued[index]
                return job
        return lane.queued.popleft()

    def _drain_lane(self, lane_name: str) -> None:
        lane = self._lanes[lane_name]
        while lane.active < lane.concurrency:
            job = self._pop_next_lane_job(lane)
            if job is None:
                return
            self._start_lane_job(lane, job)

    def _remove_queued_task(self, task_id: str) -> bool:
        for lane in self._lanes.values():
            for index, job in enumerate(lane.queued):
                if job.run_task_id == task_id:
                    del lane.queued[index]
                    self._clear_live(task_id)
                    return True
        return False

    async def _run_inline(
        self,
        lane: _InlineLane,
        job: _InlineLaneJob,
    ) -> None:
        consumer_failure: (
            InvalidTaskEnvelope | TaskAuthorityUnavailable | TaskAuthorityFault | None
        ) = None
        verified = None
        if self._consumer is None:
            consumer_failure = InvalidTaskEnvelope()
        else:
            try:
                verified = await retry_authz_read(
                    lambda: self._consumer.consume(
                        job.envelope,
                        expected_root_task_id=job.run_task_id,
                    ),
                    max_retries=_AUTHZ_RETRY_MAX_RETRIES,
                    base_delay=_AUTHZ_RETRY_BASE_DELAY,
                    cap_delay=_AUTHZ_RETRY_CAP_DELAY,
                    sleep=_AUTHZ_RETRY_SLEEP,
                    random=_AUTHZ_RETRY_RANDOM,
                    call_site="inline_task_consumer",
                    retryable_error_types=(TaskAuthorityUnavailable,),
                )
            except (
                InvalidTaskEnvelope,
                TaskAuthorityUnavailable,
                TaskAuthorityFault,
            ) as exc:
                consumer_failure = exc
        if consumer_failure is not None:
            job.manager.fail_task_for_project(
                job.ctx,
                str(job.envelope.get("task_type") or ""),
                int(job.envelope.get("episode") or 0),
                beat_num=job.envelope.get("beat_num"),
                scope=job.envelope.get("scope"),
                error=str(consumer_failure),
                metadata={"error_code": consumer_failure.code},
                expected_task_id=job.run_task_id,
            )
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            lane.executor,
            partial(
                run_project_task_core_sync,
                verified,
                job.ctx,
                job.manager,
                run_task_id=job.run_task_id,
                metadata=job.metadata,
            ),
        )

    def _on_background_task_done(
        self, task: Any, lane_name: str, task_id: str = ""
    ) -> None:
        self._background_tasks.discard(task)
        if task_id:
            self._clear_live(task_id)
        lane = self._lanes[lane_name]
        lane.active = max(lane.active - 1, 0)
        if task.cancelled():
            self._drain_lane(lane_name)
            return
        try:
            exc = task.exception()
        except Exception:
            logger.exception("Inline project task background runner failed")
            return
        if exc is not None:
            logger.error(
                "Inline project task background runner failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        self._drain_lane(lane_name)

    async def cancel_project_task(
        self,
        ctx,
        task_state,
        *,
        force: bool = False,
        acknowledge_no_refund: bool = False,
    ) -> dict:
        del force, acknowledge_no_refund
        await get_cancellation_store().request_cancel(
            project_id=ctx.project_id,
            task_type=task_state.task_type,
            episode=task_state.episode,
            task_id=task_state.task_id,
            beat_num=task_state.beat_num,
            scope=task_state.scope,
        )
        self._remove_queued_task(task_state.task_id)
        get_task_manager().update_progress_for_project(
            ctx,
            task_state.task_type,
            task_state.episode,
            beat_num=task_state.beat_num,
            scope=task_state.scope,
            progress=task_state.progress,
            current_task="任务已取消",
            status="cancelled",
            expected_task_id=task_state.task_id,
        )
        # taskkill/killpg 是阻塞调用(Windows 上可达秒级),不得占事件循环
        await asyncio.get_running_loop().run_in_executor(
            None, kill_task_processes, task_state.task_id
        )
        return {
            "ok": True,
            "status": "cancelled",
            "refund_eligible": True,
            "refund_status": "not_applicable",
        }


class InMemoryCancellationStore:
    def __init__(self) -> None:
        self._keys: dict[str, float] = {}
        self._lock = threading.Lock()

    async def request_cancel(
        self,
        *,
        project_id: str,
        task_type: str,
        episode: int,
        task_id: str,
        beat_num: int | None = None,
        scope: str | None = None,
        ttl_seconds: int = 86_400,
    ) -> None:
        key = cancel_key(
            project_id=project_id,
            task_type=task_type,
            episode=episode,
            task_id=task_id,
            beat_num=beat_num,
            scope=scope,
        )
        with self._lock:
            self._keys[key] = time.time() + max(int(ttl_seconds), 0)

    async def is_cancel_requested(
        self,
        *,
        project_id: str,
        task_type: str,
        episode: int,
        task_id: str,
        beat_num: int | None = None,
        scope: str | None = None,
    ) -> bool:
        key = cancel_key(
            project_id=project_id,
            task_type=task_type,
            episode=episode,
            task_id=task_id,
            beat_num=beat_num,
            scope=scope,
        )
        with self._lock:
            expires_at = self._keys.get(key)
            if expires_at is None:
                return False
            if expires_at < time.time():
                self._keys.pop(key, None)
                return False
            return True
