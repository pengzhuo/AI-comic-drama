"""The LiteLLM chokepoint applies the same context-budget guard as PydanticAI."""

from __future__ import annotations

import pytest

from novelvideo import llm_instrumentation


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEWAPI_TEXT_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("NEWAPI_TEXT_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("MODEL_CONTEXT_WINDOWS_JSON", raising=False)


def test_small_payload_passes_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    llm_instrumentation._guard_litellm_context_budget(
        (),
        {"model": "DC-x", "messages": [{"role": "user", "content": "hello"}]},
    )


def test_oversized_payload_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NEWAPI_TEXT_CONTEXT_TOKENS", "2048")
    monkeypatch.setenv("NEWAPI_TEXT_OUTPUT_TOKENS", "256")
    with pytest.raises(ValueError, match="MODEL_CONTEXT_BUDGET_EXCEEDED"):
        llm_instrumentation._guard_litellm_context_budget(
            (),
            {"model": "DC-x", "messages": [{"role": "user", "content": "正" * 2000}]},
        )


def test_opt_out_disables_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NEWAPI_TEXT_CONTEXT_TOKENS", "0")
    llm_instrumentation._guard_litellm_context_budget(
        (),
        {"model": "DC-x", "messages": [{"role": "user", "content": "正" * 5000}]},
    )


def test_non_dict_messages_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NEWAPI_TEXT_CONTEXT_TOKENS", "2048")
    llm_instrumentation._guard_litellm_context_budget(
        (),
        {"model": "DC-x", "messages": ["not-a-dict" * 500]},
    )
