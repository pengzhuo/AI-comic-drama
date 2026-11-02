"""制作成本预估：粗估与精估。"""

from __future__ import annotations

import pytest

from novelvideo import production_estimate as pe
from novelvideo.production_prices import ActionRequest, PriceBook


def _beat(n: int, scene: str, narration: str = "一二三四五六七八九十") -> dict:
    return {
        "beat_number": n,
        "scene_ref": {"scene_id": scene},
        "scene_id": scene,
        "narration_segment": narration,
        "visual_description": "画面描述",
        "audio_type": "narration",
    }


@pytest.fixture()
def beats():
    return [
        _beat(1, "s1"),
        _beat(2, "s1"),
        _beat(3, "s1"),
        _beat(4, "s2"),
        _beat(5, "s2"),
        _beat(6, "s2"),
    ]


def _book(**over) -> PriceBook:
    raw = {
        "contingency_pct": over.get("contingency_pct", 10.0),
        "assumptions": over.get("assumptions", {}),
        "rules": over.get(
            "rules",
            [
                {"id": "img", "match": {"kind": "image"}, "unit": "call", "price": 0.5},
                {"id": "vid", "match": {"kind": "video"}, "unit": "second", "price": 0.4},
                {"id": "aud", "match": {"kind": "audio"}, "unit": "call", "price": 0.2},
                {"id": "txt", "match": {"kind": "text"}, "unit": "1k_token", "price": 0.01},
            ],
        ),
        "client_markups": over.get("client_markups", {}),
        "markup_default": over.get("markup_default"),
        "synced": {"relays": [], "models": {}},
    }
    return PriceBook(raw=raw)


# ---------------------------------------------------------------------------
# 精估
# ---------------------------------------------------------------------------


def test_plan_refined_counts(beats):
    from novelvideo.generators.nanobanana_grid import sketch_scene_grid_split

    expected_grids = len(sketch_scene_grid_split(beats))
    actions = pe.plan_refined(beats=beats, episode=1, book=_book())

    images = [a for a in actions if a.kind == "image"]
    assert {a.feature_key for a in images} == {"mainline.sketch_grid", "mainline.render_grid"}
    assert all(a.quantity == expected_grids for a in images)

    video = next(a for a in actions if a.kind == "video")
    assert video.quantity == 6
    assert video.metrics["duration_seconds"] == pytest.approx(30.0)  # 6 × 默认 5 秒
    assert video.params["resolution"]
    assert video.params["video_input"] == "none"

    audio = next(a for a in actions if a.kind == "audio")
    assert audio.quantity == 6
    assert audio.metrics["billable_chars"] == 60  # 每镜头 10 字

    text = next(a for a in actions if a.kind == "text")
    assert text.metrics["tokens"] > 0


def test_plan_refined_uses_beat_duration(book=None):
    beats = [{**_beat(1, "s1"), "duration_seconds": 7}]
    actions = pe.plan_refined(beats=beats, book=_book())
    video = next(a for a in actions if a.kind == "video")
    assert video.metrics["duration_seconds"] == pytest.approx(7.0)


def test_plan_refined_empty_beats():
    actions = pe.plan_refined(beats=[], book=_book())
    assert not [a for a in actions if a.kind in {"image", "video", "audio"}]


# ---------------------------------------------------------------------------
# 粗估
# ---------------------------------------------------------------------------


def test_plan_coarse_scales_with_chars():
    book = _book(assumptions={"chars_per_beat": 50, "beats_per_episode": 100, "image_calls_per_beat": 1})
    actions = pe.plan_coarse(chars=10_000, book=book)
    video = next(a for a in actions if a.kind == "video")
    assert video.quantity == 200  # 10000 / 50
    assert video.metrics["duration_seconds"] == pytest.approx(1000.0)

    audio = next(a for a in actions if a.kind == "audio")
    assert audio.metrics["billable_chars"] == 10_000


def test_plan_coarse_respects_episode_hint():
    book = _book(assumptions={"chars_per_beat": 50})
    actions = pe.plan_coarse(chars=1000, episodes=3, book=book)
    video = next(a for a in actions if a.kind == "video")
    assert video.quantity == 20


def test_plan_coarse_never_empty():
    actions = pe.plan_coarse(chars=0, book=_book())
    assert actions  # 至少有文本类，且不会崩


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def test_build_estimate_totals_and_contingency(beats):
    actions = [
        ActionRequest(kind="image", model="LingShan-G2", quantity=2, metrics={"call_count": 2}),
        ActionRequest(kind="video", model="seedance-2.0-fast", metrics={"duration_seconds": 10}),
    ]
    result = pe.build_estimate(actions=actions, book=_book(contingency_pct=10.0), stage="refined")
    assert result["cost"]["cny"] == pytest.approx(2 * 0.5 + 10 * 0.4)
    assert result["cost"]["contingency_cny"] == pytest.approx((1.0 + 4.0) * 0.1)
    assert result["cost"]["total_cny"] == pytest.approx(5.5)
    assert result["unpriced_count"] == 0
    assert {row["kind"] for row in result["by_kind"]} == {"image", "video"}


def test_build_estimate_lists_unpriced():
    book = _book(rules=[{"id": "img", "match": {"kind": "image"}, "unit": "call", "price": 0.5}])
    actions = [
        ActionRequest(kind="image", model="LingShan-G2", quantity=1, metrics={"call_count": 1}),
        ActionRequest(kind="video", model="unknown-model", metrics={"duration_seconds": 5}),
    ]
    result = pe.build_estimate(actions=actions, book=book)
    assert result["unpriced_count"] == 1
    assert result["unpriced"][0]["kind"] == "video"
    assert result["cost"]["cny"] == pytest.approx(0.5)


def test_build_estimate_applies_client_markup():
    book = _book(client_markups={"19174820303": {"markup": 3.0}}, contingency_pct=0.0)
    actions = [ActionRequest(kind="image", model="m", quantity=2, metrics={"call_count": 2})]
    result = pe.build_estimate(actions=actions, book=book, phone="19174820303")
    assert result["cost"]["total_cny"] == pytest.approx(1.0)
    assert result["price"]["cny"] == pytest.approx(3.0)
    assert result["price"]["markup_source"] == "client"


# ---------------------------------------------------------------------------
# 客户视角脱敏（防泄利润）
# ---------------------------------------------------------------------------


def test_client_view_never_leaks_cost(beats):
    book = _book(client_markups={"19174820303": {"markup": 2.0}})
    actions = pe.plan_refined(beats=beats, book=book)
    client = pe.build_estimate(actions=actions, book=book, view="client", phone="19174820303")

    assert pe.display_safety_check(client)
    assert client["cost"] is None
    assert all(item["cny"] is None for item in client["items"])
    assert all(item["unit_price"] is None for item in client["items"])
    assert all(row["cny"] is None for row in client["by_kind"])
    assert client["price"]["cny"] is not None

    internal = pe.build_estimate(actions=actions, book=book, view="internal", phone="19174820303")
    assert internal["cost"]["cny"] > 0
    assert not pe.display_safety_check(
        {**client, "cost": {"total_cny": 1.0}}
    )  # 自检确实会抓到泄漏


def test_client_without_markup_shows_no_amount(beats):
    book = _book()
    actions = pe.plan_refined(beats=beats, book=book)
    client = pe.build_estimate(actions=actions, book=book, view="client", phone="13800000000")
    assert client["price"]["cny"] is None
    assert pe.display_safety_check(client)


# ---------------------------------------------------------------------------
# 报价单渲染
# ---------------------------------------------------------------------------


@pytest.fixture()
def internal_payload(beats):
    book = _book(client_markups={"19174820303": {"markup": 2.0}})
    actions = pe.plan_refined(beats=beats, book=book)
    return pe.build_estimate(
        actions=actions, book=book, view="internal", phone="19174820303", stage="refined"
    )


def test_render_quote_markdown_contains_totals(internal_payload):
    name, content = pe.render_quote(internal_payload, "md", title="国庆短剧")
    assert name.endswith(".md")
    assert "成本合计" in content
    assert "售价" in content
    assert "分项明细" in content


def test_render_quote_csv_has_header_and_rows(internal_payload):
    name, content = pe.render_quote(internal_payload, "csv")
    assert name.endswith(".csv")
    lines = content.strip().splitlines()
    assert lines[0] == "种类,项目,数量,单位,单价,金额"
    assert len(lines) > 3


def test_render_quote_txt_no_none_crash(internal_payload):
    _, content = pe.render_quote(internal_payload, "txt")
    assert "成本合计" in content


def test_render_quote_client_hides_cost(beats):
    book = _book(client_markups={"19174820303": {"markup": 2.0}})
    actions = pe.plan_refined(beats=beats, book=book)
    client = pe.build_estimate(actions=actions, book=book, view="client", phone="19174820303")
    for fmt in ("md", "csv", "txt"):
        _, content = pe.render_quote(client, fmt)
        assert "成本小计" not in content
        assert "成本合计" not in content
        assert "售价" in content
