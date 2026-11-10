"""进程级 LLM 调用闸门。

每个阶段现在都能拿自己的并发（例如 `script_process` 默认 6），但阶段之间没有共享
上限：多个项目/多个阶段同时在飞时，叠加的请求会把网关打爆（``system_cpu_overloaded``
/ 429），反过来拖慢所有任务。``map_bounded`` 每次调用各建一个 semaphore，也从来没有
真正的全局上限。

这里给所有 LLM 调用加一层**进程级**闸门：同一事件循环内共享一个 ``asyncio.Semaphore``
（跨循环用 ``WeakKeyDictionary`` 分桶，避免 semaphore 绑错 loop）。上限来自
``LLM_GLOBAL_CONCURRENCY``，默认 12；设为 ``0`` 关闭闸门（回到各阶段各自限速）。

要点：

* 只拦「在飞请求数」，不改变任何阶段的内部并发策略；
* ``global_llm_slot`` 必须在拿到阶段级闸门之后、真正发请求之前进入——否则会一边
  占着全局名额一边等阶段名额，造成死锁式饥饿；
* 上限在首次使用时按事件循环固定，运行中改环境变量不生效（进程启动即定）。
"""

from __future__ import annotations

import asyncio
import os
import weakref
from contextlib import asynccontextmanager

LLM_GLOBAL_CONCURRENCY_ENV = "LLM_GLOBAL_CONCURRENCY"
DEFAULT_GLOBAL_CONCURRENCY = 12

_gates: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def global_llm_limit() -> int:
    """全局在飞 LLM 调用上限；``0`` 表示关闭闸门。非法值回落默认。"""

    raw = os.environ.get(
        LLM_GLOBAL_CONCURRENCY_ENV, str(DEFAULT_GLOBAL_CONCURRENCY)
    ).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_GLOBAL_CONCURRENCY
    return max(0, value)


def _gate_for_running_loop() -> asyncio.Semaphore | None:
    limit = global_llm_limit()
    if limit <= 0:
        return None
    loop = asyncio.get_running_loop()
    semaphore = _gates.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        _gates[loop] = semaphore
    return semaphore


@asynccontextmanager
async def global_llm_slot():
    """占用一个全局 LLM 名额；闸门关闭时是空操作。"""

    semaphore = _gate_for_running_loop()
    if semaphore is None:
        yield
        return
    await semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()


__all__ = [
    "DEFAULT_GLOBAL_CONCURRENCY",
    "LLM_GLOBAL_CONCURRENCY_ENV",
    "global_llm_limit",
    "global_llm_slot",
]
