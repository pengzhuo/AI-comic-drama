"""Conservative, deployment-configured budgets for text model requests.

Gateway aliases do not advertise a context window. A file character limit
therefore cannot establish that an individual model request will fit. Count
the UTF-8 bytes of the mapped text and schemas as a conservative upper bound
for byte-tokenized text, leaving room for protocol framing and the response.
This deliberately overestimates Chinese text and requires no tokenizer download.
It never truncates the input.

The guard is **on by default** with a large, safe window so egregious overflow
fails visibly instead of silently at the gateway. Deployments should override
``NEWAPI_TEXT_CONTEXT_TOKENS`` (or ``MODEL_CONTEXT_WINDOWS_JSON``) with the
verified window of every text model behind their gateway aliases. Set
``NEWAPI_TEXT_CONTEXT_TOKENS=0`` to disable the check entirely (legacy behavior).

Image URLs and inline Base64 are transport representations, not text tokens.
Exclude only URLs in typed image content parts from this text estimate;
visual tokens depend on the routed model and are validated by the provider.
This is not a bound on the combined text and visual token usage.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

#: Default window when nothing is configured. Large enough to leave normal
#: per-chunk/per-beat requests alone, small enough to catch whole-document
#: prompts that would otherwise blow up at the gateway.
DEFAULT_CONTEXT_TOKENS = 131_072
DEFAULT_OUTPUT_TOKENS = 8192
FRAMING_TOKENS = 1024


class ModelContextBudgetExceeded(ValueError):
    """Raised when a text request would exceed the configured context budget.

    Subclasses ``ValueError`` so existing ``pytest.raises(ValueError)`` callers
    keep working; ``error_code`` lets the task runtime classify the failure.
    """

    error_code = "MODEL_CONTEXT_BUDGET_EXCEEDED"


def estimate_tokens(text: str) -> int:
    """Conservative token upper bound for text (UTF-8 byte length).

    Byte length overestimates Latin text and is exact for byte-tokenized
    Chinese under most providers; no tokenizer download is required.
    """

    if not text:
        return 0
    return len(text.encode("utf-8"))


def _text_budget_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy message content for counting without modifying the real request."""
    messages = []
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            content = [
                {
                    **part,
                    "image_url": {**part["image_url"], "url": "[image]"},
                }
                if part.get("type") == "image_url"
                and isinstance(part.get("image_url"), dict)
                else part
                for part in content
            ]
        messages.append({**message, "content": content})
    return {**payload, "messages": messages}


def _model_windows() -> dict[str, int]:
    """Optional per-alias windows from ``MODEL_CONTEXT_WINDOWS_JSON``."""

    raw = os.environ.get("MODEL_CONTEXT_WINDOWS_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    windows: dict[str, int] = {}
    for key, value in data.items():
        try:
            tokens = int(value)
        except (TypeError, ValueError):
            continue
        if tokens > 0:
            windows[str(key)] = tokens
    return windows


def _split_units(text: str) -> list[str]:
    """Split on paragraph breaks, then on sentence-ish boundaries."""

    blocks: list[str] = []
    for paragraph in text.split("\n\n"):
        if estimate_tokens(paragraph) <= 0:
            continue
        blocks.append(paragraph)
    if len(blocks) <= 1:
        # No paragraph structure: fall back to line boundaries.
        blocks = [line for line in text.splitlines(keepends=True) if line]
    return blocks or [text]


def _hard_split(text: str, max_bytes: int) -> list[str]:
    """Split a single oversized block by codepoints under a byte budget."""

    pieces: list[str] = []
    buf: list[str] = []
    size = 0
    for char in text:
        char_bytes = len(char.encode("utf-8"))
        if buf and size + char_bytes > max_bytes:
            pieces.append("".join(buf))
            buf = []
            size = 0
        buf.append(char)
        size += char_bytes
    if buf:
        pieces.append("".join(buf))
    return pieces


@dataclass(frozen=True)
class TextContextBudget:
    context_tokens: int
    default_output_tokens: int = DEFAULT_OUTPUT_TOKENS
    framing_tokens: int = FRAMING_TOKENS

    @classmethod
    def from_env(cls, model: str | None = None) -> TextContextBudget | None:
        """Build the budget from the environment.

        Returns ``None`` when the check is explicitly disabled
        (``NEWAPI_TEXT_CONTEXT_TOKENS=0``). Otherwise the window resolves, in
        order, from: the explicit ``NEWAPI_TEXT_CONTEXT_TOKENS`` value, a
        per-alias entry in ``MODEL_CONTEXT_WINDOWS_JSON``, or the conservative
        default. The check is therefore on by default.
        """

        raw = os.environ.get("NEWAPI_TEXT_CONTEXT_TOKENS", "").strip()
        if raw:
            context = int(raw)
            if context <= 0:
                return None
        elif model:
            context = _model_windows().get(model, DEFAULT_CONTEXT_TOKENS)
        else:
            context = DEFAULT_CONTEXT_TOKENS
        output = int(
            os.environ.get("NEWAPI_TEXT_OUTPUT_TOKENS", str(DEFAULT_OUTPUT_TOKENS))
        )
        if context <= output + FRAMING_TOKENS or output <= 0:
            raise ValueError(
                "Invalid NEWAPI_TEXT_CONTEXT_TOKENS / NEWAPI_TEXT_OUTPUT_TOKENS"
            )
        return cls(context_tokens=context, default_output_tokens=output)

    def available_input_tokens(self, reserve_tokens: int | None = None) -> int:
        """Input budget left after reserving framing and output space."""

        reserve = (
            self.framing_tokens + self.default_output_tokens
            if reserve_tokens is None
            else reserve_tokens
        )
        return max(self.context_tokens - reserve, 1)

    def _output_budget(self, settings: dict | None) -> int:
        checked = dict(settings or {})
        requested_output = checked.get("max_tokens")
        output_tokens = (
            self.default_output_tokens if requested_output is None else requested_output
        )
        if not isinstance(output_tokens, int) or isinstance(output_tokens, bool) or output_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        extra_body = checked.get("extra_body") or {}
        for key in ("max_tokens", "max_completion_tokens"):
            if key in extra_body:
                requested = extra_body[key]
                if (
                    not isinstance(requested, int)
                    or isinstance(requested, bool)
                    or requested <= 0
                ):
                    raise ValueError(f"extra_body.{key} must be a positive integer")
                output_tokens = max(output_tokens, requested)
        return output_tokens

    def _input_upper_bound(self, payload: dict[str, Any]) -> int:
        return (
            len(
                json.dumps(
                    _text_budget_payload(payload),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            + self.framing_tokens
        )

    def fits(self, payload: dict[str, Any], settings: dict | None = None) -> bool:
        """Whether the payload fits, without raising."""

        return (
            self._input_upper_bound(payload) + self._output_budget(settings)
            <= self.context_tokens
        )

    def plan_chunks(self, text: str, *, reserve_tokens: int | None = None) -> list[str]:
        """Split ``text`` into pieces that each fit the input budget.

        Prefers paragraph/sentence boundaries; hard-splits a single oversized
        block. Returns ``[text]`` unchanged when it already fits. Never drops
        content.
        """

        budget = self.available_input_tokens(reserve_tokens)
        if estimate_tokens(text) <= budget:
            return [text]

        chunks: list[str] = []
        buf = ""
        buf_bytes = 0
        for unit in _split_units(text):
            unit_bytes = estimate_tokens(unit)
            if unit_bytes > budget:
                if buf:
                    chunks.append(buf)
                    buf = ""
                    buf_bytes = 0
                chunks.extend(_hard_split(unit, budget))
                continue
            if buf and buf_bytes + unit_bytes > budget:
                chunks.append(buf)
                buf = unit
                buf_bytes = unit_bytes
            else:
                buf = f"{buf}\n\n{unit}" if buf else unit
                buf_bytes += unit_bytes + (2 if buf_bytes else 0)
        if buf:
            chunks.append(buf)
        return chunks

    def checked_settings(self, payload: dict[str, Any], settings: dict | None) -> dict:
        output_tokens = self._output_budget(settings)
        input_upper_bound = self._input_upper_bound(payload)
        if input_upper_bound + output_tokens > self.context_tokens:
            raise ModelContextBudgetExceeded(
                "MODEL_CONTEXT_BUDGET_EXCEEDED: "
                f"文本输入 token 保守上界 {input_upper_bound:,} + 输出预算 {output_tokens:,} "
                f"> 请求上下文预算 {self.context_tokens:,}。"
                "请分批处理，或核实网关模型窗口后调整请求预算；原文未截断。"
            )
        checked = dict(settings or {})
        checked["max_tokens"] = output_tokens
        return checked


def guard_payload(
    payload: dict[str, Any],
    settings: dict | None = None,
    *,
    model: str | None = None,
) -> None:
    """Raise ``MODEL_CONTEXT_BUDGET_EXCEEDED`` when a request would overflow.

    Shared chokepoint for callers that build an OpenAI-style ``messages``
    payload (e.g. the LiteLLM adapter). No-op when the check is disabled.
    """

    budget = TextContextBudget.from_env(model)
    if budget is None:
        return
    budget.checked_settings(payload, settings)

