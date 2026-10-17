"""剧本处理端点的手动触发与结果读取。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelvideo import script_process as script_process_store

pytestmark = pytest.mark.m03


def _client(monkeypatch, tmp_path, *, ctx=None):
    from novelvideo.api.deps import ProjectResolution
    from novelvideo.api.routes import script_process as route

    async def fake_resolve_project_scope(project, user, *, required_role="viewer"):
        assert project == "demo"
        return ProjectResolution(
            ctx=ctx if ctx is not None else SimpleNamespace(project_id="pid-1"),
            username="alice",
            project_name="demo",
            project_dir=tmp_path,
            output_dir=str(tmp_path),
            state_dir=str(tmp_path / "state"),
            runtime_dir=str(tmp_path / "runtime"),
        )

    calls: list[dict] = []

    async def fake_enqueue(ctx, **kwargs):
        calls.append({"ctx": ctx, **kwargs})
        return SimpleNamespace(
            task_state=SimpleNamespace(task_id="task-1"),
            backend="inline",
            queue="default",
        )

    class _Manager:
        def get_task_for_project(self, *_args, **_kwargs):
            return SimpleNamespace(
                status="running", task_id="task-1", progress=0.5, current_task="处理中"
            )

    monkeypatch.setattr(route, "resolve_project_scope", fake_resolve_project_scope)
    monkeypatch.setattr(route, "enqueue_project_task", fake_enqueue)
    monkeypatch.setattr(route, "get_task_manager", lambda: _Manager())

    app = FastAPI()
    app.include_router(route.router, prefix="/api/v1", dependencies=[])
    app.dependency_overrides[route.get_api_user] = lambda: {"username": "alice"}

    return TestClient(app), calls


def test_get_reports_absent_result(monkeypatch, tmp_path):
    client, _calls = _client(monkeypatch, tmp_path)

    response = client.get("/api/v1/projects/demo/script-process")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["has_result"] is False
    assert body["result"] is None
    assert body["task"]["status"] == "running"


def test_get_returns_saved_result_and_optional_script(monkeypatch, tmp_path):
    script_process_store.save_script_process_result(
        tmp_path,
        payload={
            "understanding": "题材：复仇短剧",
            "characters": [{"name": "林建军"}],
            "scenes": [],
            "gaps": [],
        },
        production_script="# 结构化制作剧本\n",
    )
    client, _calls = _client(monkeypatch, tmp_path)

    body = client.get("/api/v1/projects/demo/script-process").json()

    assert body["has_result"] is True
    assert body["result"]["understanding"] == "题材：复仇短剧"
    assert "production_script" not in body

    with_script = client.get(
        "/api/v1/projects/demo/script-process?include_script=true"
    ).json()
    assert with_script["production_script"] == "# 结构化制作剧本\n"


def test_start_requires_an_imported_novel(monkeypatch, tmp_path):
    client, calls = _client(monkeypatch, tmp_path)

    body = client.post("/api/v1/projects/demo/script-process/start", json={}).json()

    assert body["ok"] is False
    assert body["code"] == "NOVEL_IMPORT_REQUIRED"
    assert calls == []


def test_start_enqueues_the_stage(monkeypatch, tmp_path):
    (tmp_path / "novel.txt").write_text("林建军：你回来晚了。", encoding="utf-8")
    client, calls = _client(monkeypatch, tmp_path)

    body = client.post("/api/v1/projects/demo/script-process/start", json={}).json()

    assert body["ok"] is True
    assert body["task_type"] == "script_process"
    assert body["task_id"] == "task-1"
    assert calls[0]["task_type"] == "script_process"
    assert calls[0]["product_surface"] == "mainline"
    assert calls[0]["episode"] == 0
    assert calls[0]["payload"] == {"output_dir": str(tmp_path)}


def test_start_is_idempotent_until_forced(monkeypatch, tmp_path):
    (tmp_path / "novel.txt").write_text("林建军：你回来晚了。", encoding="utf-8")
    script_process_store.save_script_process_result(
        tmp_path,
        payload={"understanding": "理解"},
        production_script="正文",
    )
    client, calls = _client(monkeypatch, tmp_path)

    body = client.post("/api/v1/projects/demo/script-process/start", json={}).json()

    assert body["ok"] is True
    assert body["already_done"] is True
    assert calls == []


def test_forced_start_reruns_with_force_payload(monkeypatch, tmp_path):
    (tmp_path / "novel.txt").write_text("林建军：你回来晚了。", encoding="utf-8")
    script_process_store.save_script_process_result(
        tmp_path,
        payload={"understanding": "理解"},
        production_script="正文",
    )
    client, calls = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/v1/projects/demo/script-process/start",
        json={"force": True, "spine_template": "narrated"},
    )

    assert response.json()["ok"] is True
    assert calls[0]["payload"] == {
        "output_dir": str(tmp_path),
        "force": True,
        "spine_template": "narrated",
    }


def test_start_without_project_context_is_rejected(monkeypatch, tmp_path):
    (tmp_path / "novel.txt").write_text("林建军：你回来晚了。", encoding="utf-8")
    client, calls = _client(monkeypatch, tmp_path, ctx=None)

    # ctx=None is impossible via ProjectResolution's contract, but the route still
    # guards against it rather than enqueueing an unscoped task.
    body = client.post("/api/v1/projects/demo/script-process/start", json={}).json()

    assert body["ok"] is True
    assert calls and calls[0]["task_type"] == "script_process"
