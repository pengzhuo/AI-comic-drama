"""单价表与上游定价表达式求值。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novelvideo import production_prices as pp
from novelvideo.production_prices import (
    ActionRequest,
    PriceBook,
    PriceError,
    evaluate_expr,
)


#: 夹具直接取自上游 ``/api/pricing`` 的真实表达式（2026-11）。
SEEDANCE_SC_EXPR = (
    'u("resolution") == "480p" ? tier("480p", u("seconds") * 0.07880597) '
    ': u("resolution") == "720p" ? tier("720p", u("seconds") * 0.09850746) '
    ': tier("1080p", u("seconds") * 0.15761194)'
)
SEEDANCE_FAST_EXPR = (
    'u("video_input") == "none" ? tier("none", u("tokens") * 3.03731343 / 1000000) '
    ': tier("video", u("tokens") * 1.80597015 / 1000000)'
)
AUDIO_EXPR = 'tier("base", p * 17.910447761194 + c * 14.925373134328)'
DEEPSEEK_EXPR = 'tier("base", p * 0.59104478 + c * 1.77313433 + cr * 0.01970149 + cc * 0.00111642)'


@pytest.fixture()
def prices_path(tmp_path, monkeypatch):
    path = tmp_path / "production-prices.json"
    monkeypatch.setenv(pp.ENV_CONFIG_PATH, str(path))
    pp._CACHE.clear()
    yield path
    pp._CACHE.clear()


# ---------------------------------------------------------------------------
# 求值器
# ---------------------------------------------------------------------------


def test_expr_video_resolution_tier():
    assert evaluate_expr(SEEDANCE_SC_EXPR, {"resolution": "480p", "seconds": 5}) == pytest.approx(
        0.07880597 * 5
    )
    assert evaluate_expr(SEEDANCE_SC_EXPR, {"resolution": "720p", "seconds": 10}) == pytest.approx(
        0.09850746 * 10
    )
    assert evaluate_expr(SEEDANCE_SC_EXPR, {"resolution": "1080p", "seconds": 4}) == pytest.approx(
        0.15761194 * 4
    )


def test_expr_video_input_branch_and_shorthand():
    assert evaluate_expr(SEEDANCE_FAST_EXPR, {"video_input": "none", "tokens": 2_000_000}) == (
        pytest.approx(3.03731343 * 2)
    )
    assert evaluate_expr(SEEDANCE_FAST_EXPR, {"video_input": "video", "tokens": 2_000_000}) == (
        pytest.approx(1.80597015 * 2)
    )


def test_expr_prompt_completion_shorthand():
    value = evaluate_expr(AUDIO_EXPR, {"p": 1000, "c": 2000})
    assert value == pytest.approx(1000 * 17.910447761194 + 2000 * 14.925373134328)


def test_expr_cache_shorthand_and_missing_vars_default_zero():
    assert evaluate_expr(DEEPSEEK_EXPR, {"p": 1000}) == pytest.approx(1000 * 0.59104478)
    assert evaluate_expr(DEEPSEEK_EXPR, {}) == 0.0


def test_expr_rejects_unsupported_syntax():
    with pytest.raises(PriceError):
        evaluate_expr("__import__('os').system('echo hi')", {})
    with pytest.raises(PriceError):
        evaluate_expr("1 / 0", {})
    with pytest.raises(PriceError):
        evaluate_expr("u(1 +", {})


# ---------------------------------------------------------------------------
# 规则匹配
# ---------------------------------------------------------------------------


def _book(*rules, **extra) -> PriceBook:
    raw = {
        "rules": list(rules),
        "synced": extra.get("synced", {"relays": [], "models": {}}),
        "aliases": extra.get("aliases", {}),
        "client_markups": extra.get("client_markups", {}),
        "markup_default": extra.get("markup_default"),
        "assumptions": extra.get("assumptions", {}),
    }
    return PriceBook(raw=raw)


def test_manual_rule_most_specific_wins():
    book = _book(
        {"id": "kind", "match": {"kind": "image"}, "unit": "call", "price": 1.0},
        {"id": "model", "match": {"kind": "image", "model": "LingShan-G2"}, "unit": "call", "price": 2.0},
        {
            "id": "tuned",
            "match": {"kind": "image", "model": "LingShan-G2", "params": {"size": "4K"}},
            "unit": "call",
            "price": 3.0,
        },
    )
    action = ActionRequest(kind="image", model="LingShan-G2", params={"size": "4K"}, quantity=2)
    priced = book.price(action)
    assert priced.rule_id == "tuned"
    assert priced.cny == pytest.approx(6.0)
    assert priced.source == "manual"

    other = ActionRequest(kind="image", model="LingShan-G2", params={"size": "1K"})
    assert book.price(other).rule_id == "model"


def test_rule_unit_char_and_token_scaling():
    book = _book(
        {"id": "tts", "match": {"kind": "audio"}, "unit": "10k_char", "price": 1.5},
        {"id": "llm", "match": {"kind": "text"}, "unit": "1k_token", "price": 0.02},
    )
    audio = book.price(
        ActionRequest(kind="audio", metrics={"billable_chars": 25000, "call_count": 3})
    )
    assert audio.cny == pytest.approx(1.5 * 2.5)

    text = book.price(ActionRequest(kind="text", metrics={"tokens": 1_250_000}))
    assert text.cny == pytest.approx(0.02 * 1250)


def test_unpriced_is_not_silently_zero():
    book = _book()
    priced = book.price(ActionRequest(kind="video", model="mystery", metrics={"duration_seconds": 5}))
    assert priced.cny is None
    assert priced.source == "unpriced"


def test_rule_without_metrics_is_unpriced():
    book = _book({"id": "v", "match": {"kind": "video"}, "unit": "second", "price": 0.5})
    priced = book.price(ActionRequest(kind="video"))
    assert priced.cny is None
    assert "缺少单位数量" in priced.note


# ---------------------------------------------------------------------------
# 同步价目
# ---------------------------------------------------------------------------


def _synced_book(*, rate=7.3):
    return _book(
        aliases={"seedance-2.0-fast": "aicf:doubao-seedance-2.0-fast"},
        synced={
            "relays": [
                {"label": "aicf", "usd_cny_rate": rate, "quota_per_unit": 500000},
                {"label": "aigc", "usd_cny_rate": 6.7, "quota_per_unit": 500000},
            ],
            "models": {
                "aicf:doubao-seedance-2.0-sc": {
                    "model_name": "doubao-seedance-2.0-sc",
                    "quota_type": 0,
                    "billing_mode": "tiered_expr",
                    "billing_expr": SEEDANCE_SC_EXPR,
                    "usage_schema": {"resolution": {}, "seconds": {}},
                },
                "aicf:doubao-seedance-2.0-fast": {
                    "model_name": "doubao-seedance-2.0-fast",
                    "quota_type": 0,
                    "billing_mode": "tiered_expr",
                    "billing_expr": SEEDANCE_FAST_EXPR,
                    "usage_schema": {"resolution": {}, "tokens": {}, "video_input": {}},
                },
                "aigc:doubao-seed-audio-1-0": {
                    "model_name": "doubao-seed-audio-1-0",
                    "quota_type": 0,
                    "billing_mode": "tiered_expr",
                    "billing_expr": AUDIO_EXPR,
                    "usage_schema": {},
                },
            },
        },
    )


def test_synced_expression_pricing_uses_relay_rate():
    book = _synced_book()
    priced = book.price(
        ActionRequest(
            kind="video",
            model="doubao-seedance-2.0-sc",
            params={"resolution": "720p"},
            metrics={"duration_seconds": 5, "call_count": 1},
        )
    )
    assert priced.source == "synced"
    assert priced.cny == pytest.approx(0.09850746 * 5 * 7.3)


def test_synced_alias_resolution():
    book = _synced_book()
    priced = book.price(
        ActionRequest(
            kind="video",
            model="seedance-2.0-fast",
            params={"video_input": "none"},
            metrics={"tokens": 1_000_000},
        )
    )
    assert priced.source == "synced"
    assert priced.cny == pytest.approx(3.03731343 * 7.3)


def test_synced_audio_uses_aigc_rate():
    book = _synced_book()
    priced = book.price(
        ActionRequest(
            kind="audio",
            model="doubao-seed-audio-1-0",
            metrics={"prompt_tokens": 1000, "completion_tokens": 1000, "call_count": 1},
        )
    )
    assert priced.cny == pytest.approx((1000 * 17.910447761194 + 1000 * 14.925373134328) * 6.7)


def test_manual_rule_beats_synced():
    book = _synced_book()
    book.raw["rules"] = [
        {"id": "override", "match": {"kind": "video", "model": "doubao-seedance-2.0-sc"}, "unit": "second", "price": 1.0}
    ]
    priced = book.price(
        ActionRequest(
            kind="video",
            model="doubao-seedance-2.0-sc",
            params={"resolution": "720p"},
            metrics={"duration_seconds": 5},
        )
    )
    assert priced.source == "manual"
    assert priced.cny == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 加价倍率
# ---------------------------------------------------------------------------


def test_markup_per_client_then_default_then_none():
    book = _book(
        client_markups={"19174820303": {"markup": 2.5}},
        markup_default=1.8,
    )
    assert book.resolve_markup("19174820303") == (2.5, "client")
    assert book.resolve_markup("191 7482 0303") == (2.5, "client")
    assert book.resolve_markup("13800000000") == (1.8, "default")

    empty = _book()
    assert empty.resolve_markup("13800000000") == (None, "")


# ---------------------------------------------------------------------------
# 读写 / 热更新
# ---------------------------------------------------------------------------


def test_load_defaults_and_save_roundtrip(prices_path):
    book = pp.load()
    assert book.rules == []
    book.raw["rules"] = [
        {"id": "x", "match": {"kind": "image"}, "unit": "call", "price": 0.9}
    ]
    pp.save(book)
    assert prices_path.is_file()

    reloaded = pp.load(force=True)
    priced = reloaded.price(ActionRequest(kind="image", quantity=2))
    assert priced.cny == pytest.approx(1.8)


def test_load_hot_reload_on_mtime_change(prices_path):
    book = pp.load()
    assert book.price(ActionRequest(kind="image")).cny is None

    prices_path.write_text(
        json.dumps({"rules": [{"id": "y", "match": {"kind": "image"}, "unit": "call", "price": 1.0}]}),
        encoding="utf-8",
    )
    # mtime 精度在不同文件系统上可能是秒级，强制刷新一下。
    refreshed = pp.load(force=True)
    assert refreshed.price(ActionRequest(kind="image")).cny == pytest.approx(1.0)


def test_broken_file_falls_back_to_defaults(prices_path):
    prices_path.write_text("{ not json", encoding="utf-8")
    book = pp.load(force=True)
    assert book.rules == []


# ---------------------------------------------------------------------------
# 同步（离线，注入 transport）
# ---------------------------------------------------------------------------


def test_sync_prices_offline(prices_path):
    def transport(method, url, headers, payload):
        if url.endswith("/api/status"):
            return 200, json.dumps(
                {"data": {"quota_per_unit": 500000, "usd_exchange_rate": 7.3}}
            )
        if url.endswith("/api/pricing"):
            return 200, json.dumps(
                {
                    "pricing_version": "abc",
                    "group_ratio": {"default": 1},
                    "data": [
                        {"model_name": "doubao-seedance-2.0-sc", "billing_mode": "tiered_expr",
                         "billing_expr": SEEDANCE_SC_EXPR, "billing_usage_schema": {"resolution": {}, "seconds": {}}}
                    ],
                }
            )
        return 404, "{}"

    book, result = pp.sync_prices(base_urls=["https://aicf.nanie168.com"], transport=transport)
    assert result["models"] == 1
    assert result["errors"] == []
    assert "aicf:doubao-seedance-2.0-sc" in book.synced_models
    assert prices_path.is_file()


def test_sync_prices_reports_errors_without_raising(prices_path):
    def transport(method, url, headers, payload):
        return 0, "boom"

    book, result = pp.sync_prices(base_urls=["https://aicf.nanie168.com"], transport=transport)
    assert result["models"] == 0
    assert result["errors"]
