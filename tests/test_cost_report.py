"""Cost attribution: schema upgrade, gateway reconcile, report route."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

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
