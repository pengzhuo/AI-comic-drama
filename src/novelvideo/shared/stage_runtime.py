"""共享的阶段运行期基建：热调参、并发闸门、有界映射、逐调用计时。

``workflows/script_processing.py`` 最早实现了一套「独立并发 + 过载冷却 + 热调参
文件 + 逐调用计时」，但此前只有它自己在用；结构化抽取、cognee 归一化、逐行脚本、
逐 beat 优化等长稿阶段要么吃全局 ``STRUCTURED_LLM_CONCURRENCY``（默认 2），要么
是纯串行 ``for``。这里把那套语义抽成通用件：

* 每个阶段一份 ``runtime/<stage>.json`` 热调参文件，优先级 env > 文件 > 常量；
* :class:`StageLimiter` —— 正常按上限并发放行，连续瞬时失败后进入冷却，
  冷却期间把有效并发降到 ``cooldown_limit``（默认 1，与旧行为一致）；
* :func:`run_bounded` —— 保序的有界映射，首个致命错误可选取消其余在飞调用；
* :func:`record_call` —— 往项目 ``runtime/pipeline_timing.jsonl`` 追一条调用事件。

这些函数都**不抛异常**（除并发本身应有的传播），参数非法一律回落默认值。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence, TypeVar

_log = logging.getLogger("novelvideo.stage_runtime")

RUNTIME_DIR_ENV = "NOVELVIDEO_RUNTIME_DIR"
STATE_DIR_ENV = "NOVELVIDEO_STATE_DIR"

T = TypeVar("T")
R = TypeVar("R")


# ── 热调参文件 ────────────────────────────────────────────────────────────


def runtime_tuning_path(stage: str, *, explicit_env: str | None = None) -> Path:
    """热调参文件位置：显式 env > ``NOVELVIDEO_RUNTIME_DIR`` > state 同级 runtime。"""

    explicit = os.environ.get(explicit_env, "").strip() if explicit_env else ""
    if explicit:
        return Path(explicit)
    filename = f"{stage}.json"
    runtime = os.environ.get(RUNTIME_DIR_ENV, "").strip()
    if runtime:
        return Path(runtime) / filename
    state = os.environ.get(STATE_DIR_ENV, "").strip()
    if state:
        return Path(state).resolve().parent / "runtime" / filename
    return Path("runtime") / filename


def load_runtime_tuning(
    stage: str, *, explicit_env: str | None = None
) -> dict[str, Any]:
    """读取热调参文件；缺失或损坏一律返回空字典（绝不抛错）。"""

    try:
        raw = runtime_tuning_path(stage, explicit_env=explicit_env).read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# ── 值收正 ──────────────────────────────────────────────────────────────


def env_positive_int(name: str, default: int) -> int:
    """正整数环境变量；未设置或非法返回 ``default``。"""

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def coerce_positive_int(raw: Any) -> int | None:
    """把热调参文件里的值收成正整数；非法返回 ``None``。"""

    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str):
        try:
            value = int(raw.strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    return None


def env_flag(name: str) -> bool | None:
    """解析布尔环境变量；未设置或非法返回 ``None``。"""

    raw = os.environ.get(name, "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return None


def tuning_bool(raw: Any, default: bool) -> bool:
    """把热调参文件里的值收成布尔；非法返回 ``default``。"""

    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"0", "false", "no", "off"}:
            return False
        if lowered in {"1", "true", "yes", "on"}:
            return True
    return default


# ── 按阶段聚合的通用设置（env > 文件 > 常量） ─────────────────────────────


def stage_int(
    stage: str,
    env_name: str,
    key: str,
    default: int,
    *,
    explicit_env: str | None = None,
) -> int:
    """按优先级取一个正整数设置：env（>0）> 热调参文件 > ``default``。"""

    env = env_positive_int(env_name, 0)
    if env > 0:
        return env
    tuned = coerce_positive_int(
        load_runtime_tuning(stage, explicit_env=explicit_env).get(key)
    )
    if tuned is not None:
        return tuned
    return default


def stage_bool(
    stage: str,
    env_name: str,
    key: str,
    default: bool,
    *,
    explicit_env: str | None = None,
) -> bool:
    """按优先级取一个布尔设置：env > 热调参文件 > ``default``。"""

    env = env_flag(env_name)
    if env is not None:
        return env
    return tuning_bool(
        load_runtime_tuning(stage, explicit_env=explicit_env).get(key), default
    )


def stage_choice(
    stage: str,
    env_name: str,
    key: str,
    options: set[str],
    default: str,
    *,
    explicit_env: str | None = None,
) -> str:
    """按优先级取一个枚举设置：env > 热调参文件 > ``default``。"""

    env = os.environ.get(env_name, "").strip().lower()
    if env in options:
        return env
    tuned = (
        str(load_runtime_tuning(stage, explicit_env=explicit_env).get(key) or "")
        .strip()
        .lower()
    )
    if tuned in options:
        return tuned
    return default


# ── 并发闸门 ──────────────────────────────────────────────────────────────


class StageLimiter:
    """阶段并发闸门，兼做「上游过载」降速。

    正常时按配置并发放行；连续 ``streak`` 次瞬时失败（429/5xx/连接失败）后进入
    ``cooldown_seconds`` 秒冷却，冷却期间新调用必须先拿到冷却闸门，于是有效并发
    降到 ``cooldown_limit``（默认 1 —— 网关自己报 ``system_cpu_overloaded`` 时
    不继续加压；设成 ``max(1, limit // 2)`` 则是「过载减半」的温和降级）。

    ``acquire`` 返回本次是否持有了冷却闸门，释放时原样传回 :meth:`release`
    （同步释放，取消路径上不会因为 await 再抛错）。
    """

    def __init__(
        self,
        limit: int,
        *,
        streak: int,
        cooldown_seconds: float,
        cooldown_limit: int = 1,
    ) -> None:
        self._limit = max(1, int(limit))
        self._streak_threshold = max(1, int(streak))
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._cooldown_limit = max(1, int(cooldown_limit))
        self._consecutive = 0
        self._resume_at = 0.0
        self._semaphore = asyncio.Semaphore(self._limit)
        self._cooldown_semaphore = asyncio.Semaphore(self._cooldown_limit)

    @property
    def limit(self) -> int:
        return self._limit

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    @property
    def cooling_down(self) -> bool:
        """当前是否处于过载冷却中。"""

        return self._resume_at > self._now()

    def note_success(self) -> None:
        self._consecutive = 0

    def note_transient_failure(self) -> bool:
        """记一次瞬时失败；返回 ``True`` 表示本次触发了过载冷却。"""

        self._consecutive += 1
        if self._consecutive < self._streak_threshold or self._cooldown_seconds <= 0:
            return False
        self._consecutive = 0
        self._resume_at = self._now() + self._cooldown_seconds
        return True

    async def acquire(self) -> bool:
        cooling = self.cooling_down
        if cooling:
            await self._cooldown_semaphore.acquire()
        try:
            await self._semaphore.acquire()
        except BaseException:
            if cooling:
                self._cooldown_semaphore.release()
            raise
        return cooling

    def release(self, cooling: bool) -> None:
        self._semaphore.release()
        if cooling:
            self._cooldown_semaphore.release()


async def run_bounded(
    items: Sequence[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    limiter: StageLimiter | None,
    cancel_siblings: bool = True,
    gate: bool = True,
) -> list[R]:
    """按 ``limiter`` 的上限处理 ``items``，返回与输入同序的结果。

    ``limiter is None`` 时退化为严格串行。``cancel_siblings`` 为 ``True``（默认）
    时，任意一个 worker 抛出致命异常会取消其余在飞调用再上抛——适合「一条坏了
    整条流水线都白跑」的阶段；``False`` 则等所有任务收尾后再上抛。

    ``gate`` 为 ``True`` 时每次调用还会经过进程级全局闸门
    (:func:`novelvideo.shared.global_llm_gate.global_llm_slot`)，避免多阶段叠加
    把网关打爆。
    """

    if not items:
        return []
    if limiter is None:
        return [await worker(item) for item in items]

    from novelvideo.shared.global_llm_gate import global_llm_slot

    async def one(item: T) -> R:
        cooling = await limiter.acquire()
        try:
            if gate:
                async with global_llm_slot():
                    return await worker(item)
            return await worker(item)
        finally:
            limiter.release(cooling)

    tasks = [asyncio.create_task(one(item)) for item in items]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        if cancel_siblings:
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


# ── 逐调用计时 ────────────────────────────────────────────────────────────


def record_call(
    output_dir: str | os.PathLike[str] | None,
    stage: str,
    phase: str,
    *,
    started: float,
    ok: bool,
    **fields: Any,
) -> None:
    """把一次模型调用的墙钟耗时写进 ``runtime/pipeline_timing.jsonl``（仅观测）。"""

    from novelvideo.pipeline_timing import record_event

    record_event(
        output_dir,
        f"{stage}_call",
        phase=phase,
        ok=ok,
        duration_seconds=round(time.monotonic() - started, 3),
        **fields,
    )


__all__ = [
    "RUNTIME_DIR_ENV",
    "STATE_DIR_ENV",
    "StageLimiter",
    "coerce_positive_int",
    "env_flag",
    "env_positive_int",
    "load_runtime_tuning",
    "record_call",
    "run_bounded",
    "runtime_tuning_path",
    "stage_bool",
    "stage_choice",
    "stage_int",
    "tuning_bool",
]
