"""Unit tests for the shared text context budget service."""

from __future__ import annotations

import pytest

from novelvideo.utils.model_context_budget import (
    DEFAULT_CONTEXT_TOKENS,
    TextContextBudget,
    estimate_tokens,
)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEWAPI_TEXT_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("NEWAPI_TEXT_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("MODEL_CONTEXT_WINDOWS_JSON", raising=False)


def test_estimate_tokens_counts_utf8_bytes() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 4
    assert estimate_tokens("中文") == 6


def test_from_env_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    budget = TextContextBudget.from_env()
    assert budget is not None
    assert budget.context_tokens == DEFAULT_CONTEXT_TOKENS


def test_from_env_zero_disables_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NEWAPI_TEXT_CONTEXT_TOKENS", "0")
    assert TextContextBudget.from_env() is None


def test_from_env_explicit_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NEWAPI_TEXT_CONTEXT_TOKENS", "32768")
    monkeypatch.setenv("NEWAPI_TEXT_OUTPUT_TOKENS", "2048")
    budget = TextContextBudget.from_env()
    assert budget is not None
    assert budget.context_tokens == 32768
    assert budget.default_output_tokens == 2048


def test_from_env_per_model_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(
        "MODEL_CONTEXT_WINDOWS_JSON", '{"DC-hermes-LLM": 200000, "bad": "x"}'
    )
    budget = TextContextBudget.from_env("DC-hermes-LLM")
    assert budget is not None
    assert budget.context_tokens == 200000
    # Unknown alias falls back to the conservative default.
    assert TextContextBudget.from_env("DC-unknown").context_tokens == DEFAULT_CONTEXT_TOKENS


def test_from_env_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NEWAPI_TEXT_CONTEXT_TOKENS", "4096")
    monkeypatch.setenv("NEWAPI_TEXT_OUTPUT_TOKENS", "4096")
    with pytest.raises(ValueError):
        TextContextBudget.from_env()


def test_fits_and_checked_settings_agree() -> None:
    budget = TextContextBudget(context_tokens=2000, default_output_tokens=500)
    small = {"messages": [{"role": "user", "content": "hello"}]}
    huge = {"messages": [{"role": "user", "content": "正" * 2000}]}
    assert budget.fits(small) is True
    assert budget.checked_settings(small, None)["max_tokens"] == 500
    assert budget.fits(huge) is False
    with pytest.raises(ValueError, match="MODEL_CONTEXT_BUDGET_EXCEEDED"):
        budget.checked_settings(huge, None)


def test_plan_chunks_returns_single_when_it_fits() -> None:
    budget = TextContextBudget(context_tokens=4096, default_output_tokens=512)
    text = "short paragraph"
    assert budget.plan_chunks(text) == [text]


def test_plan_chunks_splits_and_preserves_content() -> None:
    budget = TextContextBudget(
        context_tokens=120, default_output_tokens=20, framing_tokens=10
    )
    available = budget.available_input_tokens()
    paragraphs = ["段落一" * 10, "段落二" * 10, "段落三" * 10]
    text = "\n\n".join(paragraphs)

    chunks = budget.plan_chunks(text)

    assert len(chunks) > 1
    assert all(estimate_tokens(chunk) <= available for chunk in chunks)
    joined = "".join(chunks)
    for paragraph in paragraphs:
        assert paragraph in joined


def test_plan_chunks_hard_splits_single_oversized_block() -> None:
    budget = TextContextBudget(
        context_tokens=120, default_output_tokens=20, framing_tokens=10
    )
    available = budget.available_input_tokens()
    text = "字" * 200  # 600 bytes, no boundaries

    chunks = budget.plan_chunks(text)

    assert len(chunks) > 1
    assert all(estimate_tokens(chunk) <= available for chunk in chunks)
    assert "".join(chunks) == text
