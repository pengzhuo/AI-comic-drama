"""Cost attribution: schema upgrade, gateway reconcile, report route."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qsl

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _project_dir(tmp_path):
    # Deliberately outside OUTPUT_DIR so usage DBs resolve to
    # ``<project_output_dir>/data.db`` and stay inside the test tmp dir.
    return tmp_path / "output" / "alice" / "demo"


def _write_gateway_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            quota INTEGER,
            submit_time TEXT,
            start_time TEXT,
            finish_time TEXT,
            status TEXT
        );
        CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT,
            upstream_request_id TEXT,
            quota INTEGER,
            model_name TEXT,
            type INTEGER
        );
        """
    )
    conn.execute("INSERT INTO tasks (task_id, quota) VALUES ('task-1', 1000)")
    conn.execute(
        "INSERT INTO logs (request_id, quota, model_name, type)"
        " VALUES ('log-img-1', 200, 'LingShan-G2', 2)"
    )
    conn.execute(
        "INSERT INTO logs (request_id, quota, model_name, type)"
        " VALUES ('log-llm-1', 50, 'DC-hermes-LLM', 2)"
    )
    conn.commit()
    conn.close()


def _timestamps(db_path, request_id, accepted_at, completed_at) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE video_request_usage SET accepted_at = ?, completed_at = ?"
        " WHERE request_id = ?",
        (accepted_at, completed_at, request_id),
    )
    conn.commit()
    conn.close()


def test_legacy_video_usage_db_gains_cost_columns(tmp_path):
    from novelvideo.video_request_usage import record_video_request

    project_dir = _project_dir(tmp_path)
    project_dir.mkdir(parents=True, exist_ok=True)
    db_path = project_dir / "data.db"

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE video_request_usage (
            request_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model_name TEXT,
            episode INTEGER,
            beat_num INTEGER,
            task_type TEXT,
            duration_seconds REAL,
            status TEXT NOT NULL DEFAULT 'accepted',
            cost_estimate REAL,
            accepted_at TEXT NOT NULL,
            completed_at TEXT,
            downloaded_at TEXT,
            updated_at TEXT NOT NULL,
            error_message TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO video_request_usage (request_id, provider, accepted_at, updated_at)"
        " VALUES ('legacy-1', 'newapi', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    record_video_request(
        project_output_dir=project_dir,
        request_id="new-1",
        provider="newapi",
        model_name="seedance-2.0-fast",
        episode=1,
        beat_num=1,
        task_type="single_video",
        duration_seconds=5.0,
    )

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(video_request_usage)")}
    assert {"gateway_request_id", "cost_quota", "quota_checked_at"} <= columns
    legacy = conn.execute(
        "SELECT gateway_request_id, cost_quota FROM video_request_usage"
        " WHERE request_id = 'legacy-1'"
    ).fetchone()
    assert legacy == (None, None)
    fresh = conn.execute(
        "SELECT gateway_request_id FROM video_request_usage WHERE request_id = 'new-1'"
    ).fetchone()
    assert fresh[0] == "new-1"
    conn.close()


def test_reconcile_apply_migrates_legacy_usage_table(tmp_path):
    """A project idle since the upgrade still has only the pre-cost columns.

    ``apply`` must add ``cost_quota``/``quota_checked_at`` itself instead of
    raising ``no such column: cost_quota``.
    """
    from novelvideo.gateway_cost_reconcile import reconcile

    project_dir = _project_dir(tmp_path)
    project_dir.mkdir(parents=True, exist_ok=True)
    db_path = project_dir / "data.db"

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE video_request_usage (
            request_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model_name TEXT,
            episode INTEGER,
            beat_num INTEGER,
            task_type TEXT,
            duration_seconds REAL,
            status TEXT NOT NULL DEFAULT 'accepted',
            cost_estimate REAL,
            accepted_at TEXT NOT NULL,
            completed_at TEXT,
            downloaded_at TEXT,
            updated_at TEXT NOT NULL,
            error_message TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO video_request_usage (request_id, provider, accepted_at, updated_at)"
        " VALUES ('task-1', 'newapi', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    gateway_db = tmp_path / "one-api.db"
    _write_gateway_db(gateway_db)

    report = reconcile(gateway_db=gateway_db, project_db=db_path, apply=True)
    assert not report.errors
    assert report.quota == 1000

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(video_request_usage)")}
    assert {"cost_quota", "quota_checked_at"} <= columns
    quota, checked_at = conn.execute(
        "SELECT cost_quota, quota_checked_at FROM video_request_usage"
        " WHERE request_id = 'task-1'"
    ).fetchone()
    conn.close()
    assert quota == 1000
    assert checked_at


def _seed_project(tmp_path):
    from novelvideo.audio_request_usage import record_audio_generation_attempt
    from novelvideo.image_request_usage import record_image_request
    from novelvideo.llm_request_usage import record_llm_request
    from novelvideo.video_request_usage import record_video_request

    project_dir = _project_dir(tmp_path)
    record_video_request(
        project_output_dir=project_dir,
        request_id="task-1",
        provider="newapi",
        model_name="seedance-2.0-fast",
        episode=1,
        beat_num=1,
        task_type="single_video",
        duration_seconds=5.0,
    )
    record_image_request(
        project_output_dir=project_dir,
        request_id="img-1",
        provider="newapi",
        model_name="LingShan-G2",
        task_type="sketch",
        scope="ep001:beat001",
        episode=1,
        gateway_request_id="log-img-1",
    )
    record_llm_request(
        project_output_dir=project_dir,
        request_id="llm-1",
        model="DC-hermes-LLM",
        provider_request_id="log-llm-1",
        episode=1,
    )
    record_audio_generation_attempt(
        project_output_dir=project_dir,
        request_id="aud-1",
        provider="fal.ai",
        model_name="IndexTTS2",
        task_type="audio_generation_indextts2",
        scope="ep001:beat001",
        episode=1,
    )
    _timestamps(
        project_dir / "data.db",
        "task-1",
        "2026-01-01T00:00:00",
        "2026-01-01T01:00:00",
    )
    return project_dir


def test_reconcile_dry_run_then_apply_and_report(tmp_path):
    from novelvideo.gateway_cost_reconcile import reconcile

    project_dir = _seed_project(tmp_path)
    db_path = project_dir / "data.db"
    gateway_db = tmp_path / "one-api.db"
    _write_gateway_db(gateway_db)

    dry = reconcile(gateway_db=gateway_db, project_db=db_path, apply=False)
    assert not dry.errors
    assert dry.quota == 1250
    conn = sqlite3.connect(db_path)
    assert (
        conn.execute(
            "SELECT cost_quota FROM video_request_usage WHERE request_id = 'task-1'"
        ).fetchone()[0]
        is None
    )
    conn.close()

    applied = reconcile(gateway_db=gateway_db, project_db=db_path, apply=True)
    assert not applied.errors
    conn = sqlite3.connect(db_path)
    quotas = dict(
        conn.execute("SELECT request_id, cost_quota FROM video_request_usage")
    )
    assert quotas["task-1"] == 1000
    assert conn.execute(
        "SELECT cost_quota FROM image_request_usage WHERE request_id = 'img-1'"
    ).fetchone()[0] == 200
    assert conn.execute(
        "SELECT cost_quota FROM llm_request_usage WHERE request_id = 'llm-1'"
    ).fetchone()[0] == 50
    assert (
        conn.execute(
            "SELECT cost_quota FROM audio_request_usage WHERE request_id = 'aud-1'"
        ).fetchone()[0]
        is None
    )
    conn.close()

    from novelvideo.api.routes.cost_report import build_cost_report

    report = build_cost_report(project_output_dir=project_dir)
    assert report["totals"]["requests"] == 4
    assert report["totals"]["quota"] == 1250
    assert report["totals"]["usd"] == 1250 / report["quota_per_usd"]
    assert report["unattributed"]["requests"] == 1
    assert report["unattributed"]["by_kind"]["audio"] == 1
    assert report["currency"] == "CNY"
    assert report["usd_cny_rate"] > 0
    assert report["totals"]["cny"] == pytest.approx(
        report["totals"]["usd"] * report["usd_cny_rate"]
    )
    episode_one = next(item for item in report["by_episode"] if item["episode"] == 1)
    assert episode_one["quota"] == 1250
    assert episode_one["duration_seconds"] == 3600
    assert episode_one["duration_hours"] == 1.0
    model_quota = {item["model"]: item["quota"] for item in report["by_model"]}
    assert model_quota["seedance-2.0-fast"] == 1000


def test_local_usage_meter_records_llm_usage(tmp_path, monkeypatch):
    import asyncio

    import novelvideo.ports as ports
    from novelvideo.ports.local.usage import LocalUsageMeter

    project_dir = _project_dir(tmp_path)
    record = SimpleNamespace(output_dir=str(project_dir))

    class _FakeRegistry:
        async def get_project(self, project_id):
            return record if project_id == "proj_demo" else None

    monkeypatch.setattr(ports, "get_project_registry", lambda: _FakeRegistry())

    meter = LocalUsageMeter()

    async def run():
        meter.set_llm_usage_context("alice", project_id="proj_demo", resource_kind="script")
        await meter.record_llm_tokens(
            user_id="alice",
            input_tokens=10,
            output_tokens=20,
            model="DC-hermes-LLM",
            project_id="proj_demo",
        )
        await meter.bump_model_call(
            user_id="alice",
            model="DC-hermes-LLM",
            project_id="proj_demo",
            resource_kind="script",
            provider_request_id="log-llm-1",
            metadata={"response_id": "resp-1"},
        )

    asyncio.run(run())

    conn = sqlite3.connect(project_dir / "data.db")
    row = conn.execute(
        "SELECT provider_request_id, input_tokens, output_tokens, provider_response_id"
        " FROM llm_request_usage"
    ).fetchone()
    conn.close()
    assert row == ("log-llm-1", 10, 20, "resp-1")


def test_provider_request_id_prefers_gateway_header():
    from novelvideo.llm_instrumentation import _extract_provider_ids

    class _Response:
        id = "upstream-uuid"
        _response_headers = {
            "x-request-id": "upstream-uuid",
            "x-oneapi-request-id": "202610260125260772383588268d9d6QCPEK2co",
        }

    request_id, _task_id, _response_id = _extract_provider_ids(_Response())
    assert request_id == "202610260125260772383588268d9d6QCPEK2co"


def test_newapi_image_request_id_prefers_gateway_header():
    from novelvideo.generators.nanobanana_grid import _newapi_request_id_from_headers

    headers = {
        "x-request-id": "ae6c682e-08c6-4a05-8553-3a48c20df2d6",
        "x-oneapi-request-id": "202610260125260772383588268d9d6QCPEK2co",
    }
    assert (
        _newapi_request_id_from_headers(headers)
        == "202610260125260772383588268d9d6QCPEK2co"
    )


def test_cost_reconcile_cli_is_registered():
    from novelvideo.cli import app

    names = {command.name for command in app.registered_commands}
    assert "cost-reconcile" in names


def test_cost_report_route_contract(tmp_path, monkeypatch):
    from novelvideo.api.routes import cost_report

    project_dir = _seed_project(tmp_path)

    async def fake_resolve(project, user, *, required_role="viewer", media_read=False):
        return SimpleNamespace(
            ctx=SimpleNamespace(project_id="proj_demo"),
            username="alice",
            project_name="demo",
            project_dir=project_dir,
            output_dir=str(project_dir),
            state_dir=str(tmp_path / "state"),
            runtime_dir=str(tmp_path / "runtime"),
        )

    monkeypatch.setattr(cost_report, "resolve_project_scope", fake_resolve)

    app = FastAPI()
    app.include_router(cost_report.router, prefix="/api/v1")
    app.dependency_overrides[cost_report.get_api_user] = lambda: {"username": "alice"}
    client = TestClient(app)

    response = client.get("/api/v1/projects/demo/cost-report")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["totals"]["requests"] == 4
    assert body["data"]["project"]["name"] == "demo"

    filtered = client.get("/api/v1/projects/demo/cost-report", params={"episode": 1})
    assert filtered.status_code == 200
    assert filtered.json()["data"]["totals"]["requests"] == 4


def _write_gateway_db_with_upstream_ids(path) -> None:
    """Gateway DB that records the upstream ids (``private_data`` / ``upstream_request_id``).

    The gateway quota is deliberately much higher than the relay's real charge:
    the whole point of ``--source relay`` is to ignore it.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            quota INTEGER,
            private_data BLOB
        );
        CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT,
            upstream_request_id TEXT,
            quota INTEGER,
            model_name TEXT,
            type INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks (task_id, quota, private_data) VALUES (?, ?, ?)",
        ("task-1", 46_875_000, json.dumps({"upstream_task_id": "up-task-1"}).encode()),
    )
    conn.execute(
        "INSERT INTO logs (request_id, upstream_request_id, quota, model_name, type)"
        " VALUES ('log-img-1', 'up-log-img', 80475, 'LingShan-G2', 2)"
    )
    conn.execute(
        "INSERT INTO logs (request_id, upstream_request_id, quota, model_name, type)"
        " VALUES ('log-llm-1', 'up-log-llm', 330000, 'DC-hermes-LLM', 2)"
    )
    conn.commit()
    conn.close()


class _FakeRelay:
    """Minimal upstream NewAPI relay: login / status / task pages / log by request_id."""

    def __init__(
        self,
        *,
        base_url: str,
        tasks: dict[str, int],
        logs: dict[str, int],
        reject_first_authed_call: bool = False,
    ) -> None:
        self.base_url = base_url
        self.tasks = dict(tasks)
        self.logs = dict(logs)
        self.calls: list[str] = []
        self._reject_next = reject_first_authed_call

    def __call__(self, method, url, headers, payload):
        self.calls.append(f"{method} {url}")
        path, _, query = url.partition("?")
        path = path.removeprefix(self.base_url)
        params = dict(parse_qsl(query))
        if path == "/api/user/login":
            return 200, json.dumps({"success": True, "data": {"access_token": "tok-1"}})
        if path == "/api/status":
            return 200, json.dumps(
                {
                    "success": True,
                    "data": {
                        "quota_per_unit": 500000,
                        "usd_exchange_rate": 7.3,
                        "custom_currency_symbol": "¥",
                        "display_in_currency": True,
                    },
                }
            )
        if not headers.get("Authorization"):
            return 401, json.dumps({"success": False, "code": "AUTH_TOKEN_EXPIRED"})
        if self._reject_next:
            # access token 只有 15 分钟，过期一次应自动重登再重试。
            self._reject_next = False
            return 401, json.dumps({"success": False, "code": "AUTH_TOKEN_EXPIRED"})
        if path == "/api/task/self":
            page = int(params.get("p") or 0)
            size = int(params.get("page_size") or 100)
            items = [
                {"task_id": task_id, "quota": quota}
                for task_id, quota in sorted(self.tasks.items())
            ]
            return 200, json.dumps(
                {
                    "success": True,
                    "data": {"items": items[page * size : page * size + size]},
                }
            )
        if path == "/api/log/self":
            request_id = params.get("request_id") or ""
            items = (
                [{"request_id": request_id, "quota": self.logs[request_id]}]
                if request_id in self.logs
                else []
            )
            return 200, json.dumps({"success": True, "data": {"items": items}})
        return 404, json.dumps({"success": False, "message": "not found"})

    def log_calls(self) -> list[str]:
        return [call for call in self.calls if "/api/log/self" in call]


def _relay_endpoints(base_url="https://aicf.example"):
    from novelvideo.relay_billing import build_endpoints

    return build_endpoints([base_url], username="hiaihub", password="secret")


def test_reconcile_relay_source_uses_upstream_quota(tmp_path):
    """``--source relay`` bridges local ids → upstream ids and bills the real charge."""
    from novelvideo.gateway_cost_reconcile import reconcile

    project_dir = _seed_project(tmp_path)
    gateway_db = tmp_path / "one-api.db"
    _write_gateway_db_with_upstream_ids(gateway_db)

    relay = _FakeRelay(
        base_url="https://aicf.example",
        tasks={"up-task-1": 220_784},
        logs={"up-log-img": 2_322, "up-log-llm": 10_213},
        reject_first_authed_call=True,
    )
    cache_path = tmp_path / "relay_cache.json"

    report = reconcile(
        gateway_db=gateway_db,
        project_db=project_dir / "data.db",
        apply=True,
        source="relay",
        relay_endpoints=_relay_endpoints(),
        relay_cache=cache_path,
        relay_transport=relay,
    )

    assert not report.errors
    assert report.source == "relay"
    assert [status.quota_per_unit for status in report.relay_status] == [500000]
    assert report.quota == 220_784 + 2_322 + 10_213

    conn = sqlite3.connect(project_dir / "data.db")
    quotas = {
        "video": conn.execute(
            "SELECT cost_quota FROM video_request_usage WHERE request_id = 'task-1'"
        ).fetchone()[0],
        "image": conn.execute(
            "SELECT cost_quota FROM image_request_usage WHERE request_id = 'img-1'"
        ).fetchone()[0],
        "llm": conn.execute(
            "SELECT cost_quota FROM llm_request_usage WHERE request_id = 'llm-1'"
        ).fetchone()[0],
    }
    conn.close()
    # The gateway's own (inflated) numbers must not leak through.
    assert quotas == {"video": 220_784, "image": 2_322, "llm": 10_213}

    from novelvideo.api.routes.cost_report import build_cost_report

    built = build_cost_report(project_output_dir=project_dir)
    assert built["totals"]["quota"] == report.quota
    assert built["totals"]["cny"] == pytest.approx(built["totals"]["usd"] * 7.3)

    # 缓存落盘后第二次运行不再查上游日志。
    assert cache_path.is_file()
    before = len(relay.log_calls())
    again = reconcile(
        gateway_db=gateway_db,
        project_db=project_dir / "data.db",
        source="relay",
        relay_endpoints=_relay_endpoints(),
        relay_cache=cache_path,
        relay_transport=relay,
    )
    assert again.quota == report.quota
    assert len(relay.log_calls()) == before


def test_reconcile_relay_source_leaves_unknown_ids_unattributed(tmp_path):
    from novelvideo.gateway_cost_reconcile import reconcile

    project_dir = _seed_project(tmp_path)
    gateway_db = tmp_path / "one-api.db"
    _write_gateway_db_with_upstream_ids(gateway_db)

    relay = _FakeRelay(
        base_url="https://aicf.example",
        tasks={"up-task-1": 220_784},
        logs={"up-log-img": 2_322},  # 上游查不到 up-log-llm
    )

    report = reconcile(
        gateway_db=gateway_db,
        project_db=project_dir / "data.db",
        apply=True,
        source="relay",
        relay_endpoints=_relay_endpoints(),
        relay_transport=relay,
    )

    assert not report.errors
    assert report.quota == 220_784 + 2_322

    conn = sqlite3.connect(project_dir / "data.db")
    assert (
        conn.execute(
            "SELECT cost_quota FROM llm_request_usage WHERE request_id = 'llm-1'"
        ).fetchone()[0]
        is None
    )
    conn.close()

    from novelvideo.api.routes.cost_report import build_cost_report

    built = build_cost_report(project_output_dir=project_dir)
    assert built["unattributed"]["by_kind"]["llm"] == 1


def test_reconcile_rejects_unknown_source(tmp_path):
    from novelvideo.gateway_cost_reconcile import reconcile

    with pytest.raises(ValueError):
        reconcile(gateway_db=tmp_path / "one-api.db", source="nope")


def test_relay_billing_client_relogins_on_expired_token():
    from novelvideo.relay_billing import RelayBillingClient

    relay = _FakeRelay(
        base_url="https://aicf.example",
        tasks={"up-task-1": 7},
        logs={},
        reject_first_authed_call=True,
    )
    client = RelayBillingClient(
        _relay_endpoints()[0], transport=relay, page_size=100
    )

    assert client.task_quotas() == {"up-task-1": 7}
    logins = [call for call in relay.calls if "/api/user/login" in call]
    assert len(logins) == 2  # 首次登录 + 401 后重登


def _cost_report_client(tmp_path, monkeypatch, project_dir):
    """带项目解析桩的 TestClient，state_dir 指向项目目录（即 data.db 所在处）。"""
    from novelvideo.api.routes import cost_report

    async def fake_resolve(project, user, *, required_role="viewer", media_read=False):
        return SimpleNamespace(
            ctx=SimpleNamespace(project_id="proj_demo"),
            username="alice",
            project_name="demo",
            project_dir=project_dir,
            output_dir=str(project_dir),
            state_dir=str(project_dir),
            runtime_dir=str(tmp_path / "runtime"),
        )

    monkeypatch.setattr(cost_report, "resolve_project_scope", fake_resolve)
    cost_report._reconcile_last_run.clear()

    app = FastAPI()
    app.include_router(cost_report.router, prefix="/api/v1")
    app.dependency_overrides[cost_report.get_api_user] = lambda: {"username": "alice"}
    return TestClient(app), cost_report


def test_cost_report_reconcile_route_backfills_gateway_quota(tmp_path, monkeypatch):
    """POST .../cost-report/reconcile 增量回填 cost_quota 并返回刷新后的明细。"""
    from novelvideo import gateway_cost_reconcile

    project_dir = _seed_project(tmp_path)
    gateway_db = tmp_path / "one-api.db"
    _write_gateway_db(gateway_db)
    monkeypatch.setattr(
        gateway_cost_reconcile, "NEWAPI_BILLING_DB_PATH", str(gateway_db)
    )

    client, _ = _cost_report_client(tmp_path, monkeypatch, project_dir)

    before = client.get(
        "/api/v1/projects/demo/cost-report", params={"episode": 1}
    ).json()["data"]
    assert before["totals"]["quota"] == 0
    assert before["unattributed"]["requests"] == 4

    response = client.post(
        "/api/v1/projects/demo/cost-report/reconcile", params={"episode": 1}
    )
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["reconcile"]["applied"] is True
    assert data["reconcile"]["skipped"] is False
    assert data["reconcile"]["source"] == "gateway"
    assert data["reconcile"]["matched"] == 3
    assert data["reconcile"]["errors"] == []

    # 视频 1000 + 图片 200 + 文本 50；音频无网关单号仍归入未归因。
    assert data["totals"]["quota"] == 1250
    assert data["totals"]["cny"] == pytest.approx(
        data["totals"]["usd"] * data["usd_cny_rate"]
    )
    assert data["unattributed"]["requests"] == 1
    assert data["unattributed"]["by_kind"]["audio"] == 1
    models = {item["model"]: item["quota"] for item in data["by_model"]}
    assert models["seedance-2.0-fast"] == 1000
    assert models["LingShan-G2"] == 200
    assert models["DC-hermes-LLM"] == 50

    # 第二次调用命中去抖窗口：不再重复对账，但报表照样返回。
    second = client.post(
        "/api/v1/projects/demo/cost-report/reconcile", params={"episode": 1}
    ).json()["data"]
    assert second["reconcile"]["skipped"] is True
    assert second["totals"]["quota"] == 1250

    forced = client.post(
        "/api/v1/projects/demo/cost-report/reconcile",
        params={"episode": 1, "force": "true"},
    ).json()["data"]
    assert forced["reconcile"]["skipped"] is False


def test_director_auto_episode_cost_hook_applies_quota(tmp_path, monkeypatch):
    """一集成片落盘后 coordinator 自动结算，无需用户操作。"""
    from novelvideo import gateway_cost_reconcile
    from novelvideo.chat import director_auto
    from novelvideo.chat.director_auto import (
        DirectorAutoCoordinator,
        DirectorAutoRun,
        DirectorAutoStore,
    )
    from novelvideo.project_context import ProjectContext

    project_dir = _seed_project(tmp_path)
    gateway_db = tmp_path / "one-api.db"
    _write_gateway_db(gateway_db)
    monkeypatch.setattr(
        gateway_cost_reconcile, "NEWAPI_BILLING_DB_PATH", str(gateway_db)
    )

    ctx = ProjectContext(
        project_id="proj_demo",
        project_name="demo",
        owner_type="user",
        owner_id="user-1",
        owner_username="alice",
        requester_user_id="user-1",
        requester_username="alice",
        requester_principals=(("user", "user-1"),),
        effective_role="owner",
        home_node_id="local",
        output_dir=project_dir,
        state_dir=project_dir,
        runtime_dir=tmp_path / "runtime",
        is_home_node=True,
    )
    run = DirectorAutoRun(
        run_id="run-1",
        username="alice",
        project_id="proj_demo",
        episode=1,
        status="running",
        activated_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        context_json=director_auto._serialize_context(ctx),
        baseline_task_ids=(),
        handled_task_ids=(),
        scope="project",
        driver="deterministic",
    )
    coordinator = DirectorAutoCoordinator(
        store=DirectorAutoStore(tmp_path / "director-auto.db")
    )

    asyncio.run(coordinator._reconcile_episode_cost(run))

    conn = sqlite3.connect(project_dir / "data.db")
    quota = conn.execute(
        "SELECT cost_quota FROM video_request_usage WHERE request_id = 'task-1'"
    ).fetchone()[0]
    conn.close()
    assert quota == 1000


def test_director_auto_episode_cost_hook_never_raises(tmp_path, monkeypatch):
    """网关账单库不可用时只记日志，绝不把成片流程拖垮。"""
    from novelvideo import gateway_cost_reconcile
    from novelvideo.chat import director_auto
    from novelvideo.chat.director_auto import (
        DirectorAutoCoordinator,
        DirectorAutoRun,
        DirectorAutoStore,
    )
    from novelvideo.project_context import ProjectContext

    project_dir = _seed_project(tmp_path)
    monkeypatch.setattr(
        gateway_cost_reconcile,
        "NEWAPI_BILLING_DB_PATH",
        str(tmp_path / "missing-one-api.db"),
    )

    ctx = ProjectContext(
        project_id="proj_demo",
        project_name="demo",
        owner_type="user",
        owner_id="user-1",
        owner_username="alice",
        requester_user_id="user-1",
        requester_username="alice",
        requester_principals=(("user", "user-1"),),
        effective_role="owner",
        home_node_id="local",
        output_dir=project_dir,
        state_dir=project_dir,
        runtime_dir=tmp_path / "runtime",
        is_home_node=True,
    )
    run = DirectorAutoRun(
        run_id="run-1",
        username="alice",
        project_id="proj_demo",
        episode=1,
        status="running",
        activated_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        context_json=director_auto._serialize_context(ctx),
        baseline_task_ids=(),
        handled_task_ids=(),
    )
    coordinator = DirectorAutoCoordinator(
        store=DirectorAutoStore(tmp_path / "director-auto.db")
    )

    # ``reconcile`` 把「账单库读取失败」记进 report.errors 而不是抛出，
    # 所以这里既不能抛异常，也不该写坏项目库。
    asyncio.run(coordinator._reconcile_episode_cost(run))

    conn = sqlite3.connect(project_dir / "data.db")
    quota = conn.execute(
        "SELECT cost_quota FROM video_request_usage WHERE request_id = 'task-1'"
    ).fetchone()[0]
    conn.close()
    assert quota is None

