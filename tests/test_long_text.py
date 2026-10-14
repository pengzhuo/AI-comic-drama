"""Tests for the generic long-text map-reduce helper."""

from __future__ import annotations

import pytest

from novelvideo.utils.long_text import (
    chunk_by_budget,
    joined_reduce,
    map_reduce,
    summarize_then_process,
)
from novelvideo.utils.model_context_budget import TextContextBudget


def _tiny_budget() -> TextContextBudget:
    # available_input_tokens == 200 - (10 + 20) == 170 bytes
    return TextContextBudget(
        context_tokens=200, default_output_tokens=20, framing_tokens=10
    )


def test_chunk_by_budget_splits_long_text() -> None:
    text = "\n\n".join(["段落一" * 10, "段落二" * 10, "段落三" * 10])
    chunks = chunk_by_budget(text, _tiny_budget())
    assert len(chunks) > 1


@pytest.mark.asyncio
async def test_map_reduce_joins_in_order() -> None:
    text = "\n\n".join(["a" * 100, "b" * 100, "c" * 100])

    async def worker(chunk: str) -> str:
        return chunk[:1].upper()

    result = await map_reduce(
        text, map_fn=worker, reduce_fn=joined_reduce("|"), budget=_tiny_budget()
    )

    # Chunks keep input order regardless of completion order.
    assert result == "A|B|C"


@pytest.mark.asyncio
async def test_map_reduce_drops_failed_chunks() -> None:
    text = "\n\n".join(["a" * 100, "b" * 100, "c" * 100])
    errors: list[str] = []

    async def worker(chunk: str) -> str:
        if chunk[:1] == "b":
            raise RuntimeError("boom")
        return chunk[:1]

    result = await map_reduce(
        text,
        map_fn=worker,
        reduce_fn=joined_reduce("|"),
        budget=_tiny_budget(),
        on_error=lambda chunk, _exc: errors.append(chunk[:1]),
    )

    assert result == "a|c"
    assert errors == ["b"]


@pytest.mark.asyncio
async def test_summarize_then_process_compresses_overflow() -> None:
    text = "\n\n".join(["段" * 30, "落" * 30, "三" * 30])

    async def expand(chunk: str) -> str:
        return "x" * 100  # each mapped output is larger than the input

    async def summarize(_chunk: str) -> str:
        return "S"

    result = await summarize_then_process(
        text,
        map_fn=expand,
        summarize_fn=summarize,
        budget=_tiny_budget(),
    )

    assert "x" not in result
    assert result and set(result.replace("\n", "")) == {"S"}


@pytest.mark.asyncio
async def test_summarize_then_process_without_summarizer_returns_join() -> None:
    text = "short"

    async def worker(chunk: str) -> str:
        return chunk.upper()

    result = await summarize_then_process(text, map_fn=worker, budget=_tiny_budget())
    assert result == "SHORT"
