"""「制作成本预估」闸门确认时的伴点预扣。

客户点「确认报价，继续制作」时，后端先用**售价**（成本 × 客户倍率，¥）按
100 伴点 = ¥1 调 ``dopoints.php`` 预扣伴点：成功才写闸门答案并继续成片；业务失败
（``code != 0``）返回 402 把接口原文交给客户；服务不可用 502 fail-closed。

这里把 ``PointsClient`` 换成假的，绝不出网。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from novelvideo import points_client, project_config
from novelvideo.api.routes import chat as chat_route
from novelvideo.api.routes import estimate as estimate_route
from novelvideo.chat import director_auto as director_auto_module


def _client_user(**overrides) -> dict:
    base = {
        "user_id": "client:13800138000",
        "username": "13800138000",
        "credential_kind": "studio_client",
        "phone": "13800138000",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def gate_env(monkeypatch):
    """把伴点客户端 / 售价 / 台账全部替换成记录用的假件。"""

    state: dict = {"enabled": True, "price": 230.81, "error": None, "charged": None}
    calls: list[dict] = []
    ledger: list[dict] = []
    confirmed: list[tuple] = []

    class FakePointsClient:
        def __init__(self, config=None, transport=None):  # noqa: ARG002
            self.config = config

        def charge(self, *, phone: str, amount: int, projects_uuid: str) -> dict:
            calls.append(
                {"phone": phone, "amount": amount, "projectsuuid": projects_uuid}
            )
            if state["error"] is not None:
                raise state["error"]
            return {"code": 0, "message": "操作成功", "data": []}

    monkeypatch.setattr(points_client, "PointsClient", FakePointsClient)
    monkeypatch.setattr(
        points_client,
        "load",
        lambda **kwargs: points_client.PointsConfig(
            enabled=state["enabled"],
            base_url="https://example.test/dopoints.php",
            sign_key="test-sign-key",
        ),
    )

    async def fake_client_price(**kwargs):  # noqa: ARG001
        return state["price"]

    monkeypatch.setattr(estimate_route, "client_price_cny", fake_client_price)
    monkeypatch.setattr(project_config, "points_charge", lambda *a, **k: state["charged"])
    monkeypatch.setattr(
        project_config,
        "record_points_charge",
        lambda *args, **kwargs: ledger.append({"args": args, **kwargs}),
    )
    monkeypatch.setattr(
        project_config, "confirm_cost_estimate", lambda *a, **k: confirmed.append(a)
    )
    return SimpleNamespace(
        state=state, calls=calls, ledger=ledger, confirmed=confirmed
    )


async def _charge(user: dict, *, episode: int = 1):
    return await chat_route._charge_points_for_episode(
        request=None,
        user=user,
        project_id="01M5TESTPROJECT0000000000",
        username=str(user.get("username") or ""),
        project_name="novel-x",
        episode=episode,
    )


# ---------------------------------------------------------------------------
# _charge_points_for_episode
# ---------------------------------------------------------------------------


async def test_client_is_charged_price_in_points_and_ledger_written(gate_env):
    await _charge(_client_user())
    assert gate_env.calls == [
        {
            "phone": "13800138000",
            "amount": 23081,
            "projectsuuid": "01M5TESTPROJECT0000000000",
        }
    ]
    assert gate_env.ledger[0]["amount"] == 23081
    assert gate_env.ledger[0]["cny"] == 230.81
    assert gate_env.ledger[0]["phone"] == "13800138000"


async def test_business_failure_blocks_with_interface_message(gate_env):
    gate_env.state["error"] = points_client.PointsNotEnough("伴点不足，请先充值")
    with pytest.raises(HTTPException) as excinfo:
        await _charge(_client_user())
    assert excinfo.value.status_code == 402
    assert excinfo.value.detail == "伴点不足，请先充值"
    assert gate_env.ledger == []


async def test_unavailable_service_is_fail_closed(gate_env):
    gate_env.state["error"] = points_client.PointsUnavailable("HTTP 503")
    with pytest.raises(HTTPException) as excinfo:
        await _charge(_client_user())
    assert excinfo.value.status_code == 502
    assert "伴点服务暂时不可用" in str(excinfo.value.detail)
    assert gate_env.ledger == []


async def test_second_confirm_does_not_charge_again(gate_env):
    gate_env.state["charged"] = {"amount": 23081}
    await _charge(_client_user())
    assert gate_env.calls == []
    assert gate_env.ledger == []


async def test_internal_operator_is_not_charged(gate_env):
    await _charge(_client_user(credential_kind="internal_console", phone=""))
    assert gate_env.calls == []


async def test_client_without_phone_is_not_charged(gate_env):
    await _charge(_client_user(phone=""))
    assert gate_env.calls == []


async def test_disabled_switch_skips_charge(gate_env):
    gate_env.state["enabled"] = False
    await _charge(_client_user())
    assert gate_env.calls == []


async def test_missing_markup_is_refused(gate_env):
    gate_env.state["price"] = None
    with pytest.raises(HTTPException) as excinfo:
        await _charge(_client_user())
    assert excinfo.value.status_code == 400
    assert gate_env.calls == []
    assert gate_env.ledger == []


async def test_zero_amount_is_skipped(gate_env):
    gate_env.state["price"] = 0.001
    await _charge(_client_user())
    assert gate_env.calls == []


# ---------------------------------------------------------------------------
# 端点接线：确认闸门 → 先扣费 → 写闸门答案 → 恢复
# ---------------------------------------------------------------------------


def _internal_user(**overrides) -> dict:
    base = {
        "user_id": "local",
        "username": "local",
        "role": "owner",
        "credential_kind": "internal_console",
    }
    base.update(overrides)
    return base


def _gate_run(*, username: str = "13800138000") -> SimpleNamespace:
    return SimpleNamespace(
        username=username,
        context=SimpleNamespace(project_name="novel-x"),
        episode=1,
        status="paused",
        run_id="run-1",
        activated_at="2026-11-03T00:00:00+00:00",
        updated_at="2026-11-03T00:00:00+00:00",
        last_error="",
        voice_policy=None,
        scope="project",
        target_episodes=0,
        driver="deterministic",
    )


def _gate_app(
    monkeypatch,
    *,
    user: dict,
    run: SimpleNamespace | None = None,
    owner_type: str = "client",
):
    """构造到 confirm 端点的最小 app（不打真实 run / 归属者解析）。

    ``run=None`` 由调用方显式传 ``run=None`` 表示项目没有 run。
    """

    async def fake_resolve(*, user, project_id, required_role="viewer", media_read=False):
        return SimpleNamespace(
            project_id="01M5TESTPROJECT0000000000",
            project_name="novel-x",
            owner_username="13800138000",
            owner_type=owner_type,
        )

    class FakeCoordinator:
        async def get_by_project(self, project_id):
            return run

        async def resume_paused(self, **kwargs):
            # 恢复必须落在 run 归属者身上，而不是请求者身份。
            assert kwargs.get("username") == run.username
            # 真协调器的收费闸门拦截：未确认时任何「继续」都必须被拒（见 director_auto）。
            if director_auto_module.DirectorAutoCoordinator._cost_gate_blocks_resume(run):
                raise ValueError(director_auto_module.COST_ESTIMATE_RESUME_BLOCKED_ERROR)
            return SimpleNamespace(**{**vars(run), "status": "running"})

    monkeypatch.setattr(chat_route, "resolve_project_context", fake_resolve)
    monkeypatch.setattr(chat_route, "director_auto_coordinator", FakeCoordinator())

    app = FastAPI()
    app.include_router(chat_route.router, prefix="/api/v1")
    app.dependency_overrides[chat_route.get_api_user] = lambda: user
    return TestClient(app)


@pytest.fixture()
def gate_http(monkeypatch, gate_env):
    return _gate_app(monkeypatch, user=_client_user(), run=_gate_run())


def test_confirm_endpoint_charges_then_confirms(gate_http, gate_env):
    response = gate_http.post(
        "/api/v1/projects/demo/chat/director-auto/gates/cost_estimate/confirm",
        json={"episode": 1},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert gate_env.calls == [
        {
            "phone": "13800138000",
            "amount": 23081,
            "projectsuuid": "01M5TESTPROJECT0000000000",
        }
    ]
    assert gate_env.confirmed == [("13800138000", "novel-x", 1)]


def test_internal_operator_cannot_confirm_client_cost(monkeypatch, gate_env):
    """收费闸门只能客户本人确认：内部端代点必须 403，不能写答案也不能扣费。"""

    client = _gate_app(monkeypatch, user=_internal_user(), run=_gate_run())
    response = client.post(
        "/api/v1/projects/demo/chat/director-auto/gates/cost_estimate/confirm",
        json={"episode": 1},
    )
    assert response.status_code == 403
    assert "客户本人" in response.json()["detail"]
    assert gate_env.calls == []
    assert gate_env.confirmed == []


def test_internal_operator_can_confirm_internal_project(monkeypatch, gate_env):
    """内部项目没有收费对象，内部端自己确认即可（不能因此死锁）。"""

    client = _gate_app(
        monkeypatch,
        user=_internal_user(),
        run=_gate_run(username="local"),
        owner_type="internal",
    )
    response = client.post(
        "/api/v1/projects/demo/chat/director-auto/gates/cost_estimate/confirm",
        json={"episode": 1},
    )
    assert response.status_code == 200
    assert gate_env.calls == []  # 内部项目不扣伴点
    assert gate_env.confirmed == [("local", "novel-x", 1)]


def test_confirm_without_run_is_not_a_silent_200(monkeypatch, gate_env):
    client = _gate_app(monkeypatch, user=_internal_user(), run=None)
    response = client.post(
        "/api/v1/projects/demo/chat/director-auto/gates/cost_estimate/confirm",
        json={"episode": 1},
    )
    assert response.status_code == 404
    assert gate_env.calls == []
    assert gate_env.confirmed == []


def test_confirm_endpoint_leaves_gate_paused_when_points_fail(gate_http, gate_env):
    gate_env.state["error"] = points_client.PointsNotEnough("伴点不足，请先充值")
    response = gate_http.post(
        "/api/v1/projects/demo/chat/director-auto/gates/cost_estimate/confirm",
        json={"episode": 1},
    )
    assert response.status_code == 402
    assert response.json()["detail"] == "伴点不足，请先充值"
    # 闸门没答：制作保持暂停，客户可以充值后再点一次。
    assert gate_env.confirmed == []


def test_confirm_endpoint_unknown_gate_still_400(gate_http, gate_env):
    response = gate_http.post(
        "/api/v1/projects/demo/chat/director-auto/gates/nope/confirm",
        json={"episode": 1},
    )
    assert response.status_code == 400
    assert gate_env.calls == []


# ---------------------------------------------------------------------------
# 旁路：run 停在收费闸门时，「继续」不能绕过预扣
# ---------------------------------------------------------------------------


def _cost_gate_run(*, username: str = "13800138000") -> SimpleNamespace:
    return SimpleNamespace(
        **{
            **vars(_gate_run(username=username)),
            "last_error": director_auto_module.COST_ESTIMATE_REQUIRED_ERROR,
        }
    )


def test_continue_is_refused_while_the_client_has_not_confirmed(
    monkeypatch, gate_env
) -> None:
    """线上事故的回归：报价卡没点，直接打 /continue 必须 409，不能白跑一集。"""

    monkeypatch.setattr(project_config, "cost_estimate_gate_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(project_config, "cost_estimate_confirmed", lambda *_a, **_k: False)
    client = _gate_app(monkeypatch, user=_client_user(), run=_cost_gate_run())

    response = client.post("/api/v1/projects/demo/chat/director-auto/continue", json={})

    assert response.status_code == 409
    assert "确认报价" in response.json()["detail"]
    assert gate_env.calls == []
    assert gate_env.ledger == []
    assert gate_env.confirmed == []


def test_continue_is_allowed_once_the_client_has_confirmed(monkeypatch, gate_env) -> None:
    monkeypatch.setattr(project_config, "cost_estimate_gate_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(project_config, "cost_estimate_confirmed", lambda *_a, **_k: True)
    client = _gate_app(monkeypatch, user=_client_user(), run=_cost_gate_run())

    response = client.post("/api/v1/projects/demo/chat/director-auto/continue", json={})

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_director_auto_gate_map_has_cost_estimate():
    assert director_auto_module.COST_ESTIMATE_GATE_STEPS, "cost_estimate gate steps missing"
