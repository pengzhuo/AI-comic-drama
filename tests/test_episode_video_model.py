"""每集视频模型选择：存储、解析、估价贯通与价格表绑定。

客户在「上传剧本」处选项目默认视频模型，在「每集报价」处可逐集覆盖。所选模型
必须（a）落进项目配置，（b）贯通报价/预扣，（c）同步上游价目后能绑到本地 id。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from novelvideo import production_estimate as estimate
from novelvideo import production_prices as prices
from novelvideo import project_config as pc


@pytest.fixture()
def state_root(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setattr(pc, "OUTPUT_DIR", root, raising=False)
    monkeypatch.setattr(pc, "STATE_DIR", root, raising=False)
    return root


def _beat(n: int, scene: str = "s1") -> dict:
    return {
        "beat_number": n,
        "scene_ref": {"scene_id": scene},
        "scene_id": scene,
        "narration_segment": "一二三四五六七八九十",
        "visual_description": "画面描述",
        "audio_type": "narration",
        "duration_seconds": 5,
    }


# ---------------------------------------------------------------------------
# 目录 id ↔ 后端值
# ---------------------------------------------------------------------------


def test_video_backend_value_and_catalog_id_roundtrip():
    assert pc.video_backend_value("seedance-2.0-mini") == "newapi_seedance-2.0-mini"
    assert pc.video_backend_value("newapi_seedance-2.5") == "newapi_seedance-2.5"
    assert pc.video_backend_value("") == ""
    assert pc.video_catalog_id("newapi_seedance-2.0-fast") == "seedance-2.0-fast"
    assert pc.video_catalog_id("seedance-2.5") == "seedance-2.5"
    assert pc.video_catalog_id("") == ""


# ---------------------------------------------------------------------------
# 每集覆盖的读写 / 解析
# ---------------------------------------------------------------------------


def test_set_and_clear_episode_video_backend(state_root):
    pc.set_episode_video_backend("alice", "demo", 2, "newapi_seedance-2.0-mini")
    config = pc.load_project_config("alice", "demo")
    assert pc.episode_video_backend(config, 2) == "newapi_seedance-2.0-mini"

    pc.set_episode_video_backend("alice", "demo", 2, "")
    config = pc.load_project_config("alice", "demo")
    assert pc.episode_video_backend(config, 2) == ""
    assert pc.EPISODE_VIDEO_BACKENDS_KEY not in config


def test_effective_video_model_for_episode_prefers_override(state_root):
    pc.save_project_config("alice", "demo", {"video_backend": "newapi_seedance-2.5"})
    pc.set_episode_video_backend("alice", "demo", 3, "newapi_seedance-2.0-fast")

    assert pc.effective_video_model_for_episode("alice", "demo", 3) == "seedance-2.0-fast"
    # 没覆盖的集回落项目默认
    assert pc.effective_video_model_for_episode("alice", "demo", 4) == "seedance-2.5"


def test_resolve_video_model_prefers_episode_override(state_root):
    from novelvideo.api.routes import estimate as est_route

    pc.save_project_config("alice", "demo", {"video_backend": "newapi_seedance-2.5"})
    pc.set_episode_video_backend("alice", "demo", 2, "newapi_seedance-2.0-mini")
    resolution = SimpleNamespace(username="alice", project_name="demo")

    assert est_route._resolve_video_model(resolution, 2) == "seedance-2.0-mini"
    assert est_route._resolve_video_model(resolution, 9) == "seedance-2.5"


# ---------------------------------------------------------------------------
# 估价按所选模型落值
# ---------------------------------------------------------------------------


def test_plan_refined_uses_video_model():
    beats = [_beat(i) for i in range(1, 4)]
    actions = estimate.plan_refined(beats=beats, episode=1, video_model="seedance-2.0-mini")
    video = [action for action in actions if action.kind == "video"]
    assert video and video[0].model == "seedance-2.0-mini"


def test_plan_coarse_uses_video_model():
    actions = estimate.plan_coarse(chars=10_000, video_model="seedance-2.0")
    video = [action for action in actions if action.kind == "video"]
    assert video and video[0].model == "seedance-2.0"


# ---------------------------------------------------------------------------
# 上游同步价目：厂商前缀后缀匹配
# ---------------------------------------------------------------------------


def _synced_book(tmp_path, monkeypatch, models: dict) -> prices.PriceBook:
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {
                "synced": {
                    "relays": [
                        {"label": "aiqy", "usd_cny_rate": 7.3, "quota_per_unit": 500000}
                    ],
                    "models": models,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(prices.ENV_CONFIG_PATH, str(path))
    prices._CACHE.clear()
    return prices.load(force=True)


def test_synced_suffix_match_binds_local_id(tmp_path, monkeypatch):
    book = _synced_book(
        tmp_path,
        monkeypatch,
        {
            "aiqy:doubao-seedance-2.5": {"quota_type": 1, "model_price": 0.5},
            "aiqy:doubao-seedance-2.0-mini": {"quota_type": 1, "model_price": 0.2},
        },
    )
    try:
        assert book._resolve_synced_model("seedance-2.5")[0] == "aiqy:doubao-seedance-2.5"
        assert (
            book._resolve_synced_model("seedance-2.0-mini")[0]
            == "aiqy:doubao-seedance-2.0-mini"
        )
    finally:
        prices._CACHE.clear()


def test_synced_suffix_ambiguous_is_not_bound(tmp_path, monkeypatch):
    book = _synced_book(
        tmp_path,
        monkeypatch,
        {
            "aiqy:doubao-seedance-2.5": {"quota_type": 1, "model_price": 0.5},
            "aicf:vendor-seedance-2.5": {"quota_type": 1, "model_price": 0.6},
        },
    )
    # 清掉出厂默认 alias，制造「没有任何偏好中转站」的最坏情况。
    book.raw["aliases"] = {}
    try:
        assert book._resolve_synced_model("seedance-2.5") is None
    finally:
        prices._CACHE.clear()


def test_synced_suffix_prefers_relay_used_by_alias(tmp_path, monkeypatch):
    """多镜像歧义时，优先价目表已经在用的中转站（其它 alias 指过的 label）。"""

    book = _synced_book(
        tmp_path,
        monkeypatch,
        {
            "aicf:doubao-seedance-2-0": {"quota_type": 1, "model_price": 0.5},
            "aiqy:doubao-seedance-2-0": {"quota_type": 1, "model_price": 0.7},
        },
    )
    book.raw["aliases"] = {"seedance-2.0-fast": "aiqy:doubao-seedance-2.0-fast"}
    try:
        assert (
            book._resolve_synced_model("seedance-2.0")[0] == "aiqy:doubao-seedance-2-0"
        )
    finally:
        prices._CACHE.clear()


# ---------------------------------------------------------------------------
# 端点：每集视频模型读写
# ---------------------------------------------------------------------------


def test_episode_video_model_endpoint_roundtrip(state_root, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from novelvideo.api.routes import estimate as est_route

    async def fake_resolve(project, user, *, required_role="viewer", media_read=False):
        return SimpleNamespace(
            ctx=SimpleNamespace(project_id="proj_demo"),
            username="alice",
            project_name=project,
            project_dir=tmp_path,
            output_dir=str(tmp_path),
            state_dir=str(tmp_path),
            runtime_dir=str(tmp_path / "rt"),
        )

    monkeypatch.setattr(est_route, "resolve_project_scope", fake_resolve)

    app = FastAPI()
    app.include_router(est_route.router, prefix="/api/v1")
    app.dependency_overrides[est_route.get_api_user] = lambda: {
        "username": "alice",
        "credential_kind": "internal_console",
    }
    client = TestClient(app)

    got = client.get("/api/v1/projects/demo/episodes/2/video-model")
    assert got.status_code == 200
    data = got.json()["data"]
    assert data["video_model"] == pc.DEFAULT_SELECTABLE_VIDEO_MODEL
    assert {option["id"] for option in data["options"]} == set(pc.SELECTABLE_VIDEO_MODELS)

    put = client.put(
        "/api/v1/projects/demo/episodes/2/video-model",
        json={"video_model": "seedance-2.0-mini"},
    )
    assert put.status_code == 200
    assert put.json()["data"]["override_model"] == "seedance-2.0-mini"

    bad = client.put(
        "/api/v1/projects/demo/episodes/2/video-model",
        json={"video_model": "gpt-4"},
    )
    assert bad.status_code == 400
