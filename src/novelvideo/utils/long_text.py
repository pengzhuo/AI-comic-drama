"""Generic chunked map-reduce for long text model calls.

Several call sites pass a whole episode or novel to a single model request,
which overflows the model window and fails at the gateway. This helper chunks
the text within the shared context budget, runs the per-chunk model call with
bounded concurrency, and merges the results — turning "the whole request fails"
into "it completes".

Chunking is token/byte aware (see :mod:`novelvideo.utils.model_context_budget`);
merging is supplied by the caller because only it knows how to combine its
outputs (concatenate rewrites, union extracted entities, summarize, ...).
"""

from __future__ import annotations

from typing import Awaitable, Callable, Sequence, TypeVar

from novelvideo.utils.bounded_concurrency import default_llm_concurrency, map_bounded
from novelvideo.utils.model_context_budget import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_OUTPUT_TOKENS,
    FRAMING_TOKENS,
    TextContextBudget,
    estimate_tokens,
)

R = TypeVar("R")
T = TypeVar("T")

_Reducer = Callable[[Sequence["R | None"]], T]


def _default_budget() -> TextContextBudget:
    return TextContextBudget(
        context_tokens=DEFAULT_CONTEXT_TOKENS,
        default_output_tokens=DEFAULT_OUTPUT_TOKENS,
        framing_tokens=FRAMING_TOKENS,
    )


def chunk_by_budget(
    text: str,
    budget: TextContextBudget | None = None,
    *,
    reserve_tokens: int | None = None,
) -> list[str]:
    """Split ``text`` into pieces that each fit the context budget."""

    return (budget or _default_budget()).plan_chunks(
        text, reserve_tokens=reserve_tokens
    )


def fit_text(
    text: str,
    budget: TextContextBudget | None = None,
    *,
    reserve_tokens: int | None = None,
) -> str:
    """Return the longest leading piece of ``text`` that fits the budget.

    ``text`` is returned unchanged when it already fits. Use for *supplementary*
    context (evidence that may be trimmed), never for primary content.
    """

    if not text:
        return text
    chunks = chunk_by_budget(text, budget, reserve_tokens=reserve_tokens)
    return chunks[0] if chunks else ""


async def map_reduce(
    text: str,
    *,
    map_fn: Callable[[str], Awaitable[R]],
    reduce_fn: _Reducer[R, T],
    budget: TextContextBudget | None = None,
    reserve_tokens: int | None = None,
    limit: int | None = None,
    on_error: Callable[[str, BaseException], None] | None = None,
    chunker: Callable[[str], list[str]] | None = None,
    require_all: bool = False,
) -> T:
    """Chunk ``text``, map ``map_fn`` over the chunks, then ``reduce_fn``.

    Chunks run with bounded concurrency; by default a failed chunk yields
    ``None`` in its slot rather than failing the whole call. ``reduce_fn``
    receives the ordered results (including ``None`` for failures) and returns
    the merged value. Set ``require_all`` when dropping a chunk would silently
    lose content (e.g. rewriting) — then any failure raises instead.
    """

    if chunker is not None:
        chunks = chunker(text)
    else:
        chunks = chunk_by_budget(text, budget, reserve_tokens=reserve_tokens)
    results = await map_bounded(
        chunks,
        map_fn,
        limit=limit if limit is not None else default_llm_concurrency(),
        on_error=on_error,
    )
    if require_all:
        failed = sum(1 for result in results if result is None)
        if failed:
            raise ValueError(
                f"long_text map_reduce: {failed} of {len(chunks)} chunk(s) failed"
            )
    return reduce_fn(results)


def joined_reduce(separator: str = "\n\n", *, strip: bool = True) -> Callable[[Sequence[str | None]], str]:
    """Reduce string chunks by joining the non-empty ones in order."""

    def _reduce(results: Sequence[str | None]) -> str:
        parts = [str(part) for part in results if part]
        if strip:
            parts = [part.strip() for part in parts if part.strip()]
        return separator.join(parts)

    return _reduce


async def summarize_then_process(
    text: str,
    *,
    map_fn: Callable[[str], Awaitable[str]],
    summarize_fn: Callable[[str], Awaitable[str]] | None = None,
    budget: TextContextBudget | None = None,
    reserve_tokens: int | None = None,
    limit: int | None = None,
    separator: str = "\n\n",
) -> str:
    """Map over chunks and join; if the join still overflows, summarize once.

    The hierarchical fallback is for tasks with no natural merge: when the
    combined output exceeds the budget, chunk it again and run ``summarize_fn``
    over those chunks, then join. Without ``summarize_fn`` the joined output is
    returned as-is.
    """

    effective_budget = budget or _default_budget()
    joined = await map_reduce(
        text,
        map_fn=map_fn,
        reduce_fn=joined_reduce(separator),
        budget=effective_budget,
        reserve_tokens=reserve_tokens,
        limit=limit,
    )
    if summarize_fn is None:
        return joined
    if estimate_tokens(joined) <= effective_budget.available_input_tokens(reserve_tokens):
        return joined
    return await map_reduce(
        joined,
        map_fn=summarize_fn,
        reduce_fn=joined_reduce(separator),
        budget=effective_budget,
        reserve_tokens=reserve_tokens,
        limit=limit,
    )
