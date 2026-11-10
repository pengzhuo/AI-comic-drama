"""Bounded parallel mapping for per-chunk LLM work.

Structured extraction runs one model call per source chunk, and a screenplay can
carry well over a hundred scenes.  Running them serially makes a build take
minutes of pure round-trip latency; running them all at once buries the gateway
and trips rate limits.  Both extremes are avoided by mapping with a fixed number
of slots in flight.

Results keep the input order regardless of completion order, because downstream
merging depends on chunk order to resolve ties deterministically.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")

STRUCTURED_LLM_CONCURRENCY_ENV = "STRUCTURED_LLM_CONCURRENCY"

# Matches COGNEE_LLM_CONCURRENCY's default. The gateway is the shared
# bottleneck, so a second pool running deeper would trip the same rate limits
# the Cognee pool is tuned to avoid.
DEFAULT_LIMIT = 2


def default_llm_concurrency() -> int:
    """Slots for per-chunk LLM work, overridable per deployment."""
    raw = os.getenv(STRUCTURED_LLM_CONCURRENCY_ENV, str(DEFAULT_LIMIT)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{STRUCTURED_LLM_CONCURRENCY_ENV} must be a positive integer, got {raw!r}"
        ) from exc
    if value <= 0:
        raise ValueError(
            f"{STRUCTURED_LLM_CONCURRENCY_ENV} must be a positive integer, got {raw!r}"
        )
    return value


async def map_bounded(
    items: Sequence[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    limit: int | None = None,
    on_error: Callable[[T, BaseException], Any] | None = None,
    gate: bool = True,
    stage: str | None = None,
) -> list[R | None]:
    """Run ``worker`` over ``items`` with at most ``limit`` calls in flight.

    A failing item yields ``None`` in its slot rather than cancelling the batch:
    one unparseable scene must not discard the analysis of every other scene.
    ``on_error`` is invoked for each failure so callers can record it against the
    chunk that produced it.

    ``gate`` (default on) also takes a process-global LLM slot
    (:mod:`novelvideo.shared.global_llm_gate`) so several stages running at once
    cannot collectively bury the gateway.

    ``stage`` (optional) resolves the concurrency from
    ``runtime/<stage>.json`` when ``limit`` is ``None``: stage-specific env
    (``STRUCTURED_LLM_CONCURRENCY``) > that file > :func:`default_llm_concurrency`.
    """
    if not items:
        return []

    resolved = limit
    if resolved is None:
        if stage:
            from novelvideo.shared.stage_runtime import stage_int

            resolved = stage_int(
                stage,
                STRUCTURED_LLM_CONCURRENCY_ENV,
                "concurrency",
                default_llm_concurrency(),
            )
        else:
            resolved = default_llm_concurrency()
    effective = int(resolved)
    semaphore = asyncio.Semaphore(max(1, effective))

    from novelvideo.shared.global_llm_gate import global_llm_slot

    async def run(item: T) -> R | None:
        async with semaphore:
            try:
                if gate:
                    async with global_llm_slot():
                        return await worker(item)
                return await worker(item)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                if on_error is not None:
                    on_error(item, exc)
                return None

    return list(await asyncio.gather(*(run(item) for item in items)))
