"""Project-level task admission limits."""

from __future__ import annotations

import os
from dataclasses import dataclass

from novelvideo.task_backend.queues import normalize_queue_kind

PROJECT_LANE_LIMIT_DEFAULTS = {
    "default": 12,
    "video": 3,
    "world": 3,
    "ffmpeg": 2,
}

PROJECT_LANE_MIN_DEFAULTS = {
    "default": 3,
    "video": 1,
    "world": 1,
    "ffmpeg": 1,
}

#: 每用户每 lane 的在飞上限。``video`` 原本是 1，而项目级 lane 的「有效上限」是
#: ``min(hard, max(min, users*user))`` —— 单用户项目下 user=1 会把视频吞吐硬压成
#: **1 路**：实测 95 次生成、单次 p50 152s，整段视频阶段因此铺了 3.7h 墙钟。
#: 与项目级 hard limit 对齐后才能真正并行（值随 hard limit 一起收到 3，见
#: ``GLOBAL_LANE_CONCURRENCY_DEFAULTS`` 的内存说明）。
PROJECT_USER_LANE_LIMIT_DEFAULTS = {
    "default": 3,
    "video": 3,
    "world": 3,
    "ffmpeg": 1,
}

#: CE inline 执行槽位。``video``/``world`` 原本分别只有 2/1，比项目级 hard limit
#: 还低，等于「配了 4 路却只跑 2 路」。
#:
#: 上限从 default=8/video=4 收到 default=6/video=3：inline lane 是**同一个进程内**
#: 跑的，视频任务各自持有解码帧/图片 payload，实测 video=4 时容器内存 30s 内从
#: 4.4G 冲到 9.8G（主机 15.6G 且当时 Swap=0，直接把整机拖死）。少一路并发
#: 换的是峰值内存，值得；真要提速请显式设 ``ST_CE_GLOBAL_MAX_ACTIVE_VIDEO_TASKS``。
GLOBAL_LANE_CONCURRENCY_DEFAULTS = {
    "default": 6,
    "video": 3,
    "world": 3,
    "ffmpeg": 1,
}

GLOBAL_LANE_QUEUE_LIMIT_DEFAULTS = {
    "default": 512,
    "video": 128,
    "world": 64,
    "ffmpeg": 64,
}


@dataclass
class ProjectTaskLimitExceeded(RuntimeError):
    project_id: str
    queue_kind: str
    limit: int
    active: int

    def __post_init__(self) -> None:
        super().__init__(self.project_id, self.queue_kind, self.limit, self.active)

    def __str__(self) -> str:
        return (
            f"project {self.project_id} {self.queue_kind} lane is full "
            f"({self.active}/{self.limit})"
        )


@dataclass
class ProjectUserTaskLimitExceeded(RuntimeError):
    project_id: str
    requester_user_id: str
    queue_kind: str
    limit: int
    active: int

    def __post_init__(self) -> None:
        super().__init__(
            self.project_id,
            self.requester_user_id,
            self.queue_kind,
            self.limit,
            self.active,
        )

    def __str__(self) -> str:
        return (
            f"user {self.requester_user_id} in project {self.project_id} "
            f"{self.queue_kind} lane is full ({self.active}/{self.limit})"
        )


@dataclass
class GlobalLaneQueueLimitExceeded(RuntimeError):
    project_id: str
    queue_kind: str
    limit: int
    queued: int

    def __post_init__(self) -> None:
        super().__init__(self.project_id, self.queue_kind, self.limit, self.queued)

    def __str__(self) -> str:
        return (
            f"global {self.queue_kind} lane queue is full for project {self.project_id} "
            f"({self.queued}/{self.limit})"
        )


@dataclass
class ChannelTaskLimitExceeded(RuntimeError):
    """Channel (organization or platform pool) in-flight lane limit.

    ``scope_kind`` is ``"platform"`` for the shared pool (``org_id is None``)
    and ``"organization"`` for a sold channel. The HTTP ``limit_scope`` is
    derived from it in the app-level handler, not stored here — same as the
    three project-level classes above.
    """

    scope_kind: str
    org_id: str | None
    queue_kind: str
    limit: int
    active: int

    def __post_init__(self) -> None:
        super().__init__(
            self.scope_kind,
            self.org_id,
            self.queue_kind,
            self.limit,
            self.active,
        )

    def __str__(self) -> str:
        return (
            f"channel {self.org_id or self.scope_kind} {self.queue_kind} lane is full "
            f"({self.active}/{self.limit})"
        )


@dataclass
class UserTaskLimitExceeded(RuntimeError):
    """Per-user in-flight lane limit inside a channel (not scoped to a project)."""

    requester_user_id: str
    queue_kind: str
    limit: int
    active: int

    def __post_init__(self) -> None:
        super().__init__(
            self.requester_user_id,
            self.queue_kind,
            self.limit,
            self.active,
        )

    def __str__(self) -> str:
        return (
            f"user {self.requester_user_id} {self.queue_kind} lane is full "
            f"({self.active}/{self.limit})"
        )


def _lane_active_limit(
    queue_kind: str | None,
    *,
    env_prefix: str,
    defaults: dict[str, int],
) -> int | None:
    lane = normalize_queue_kind(queue_kind)
    env_name = f"{env_prefix}_{lane.upper()}_TASKS"
    raw = os.environ.get(env_name)
    default = defaults[lane]
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else None


def _positive_lane_int(
    queue_kind: str | None,
    *,
    env_prefix: str,
    defaults: dict[str, int],
) -> int:
    lane = normalize_queue_kind(queue_kind)
    env_name = f"{env_prefix}_{lane.upper()}_TASKS"
    raw = os.environ.get(env_name)
    default = defaults[lane]
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return max(parsed, 1)


def project_lane_active_limit(queue_kind: str | None) -> int | None:
    return _lane_active_limit(
        queue_kind,
        env_prefix="ST_PROJECT_MAX_ACTIVE",
        defaults=PROJECT_LANE_LIMIT_DEFAULTS,
    )


def project_lane_min_active_limit(queue_kind: str | None) -> int:
    lane = normalize_queue_kind(queue_kind)
    env_name = f"ST_PROJECT_MIN_ACTIVE_{lane.upper()}_TASKS"
    raw = os.environ.get(env_name)
    default = PROJECT_LANE_MIN_DEFAULTS[lane]
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def project_user_lane_active_limit(queue_kind: str | None) -> int | None:
    return _lane_active_limit(
        queue_kind,
        env_prefix="ST_PROJECT_USER_MAX_ACTIVE",
        defaults=PROJECT_USER_LANE_LIMIT_DEFAULTS,
    )


def project_lane_effective_active_limit(
    queue_kind: str | None,
    *,
    eligible_user_count: int,
) -> int | None:
    hard_limit = project_lane_active_limit(queue_kind)
    user_limit = project_user_lane_active_limit(queue_kind)
    if hard_limit is None:
        return None
    if user_limit is None:
        return hard_limit
    member_count = max(int(eligible_user_count), 1)
    computed = max(project_lane_min_active_limit(queue_kind), member_count * user_limit)
    return min(hard_limit, computed)


def global_lane_concurrency(queue_kind: str | None) -> int:
    """CE inline execution slots per lane, distinct from per-project admission."""
    return _positive_lane_int(
        queue_kind,
        env_prefix="ST_CE_GLOBAL_MAX_ACTIVE",
        defaults=GLOBAL_LANE_CONCURRENCY_DEFAULTS,
    )


def global_lane_queue_limit(queue_kind: str | None) -> int:
    """CE inline pending queue bound per lane."""
    return _positive_lane_int(
        queue_kind,
        env_prefix="ST_CE_GLOBAL_MAX_QUEUED",
        defaults=GLOBAL_LANE_QUEUE_LIMIT_DEFAULTS,
    )
