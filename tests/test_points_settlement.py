"""成片完成后按实际用量多退少补（伴点结算）。

覆盖：状态机（settled/owed/pending/refund_pending）、幂等、补缴重试、交付闸门、
按集 relay 口径汇总。全部用假件，绝不出网。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelvideo import points_client, points_settlement, production_prices
from novelvideo import project_config
from novelvideo.api.routes import points as points_route
from novelvideo.gateway_cost_reconcile import episode_quota
from novelvideo.project_config import points_settlement_in_state_dir

CLIENT_PHONE = "13800138000"

#: 一集实收 quota → ¥ → 售价伴点（与 _actual_cny_from_quota / 报价卡同口径）。
QUOTA = 7_300_000
ACTUAL_CNY = QUOTA / 500_000 * 7.3  # 106.58 ¥
SETTLEMENT_POINTS = int(round(ACTUAL_CNY * 1.15 * 2.5 * 100))  # 30642 伴点


def _client_user(**overrides) -> dict:
    base = {
        "user_id": f"client:{CLIENT_PHONE}",
        "username": CLIENT_PHONE,
        "credential_kind": "studio_client",
        "phone": CLIENT_PHONE,
    }
    base.update(overrides)
    return base


def _operator(**overrides) -> dict:
    base = {
        "user_id": "local",
        "username": "local",
        "credential_kind": "internal_console",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------


class FakeBook:
    def __init__(self, markup: float | None = 2.5, contingency: float = 15.0):
        self._markup = markup
        self.contingency_pct = contingency

    def resolve_markup(self, phone: str):
        if self._markup is None:
            return None, ""
        return self._markup, "client"


class FakePointsClient:
    """记录扣/退；可按需抛错。"""

    calls: list[dict] = []
    errors: list[Exception] = []

    def __init__(self, config=None, transport=None):  # noqa: ARG002
        self.config = config
        FakePointsClient.last = self

    def _move(self, optype: int, *, phone: str, amount: int, projects_uuid: str) -> dict:
        entry = {
            "optype": optype,
            "phone": phone,
            "amount": amount,
            "projectsuuid": projects_uuid,
        }
        FakePointsClient.calls.append(entry)
        error = FakePointsClient.errors.pop(0) if FakePointsClient.errors else None
        if error is not None:
            raise error
        return {"code": 0, "message": "操作成功", "data": []}

    def charge(self, *, phone: str, amount: int, projects_uuid: str) -> dict:
        return self._move(1, phone=phone, amount=amount, projects_uuid=projects_uuid)

    def refund(self, *, phone: str, amount: int, projects_uuid: str) -> dict:
        return self._move(2, phone=phone, amount=amount, projects_uuid=projects_uuid)


def _seed_prepay(state_dir: Path, episode: int, *, amount: int, phone: str = CLIENT_PHONE) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "project_config.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.setdefault("points_charges", {})[str(episode)] = {
        "amount": amount,
        "phone": phone,
        "at": "2026-11-03T00:00:00+00:00",
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_usage_db(state_dir: Path, rows: list[tuple[str, int | None, int | None]]) -> Path:
    """(request_id, episode, cost_quota) → 项目 data.db。"""

    state_dir.mkdir(parents=True, exist_ok=True)
    db = state_dir / "data.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE video_request_usage ("
        "request_id TEXT, provider TEXT, model_name TEXT, episode INTEGER, "
        "duration_seconds REAL, cost_quota INTEGER)"
    )
    for request_id, episode, quota in rows:
        conn.execute(
            "INSERT INTO video_request_usage VALUES (?,?,?,?,?,?)",
            (request_id, "newapi", "seedance-2.0-fast", episode, 5.0, quota),
        )
    conn.commit()
    conn.close()
    return db


def _client_state_dir(tmp_path: Path, name: str = "proj") -> Path:
    """客户项目形态的状态目录：父目录名是手机号（``owner_username`` 契约）。"""

    return tmp_path / CLIENT_PHONE / name


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """settle=compose + 假 PointsClient + 假价格表；返回状态容器。"""

    state_dir = tmp_path / "state" / "project"
    store = SimpleNamespace(state_dir=state_dir, maps={}, errors=[], booked=FakeBook())
    FakePointsClient.calls = []
    FakePointsClient.errors = []
    FakePointsClient.last = None

    monkeypatch.setattr(points_client, "PointsClient", FakePointsClient)
    monkeypatch.setattr(
        points_client,
        "load",
        lambda **kwargs: points_client.PointsConfig(
            enabled=True,
            base_url="https://example.test/dopoints.php",
            sign_key="test-sign-key",
            settle="compose",
        ),
    )
    monkeypatch.setattr(
        production_prices, "load", lambda **kwargs: store.booked
    )

    def fake_relay_maps(project_db, *, workers=4):  # noqa: ARG001
        if store.errors:
            raise RuntimeError("relay down")
        return dict(store.maps), {}, []

    monkeypatch.setattr(points_settlement, "_relay_maps", fake_relay_maps)
    return SimpleNamespace(state_dir=state_dir, maps=store.maps, booked=store.booked, errors=store.errors)


async def _settle(env, episode: int = 1, **kwargs):
    return await points_settlement.settle_episode(
        state_dir=env.state_dir,
        episode=episode,
        projects_uuid="01M5TESTPROJECT0000000000",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 路径识别
# ---------------------------------------------------------------------------


def test_final_video_episode_matches_canonical_path():
    assert points_settlement.final_video_episode("videos/episodes/ep003_final.mp4") == 3
    assert (
        points_settlement.final_video_episode("/videos/episodes/ep12_final.mp4") == 12
    )
    assert points_settlement.final_video_episode("videos\\episodes\\ep7_final.mp4") == 7
    assert points_settlement.final_video_episode("videos/episodes/ep003.mp4") is None
    assert points_settlement.final_video_episode("frames/ep003/1.png") is None


def test_owed_message_wording_depends_on_prepaid():
    assert points_settlement.owed_message(900) == (
        "本集实际用量超过预扣，需补缴 900 伴点后解锁成片"
    )
    assert points_settlement.owed_message(900, prepaid=0) == (
        "本集尚未支付制作伴点，需补缴 900 伴点后解锁成片"
    )


# ---------------------------------------------------------------------------
# episode_quota（按集汇总，relay 只认对上的行）
# ---------------------------------------------------------------------------


def test_episode_quota_only_counts_relay_matched_rows(tmp_path):
    db = _make_usage_db(
        tmp_path / "p",
        [("t1", 1, None), ("t2", 1, None), ("t3", 2, None)],
    )
    stat = episode_quota(db, 1, task_quota={"t1": 100}, log_quota={"t2": 250})
    assert stat == {"quota": 350, "rows": 2, "matched": 2, "priced": 0}
    assert episode_quota(db, 2, task_quota={"t1": 100})["rows"] == 1
    assert episode_quota(db, 2, task_quota={"t1": 100})["matched"] == 0


def test_episode_quota_without_maps_sums_stored_cost_quota(tmp_path):
    db = _make_usage_db(tmp_path / "p", [("t1", 1, 500), ("t2", 1, None)])
    stat = episode_quota(db, 1)
    assert stat["quota"] == 500
    assert stat["priced"] == 1


# ---------------------------------------------------------------------------
# settle_episode
# ---------------------------------------------------------------------------


async def test_disabled_settle_is_a_noop(env, monkeypatch):
    monkeypatch.setattr(
        points_client,
        "load",
        lambda **kwargs: points_client.PointsConfig(enabled=True, settle="off"),
    )
    _seed_prepay(env.state_dir, 1, amount=1000)
    assert await _settle(env) is None


async def test_episode_without_prepay_is_not_ours(env):
    _make_usage_db(env.state_dir, [("t1", 1, 1000)])
    _seed_prepay(env.state_dir, 1, amount=0)
    assert await _settle(env) is None


async def test_unprepaid_client_episode_charges_full_and_settles(env, tmp_path):
    """客户项目 + gate 开 + 从未预扣：按实际用量**全额**补收，不再放行。"""

    client_dir = _client_state_dir(tmp_path)
    _make_usage_db(client_dir, [("t1", 1, 0)])
    env.maps["t1"] = QUOTA
    entry = await points_settlement.settle_episode(
        state_dir=client_dir,
        episode=1,
        projects_uuid="01M5TESTPROJECT0000000000",
    )
    assert entry["status"] == points_settlement.STATUS_SETTLED
    assert entry["prepaid"] == 0
    assert entry["settlement"] == SETTLEMENT_POINTS
    assert entry["charged"] == SETTLEMENT_POINTS
    assert FakePointsClient.calls == [
        {
            "optype": 1,
            "phone": CLIENT_PHONE,
            "amount": SETTLEMENT_POINTS,
            "projectsuuid": "01M5TESTPROJECT0000000000",
        }
    ]
    assert points_settlement.delivery_block(state_dir=client_dir, episode=1) is None


async def test_unprepaid_client_insufficient_blocks_as_owed(env, tmp_path):
    client_dir = _client_state_dir(tmp_path)
    _make_usage_db(client_dir, [("t1", 1, 0)])
    env.maps["t1"] = QUOTA
    FakePointsClient.errors.append(points_client.PointsNotEnough("伴点不足，请先充值"))
    entry = await points_settlement.settle_episode(state_dir=client_dir, episode=1)
    assert entry["status"] == points_settlement.STATUS_OWED
    assert entry["prepaid"] == 0
    assert entry["phone"] == CLIENT_PHONE
    block = points_settlement.delivery_block(state_dir=client_dir, episode=1)
    assert block is not None
    assert block.status == points_settlement.STATUS_OWED
    assert block.owed_points == SETTLEMENT_POINTS
    assert block.message == "伴点不足，请先充值"


async def test_unprepaid_client_relay_unknown_blocks_as_unpriced(env, tmp_path):
    """算不出实际用量时，无预扣集 fail-closed（unpriced），不放行。"""

    client_dir = _client_state_dir(tmp_path)
    _make_usage_db(client_dir, [("t1", 1, 0)])
    env.maps.clear()  # 对不上 → 算不出实际用量
    entry = await points_settlement.settle_episode(state_dir=client_dir, episode=1)
    assert entry["status"] == points_settlement.STATUS_UNPRICED
    assert FakePointsClient.calls == []
    block = points_settlement.delivery_block(state_dir=client_dir, episode=1)
    assert block is not None
    assert block.status == points_settlement.STATUS_UNPRICED


async def test_unprepaid_client_retry_after_topup_charges(env, tmp_path):
    client_dir = _client_state_dir(tmp_path)
    _make_usage_db(client_dir, [("t1", 1, 0)])
    env.maps["t1"] = QUOTA
    FakePointsClient.errors.append(points_client.PointsNotEnough("伴点不足，请先充值"))
    first = await points_settlement.settle_episode(state_dir=client_dir, episode=1)
    assert first["status"] == points_settlement.STATUS_OWED

    retried = await points_settlement.settle_episode(
        state_dir=client_dir, episode=1, force=True
    )
    assert retried["status"] == points_settlement.STATUS_SETTLED
    assert retried["charged"] == SETTLEMENT_POINTS
    assert points_settlement.delivery_block(state_dir=client_dir, episode=1) is None


async def test_no_relay_match_is_pending_and_allows(env):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=23081)
    env.maps.clear()
    entry = await _settle(env)
    assert entry["status"] == points_settlement.STATUS_PENDING
    assert FakePointsClient.calls == []
    assert points_settlement.delivery_block(state_dir=env.state_dir, episode=1) is None


async def test_relay_failure_is_pending_and_allows(env):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=23081)
    env.errors.append(RuntimeError("relay down"))
    entry = await _settle(env)
    assert entry["status"] == points_settlement.STATUS_PENDING
    assert points_settlement.delivery_block(state_dir=env.state_dir, episode=1) is None


async def test_missing_markup_is_pending(env):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=23081)
    env.maps["t1"] = 7_300_000
    env.booked._markup = None
    entry = await _settle(env)
    assert entry["status"] == points_settlement.STATUS_PENDING


async def test_actual_over_prepay_charges_difference(env):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=23081)
    env.maps["t1"] = QUOTA  # 实收 106.58 ¥ → 售价 306.42 ¥ → 30642 伴点
    entry = await _settle(env)
    assert entry["status"] == points_settlement.STATUS_SETTLED
    assert entry["settlement"] == SETTLEMENT_POINTS
    assert entry["delta"] == SETTLEMENT_POINTS - 23081
    assert entry["charged"] == SETTLEMENT_POINTS - 23081
    assert FakePointsClient.calls == [
        {
            "optype": 1,
            "phone": CLIENT_PHONE,
            "amount": SETTLEMENT_POINTS - 23081,
            "projectsuuid": "01M5TESTPROJECT0000000000",
        }
    ]
    assert points_settlement.delivery_block(state_dir=env.state_dir, episode=1) is None


async def test_insufficient_balance_blocks_with_interface_message(env):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=100)
    env.maps["t1"] = 7_300_000
    FakePointsClient.errors.append(points_client.PointsNotEnough("伴点不足，请先充值"))
    entry = await _settle(env)
    assert entry["status"] == points_settlement.STATUS_OWED
    assert entry["reason"] == points_settlement.REASON_INSUFFICIENT
    assert entry["message"] == "伴点不足，请先充值"
    assert entry["charged"] == 0
    block = points_settlement.delivery_block(state_dir=env.state_dir, episode=1)
    assert block is not None
    assert block.owed_points == entry["delta"]
    assert block.message == "伴点不足，请先充值"


async def test_transport_failure_is_fail_closed(env):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=100)
    env.maps["t1"] = 7_300_000
    FakePointsClient.errors.append(points_client.PointsUnavailable("HTTP 503"))
    entry = await _settle(env)
    assert entry["status"] == points_settlement.STATUS_OWED
    assert entry["reason"] == points_settlement.REASON_UNAVAILABLE
    assert entry["message"] == points_settlement.UNAVAILABLE_MESSAGE
    assert points_settlement.delivery_block(state_dir=env.state_dir, episode=1) is not None


async def test_owed_retry_with_force_charges_remainder_and_settles(env):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=100)
    env.maps["t1"] = 7_300_000
    FakePointsClient.errors.append(points_client.PointsNotEnough("伴点不足，请先充值"))
    entry = await _settle(env)
    assert entry["status"] == points_settlement.STATUS_OWED

    # 客户充值后再点一次「补缴」。
    retried = await _settle(env, force=True)
    assert retried["status"] == points_settlement.STATUS_SETTLED
    assert retried["charged"] == retried["delta"]
    assert FakePointsClient.calls[-1]["amount"] == retried["delta"]
    assert points_settlement.delivery_block(state_dir=env.state_dir, episode=1) is None


async def test_owed_retry_without_force_does_not_charge(env):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=100)
    env.maps["t1"] = 7_300_000
    FakePointsClient.errors.append(points_client.PointsNotEnough("伴点不足，请先充值"))
    await _settle(env)
    calls_before = len(FakePointsClient.calls)
    entry = await _settle(env)
    assert entry["status"] == points_settlement.STATUS_OWED
    assert len(FakePointsClient.calls) == calls_before


async def test_concurrent_owed_retries_charge_once(env):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=100)
    env.maps["t1"] = QUOTA
    FakePointsClient.errors.append(points_client.PointsNotEnough("伴点不足，请先充值"))
    first = await _settle(env)
    assert first["status"] == points_settlement.STATUS_OWED
    calls_before = len(FakePointsClient.calls)

    results = await asyncio.gather(
        _settle(env, force=True), _settle(env, force=True), _settle(env, force=True)
    )
    # 并列补缴只扣一次（同一 (循环, 项目, 集) 串行化）。
    assert len(FakePointsClient.calls) == calls_before + 1
    assert {entry["status"] for entry in results} == {points_settlement.STATUS_SETTLED}
    assert points_settlement.delivery_block(state_dir=env.state_dir, episode=1) is None


async def test_actual_below_prepay_refunds(env):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=50000)
    env.maps["t1"] = QUOTA
    entry = await _settle(env)
    assert entry["status"] == points_settlement.STATUS_SETTLED
    assert entry["refunded"] == 50000 - SETTLEMENT_POINTS
    assert FakePointsClient.calls == [
        {
            "optype": 2,
            "phone": CLIENT_PHONE,
            "amount": 50000 - SETTLEMENT_POINTS,
            "projectsuuid": "01M5TESTPROJECT0000000000",
        }
    ]


async def test_refund_failure_allows_delivery(env):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=50000)
    env.maps["t1"] = 7_300_000
    FakePointsClient.errors.append(points_client.PointsUnavailable("HTTP 503"))
    entry = await _settle(env)
    assert entry["status"] == points_settlement.STATUS_REFUND_PENDING
    assert points_settlement.delivery_block(state_dir=env.state_dir, episode=1) is None


async def test_settlement_is_idempotent_even_when_cost_grows(env):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=50000)
    env.maps["t1"] = 7_300_000
    first = await _settle(env)
    assert first["status"] == points_settlement.STATUS_SETTLED
    calls_before = len(FakePointsClient.calls)

    # 交付后成本再涨：终态写死，不追扣、不再动钱。
    env.maps["t1"] = 30_000_000
    second = await _settle(env, force=True)
    assert second == first
    assert len(FakePointsClient.calls) == calls_before


async def test_tolerance_absorbs_small_difference(env, monkeypatch):
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    # 结算 SETTLEMENT_POINTS，预扣 31000 → 差额 358，落在 10% 容差（3100）内。
    _seed_prepay(env.state_dir, 1, amount=31000)
    env.maps["t1"] = QUOTA
    monkeypatch.setattr(
        points_client,
        "load",
        lambda **kwargs: points_client.PointsConfig(
            enabled=True, settle="compose", settle_tolerance_pct=10.0
        ),
    )
    entry = await _settle(env)
    assert entry["status"] == points_settlement.STATUS_SETTLED
    assert entry["settlement"] == SETTLEMENT_POINTS
    assert FakePointsClient.calls == []


# ---------------------------------------------------------------------------
# 交付闸门
# ---------------------------------------------------------------------------


def test_gate_delivery_blocks_only_clients(env):
    _seed_prepay(env.state_dir, 1, amount=100)
    project_config.record_points_settlement_in_state_dir(
        env.state_dir,
        1,
        status=points_settlement.STATUS_OWED,
        delta=900,
        charged=0,
        message="需补缴 900 伴点",
    )
    block = points_settlement.gate_delivery(
        user=_client_user(), state_dir=env.state_dir, episode=1
    )
    assert block is not None and block.owed_points == 900
    # 内部端不受限；以客户视角预览时同样按客户处理。
    assert (
        points_settlement.gate_delivery(user=_operator(), state_dir=env.state_dir, episode=1)
        is None
    )
    preview = points_settlement.gate_delivery(
        user=_operator(),
        state_dir=env.state_dir,
        episode=1,
        as_client=CLIENT_PHONE,
    )
    assert preview is not None and preview.owed_points == 900


def test_gate_delivery_without_settlement_entry_is_inflight(env, monkeypatch):
    _seed_prepay(env.state_dir, 1, amount=10000)
    scheduled: list[dict] = []
    monkeypatch.setattr(
        points_settlement,
        "schedule_settle",
        lambda **kwargs: scheduled.append(kwargs) or True,
    )
    block = points_settlement.gate_delivery(
        user=_client_user(), state_dir=env.state_dir, episode=1
    )
    assert block is not None
    assert block.status == points_settlement.STATUS_INFLIGHT
    assert scheduled and scheduled[0]["episode"] == 1


def test_gate_delivery_passes_without_prepay_for_non_client_dir(env):
    """``env.state_dir`` 的父目录不是手机号（非客户项目）→ 无预扣仍放行。"""

    assert (
        points_settlement.gate_delivery(
            user=_client_user(), state_dir=env.state_dir, episode=1
        )
        is None
    )


def test_gate_delivery_unprepaid_client_blocks_and_schedules(env, tmp_path, monkeypatch):
    """客户项目 + gate 开 + 无预扣：交付闸门拦下并触发一次补收。"""

    client_dir = _client_state_dir(tmp_path)
    scheduled: list[dict] = []
    monkeypatch.setattr(
        points_settlement,
        "schedule_settle",
        lambda **kwargs: scheduled.append(kwargs) or True,
    )
    block = points_settlement.gate_delivery(
        user=_client_user(), state_dir=client_dir, episode=1
    )
    assert block is not None
    assert block.status == points_settlement.STATUS_INFLIGHT
    assert scheduled and scheduled[0]["episode"] == 1


def test_unprepaid_gate_disabled_passes(env, tmp_path):
    """运维把 ``production_estimate_gate`` 关掉 → 明确不收费，无预扣也放行。"""

    client_dir = _client_state_dir(tmp_path)
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "project_config.json").write_text(
        json.dumps({"production_estimate_gate": False}), encoding="utf-8"
    )
    assert points_settlement.delivery_block(state_dir=client_dir, episode=1) is None
    assert (
        points_settlement.gate_delivery(
            user=_client_user(), state_dir=client_dir, episode=1
        )
        is None
    )


def test_unprepaid_non_client_dir_passes(env, tmp_path):
    """内部项目（owner 目录 ``local``）无预扣 → 与收费无关。"""

    internal_dir = tmp_path / "local" / "01M5TESTPROJECT0000000000"
    assert points_settlement.delivery_block(state_dir=internal_dir, episode=1) is None


def test_manual_release_unlocks_delivery(env):
    _seed_prepay(env.state_dir, 1, amount=100)
    project_config.record_points_settlement_in_state_dir(
        env.state_dir,
        1,
        status=points_settlement.STATUS_OWED,
        delta=900,
        charged=0,
        message="需补缴 900 伴点",
    )
    project_config.record_points_settlement_in_state_dir(
        env.state_dir,
        1,
        status=points_settlement.STATUS_SETTLED_MANUAL,
        by="local",
        action="manual_release",
        charged=0,
        refunded=0,
    )
    assert (
        points_settlement.gate_delivery(
            user=_client_user(), state_dir=env.state_dir, episode=1
        )
        is None
    )
    entry = points_settlement_in_state_dir(env.state_dir, 1)
    assert entry is not None and entry["status"] == points_settlement.STATUS_SETTLED_MANUAL


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


def test_population_blocks_unpriced():
    data = points_route._population(
        {"status": points_settlement.STATUS_UNPRICED, "message": "稍后重试"}
    )
    assert data["blocked"] is True
    assert data["owed_points"] == 0
    assert data["message"] == "稍后重试"


@pytest.fixture()
def http(env, monkeypatch):
    async def fake_resolve(project, user, required_role="viewer", media_read=False):  # noqa: ARG001
        return SimpleNamespace(
            state_dir=env.state_dir,
            ctx=SimpleNamespace(project_id="01M5TESTPROJECT0000000000"),
        )

    monkeypatch.setattr(points_route, "resolve_project_scope", fake_resolve)
    app = FastAPI()
    app.include_router(points_route.router, prefix="/api/v1")
    return SimpleNamespace(app=app, env=env)


def test_settle_endpoint_retries_and_unlocks(http):
    env = http.env
    _make_usage_db(env.state_dir, [("t1", 1, 0)])
    _seed_prepay(env.state_dir, 1, amount=100)
    env.maps["t1"] = 7_300_000
    FakePointsClient.errors.append(points_client.PointsNotEnough("伴点不足，请先充值"))

    http.app.dependency_overrides[points_route.get_api_user] = lambda: _client_user()
    client = TestClient(http.app)
    first = client.post("/api/v1/projects/demo/episodes/1/points/settle")
    assert first.status_code == 200
    assert first.json()["data"]["blocked"] is True
    assert first.json()["data"]["owed_points"] > 0

    second = client.post("/api/v1/projects/demo/episodes/1/points/settle")
    assert second.status_code == 200
    assert second.json()["data"]["status"] == points_settlement.STATUS_SETTLED
    assert second.json()["data"]["blocked"] is False


def test_release_endpoint_is_operator_only(http):
    env = http.env
    _seed_prepay(env.state_dir, 1, amount=100)
    project_config.record_points_settlement_in_state_dir(
        env.state_dir, 1, status=points_settlement.STATUS_OWED, delta=900, charged=0
    )
    client = TestClient(http.app)

    http.app.dependency_overrides[points_route.get_api_user] = lambda: _client_user()
    denied = client.post("/api/v1/projects/demo/episodes/1/points/release")
    assert denied.status_code == 403

    http.app.dependency_overrides[points_route.get_api_user] = lambda: _operator()
    allowed = client.post("/api/v1/projects/demo/episodes/1/points/release")
    assert allowed.status_code == 200
    assert allowed.json()["data"]["status"] == points_settlement.STATUS_SETTLED_MANUAL
    entry = points_settlement_in_state_dir(env.state_dir, 1)
    assert entry is not None and entry["by"] == "local"
