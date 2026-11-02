"""制作成本预估 / 单价表路由契约。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelvideo import production_prices as prices


def _beat(n: int, scene: str, narration: str = "一二三四五六七八九十") -> dict:
    return {
        "beat_number": n,
        "scene_ref": {"scene_id": scene},
        "scene_id": scene,
        "narration_segment": narration,
        "visual_description": "画面描述",
        "audio_type": "narration",
        "duration_seconds": 5,
    }


class _FakeStore:
    def __init__(self, beats_by_episode: dict[int, list[dict]]) -> None:
        self._beats = beats_by_episode
        self.closed = False

    async def get_beats_as_dicts(self, episode: int) -> list[dict]:
        return list(self._beats.get(int(episode), []))

    async def list_episodes(self) -> list:
        return [SimpleNamespace(number=n) for n in sorted(self._beats)]

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def price_file(tmp_path, monkeypatch):
    path = tmp_path / "production-prices.json"
    path.write_text(
        json.dumps(
            {
                "contingency_pct": 10,
                "rules": [
                    {"id": "img", "match": {"kind": "image"}, "unit": "call", "price": 0.5},
                    {"id": "vid", "match": {"kind": "video"}, "unit": "second", "price": 0.4},
                    {"id": "aud", "match": {"kind": "audio"}, "unit": "call", "price": 0.2},
                    {"id": "txt", "match": {"kind": "text"}, "unit": "1k_token", "price": 0.01},
                ],
                "client_markups": {"19174820303": {"markup": 2.0}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(prices.ENV_CONFIG_PATH, str(path))
    prices._CACHE.clear()
    yield path
    prices._CACHE.clear()


def _client(tmp_path, monkeypatch, *, store=None, user=None, internal=False, gate=True):
    from novelvideo.api.routes import estimate as est_route

    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    async def fake_resolve(project, resolved_user, *, required_role="viewer", media_read=False):
        return SimpleNamespace(
            ctx=SimpleNamespace(project_id="proj_demo"),
            username="alice",
            project_name=project,
            project_dir=project_dir,
            output_dir=str(project_dir),
            state_dir=str(project_dir),
            runtime_dir=str(tmp_path / "runtime"),
        )

    async def fake_store(ctx):
        return store or _FakeStore({})

    monkeypatch.setattr(est_route, "resolve_project_scope", fake_resolve)
    monkeypatch.setattr(est_route, "make_cognee_store_for_context", fake_store)
    monkeypatch.setattr(
        est_route.studio_access, "verify_internal_cookie", lambda value: bool(internal)
    )
    monkeypatch.setattr(est_route.studio_access, "enabled", lambda: bool(gate))

    app = FastAPI()
    app.include_router(est_route.router, prefix="/api/v1")
    app.dependency_overrides[est_route.get_api_user] = lambda: (
        user or {"username": "alice", "credential_kind": "internal_console"}
    )
    return TestClient(app), est_route


def test_estimate_coarse_contract(tmp_path, monkeypatch, price_file):
    client, _ = _client(tmp_path, monkeypatch)
    response = client.get(
        "/api/v1/projects/demo/production-estimate", params={"chars": 10_000}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["stage"] == "coarse"
    assert data["view"] == "internal"
    assert data["cost"]["cny"] > 0
    assert data["unpriced_count"] == 0
    assert data["project"] == "demo"


def test_estimate_refined_contract(tmp_path, monkeypatch, price_file):
    store = _FakeStore({1: [_beat(i, "s1") for i in range(1, 7)]})
    client, _ = _client(tmp_path, monkeypatch, store=store)
    response = client.get(
        "/api/v1/projects/demo/production-estimate", params={"episode": 1}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["stage"] == "refined"
    assert data["episode"] == 1
    kinds = {item["kind"] for item in data["items"]}
    assert {"image", "video", "audio", "text"} <= kinds
    assert store.closed is True


def test_estimate_client_view_is_masked_over_http(tmp_path, monkeypatch, price_file):
    store = _FakeStore({1: [_beat(i, "s1") for i in range(1, 5)]})
    user = {"user_id": "client:19174820303", "credential_kind": "studio_client", "phone": "19174820303"}
    client, _ = _client(tmp_path, monkeypatch, store=store, user=user)
    response = client.get(
        "/api/v1/projects/demo/production-estimate", params={"episode": 1}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["view"] == "client"
    assert data["cost"] is None
    assert all(item["cny"] is None and item["unit_price"] is None for item in data["items"])
    assert data["price"]["cny"] is not None
    # 原始响应文本里不能出现成本键（防泄漏）
    assert '"cost":null' in response.text


def test_estimate_internal_can_preview_client(tmp_path, monkeypatch, price_file):
    store = _FakeStore({1: [_beat(i, "s1") for i in range(1, 5)]})
    client, _ = _client(tmp_path, monkeypatch, store=store)
    response = client.get(
        "/api/v1/projects/demo/production-estimate",
        params={"episode": 1, "as_client": "19174820303"},
    )
    data = response.json()["data"]
    assert data["view"] == "client"
    assert data["price"]["cny"] is not None


def test_quote_route_formats(tmp_path, monkeypatch, price_file):
    store = _FakeStore({1: [_beat(i, "s1") for i in range(1, 5)]})
    client, _ = _client(tmp_path, monkeypatch, store=store)
    csv = client.get(
        "/api/v1/projects/demo/production-estimate/quote",
        params={"episode": 1, "format": "csv"},
    )
    assert csv.status_code == 200
    body = csv.json()["data"]
    assert body["filename"].endswith(".csv")
    assert body["content"].startswith("种类,项目,数量,单位,单价,金额")

    bad = client.get(
        "/api/v1/projects/demo/production-estimate/quote", params={"format": "pdf"}
    )
    assert bad.status_code == 400


def test_settings_requires_internal_session(tmp_path, monkeypatch, price_file):
    client, _ = _client(tmp_path, monkeypatch, internal=False)
    denied = client.get("/api/v1/settings/production-prices")
    assert denied.status_code == 403

    client2, _ = _client(tmp_path, monkeypatch, internal=True)
    ok = client2.get("/api/v1/settings/production-prices")
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["rules"]
    assert "units" in data


def test_settings_put_updates_and_validates(tmp_path, monkeypatch, price_file):
    client, _ = _client(tmp_path, monkeypatch, internal=True)
    bad = client.put(
        "/api/v1/settings/production-prices",
        json={"rules": [{"id": "x", "unit": "fortnight", "price": 1}]},
    )
    assert bad.status_code == 400

    ok = client.put(
        "/api/v1/settings/production-prices",
        json={"markup_default": 1.5, "assumptions": {"chars_per_beat": 42}},
    )
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["markup_default"] == 1.5
    assert data["assumptions"]["chars_per_beat"] == 42

    # 持久化到磁盘
    stored = json.loads(price_file.read_text(encoding="utf-8"))
    assert stored["markup_default"] == 1.5


def test_settings_sync_and_calibrate(tmp_path, monkeypatch, price_file):
    client, est_route = _client(tmp_path, monkeypatch, internal=True)

    monkeypatch.setattr(
        prices,
        "sync_prices",
        lambda book=None, **kw: (prices.load(), {"models": 3, "relays": [], "errors": []}),
    )
    synced = client.post("/api/v1/settings/production-prices/sync")
    assert synced.status_code == 200
    assert synced.json()["data"]["models"] == 3

    monkeypatch.setattr(
        prices,
        "calibrate_from_project",
        lambda project_dir, **kw: {
            "rules": [
                {
                    "id": "actual:image:LingShan-G2",
                    "kind": "image",
                    "model": "LingShan-G2",
                    "unit": "call",
                    "price": 0.77,
                    "samples": 12,
                }
            ],
            "metadata": {"computed_at": "2026-11-02T00:00:00"},
        },
    )
    preview = client.post(
        "/api/v1/settings/production-prices/calibrate", params={"project": "demo"}
    )
    assert preview.status_code == 200
    assert preview.json()["data"]["preview"] is True

    applied = client.post(
        "/api/v1/settings/production-prices/calibrate",
        params={"project": "demo", "apply": True},
    )
    assert applied.status_code == 200
    body = applied.json()["data"]
    assert body["preview"] is False
    assert body["price_book"]["actual_rules"][0]["price"] == 0.77


def test_calibrate_requires_owner_scope(tmp_path, monkeypatch, price_file):
    client, est_route = _client(tmp_path, monkeypatch, internal=True)
    seen = {}

    async def fake_resolve(project, user, *, required_role="viewer", media_read=False):
        seen["role"] = required_role
        return SimpleNamespace(
            ctx=SimpleNamespace(project_id="proj_demo"),
            project_dir=tmp_path,
            output_dir=str(tmp_path),
            state_dir=str(tmp_path),
            runtime_dir=str(tmp_path),
        )

    monkeypatch.setattr(est_route, "resolve_project_scope", fake_resolve)
    monkeypatch.setattr(
        prices, "calibrate_from_project", lambda project_dir, **kw: {"rules": [], "metadata": {}}
    )
    response = client.post(
        "/api/v1/settings/production-prices/calibrate", params={"project": "demo"}
    )
    assert response.status_code == 200
    assert seen["role"] == "owner"
