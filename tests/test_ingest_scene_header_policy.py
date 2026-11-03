"""The task-level 精品剧 scene-header gate obeys the import's ``format_policy``.

The REST pre-check may let a headerless drama through (``format_policy="warn"``,
工作室一键成片), but the ingest task re-checks scene headers on its own.  The
policy must be threaded through the task payload (``config.require_scene_headers``)
so the two gates agree; otherwise the studio flow imports successfully and then
fails inside ``ingest_fast`` — the bug this pins.
"""

from pathlib import Path

import pytest

from novelvideo.project_context import ProjectContext


def _ctx(tmp_path: Path) -> ProjectContext:
    return ProjectContext(
        project_id="proj_ingest",
        project_name="demo",
        owner_type="user",
        owner_id="owner",
        owner_username="alice",
        requester_user_id="editor",
        requester_username="bob",
        requester_principals=(("user", "editor"),),
        effective_role="editor",
        home_node_id="node_a",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        is_home_node=True,
    )


class _Manager:
    def update_progress_for_project(self, *args, **kwargs) -> None:
        return None

    def get_task_for_project(self, *args, **kwargs):
        return None


class _FakeStructuredStore:
    def __init__(self, *args, **kwargs) -> None:
        self.closed = False

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, True),  # 默认严格：内部捏料页 / 旧客户端行为不变
        ({"require_scene_headers": True}, True),
        ({"require_scene_headers": False}, False),  # 工作室 warn 放行
    ],
)
async def test_ingest_runner_forwards_scene_header_policy(
    tmp_path, monkeypatch, config, expected
):
    from novelvideo import knowledge_pipeline, sqlite_store, structured_ingest
    from novelvideo.task_backend.runners import ingest

    seen: dict = {}

    async def fake_ingest(store, novel_path, **kwargs):
        seen.update(kwargs)
        return {"status": "source_ready"}

    monkeypatch.setattr(
        knowledge_pipeline, "is_structured_pipeline", lambda _state_dir: True
    )
    monkeypatch.setattr(sqlite_store, "SQLiteStore", _FakeStructuredStore)
    monkeypatch.setattr(structured_ingest, "ingest_source_text_structured", fake_ingest)
    monkeypatch.setattr(ingest, "get_task_manager", lambda: _Manager())
    monkeypatch.setattr(ingest, "_reset_preprocess_artifacts", lambda _ctx: None)
    monkeypatch.setattr(ingest, "remember_ingest_completion", lambda _ctx: None)

    async def _noop_hand_off(_ctx):
        return None

    monkeypatch.setattr(ingest, "_enqueue_screenplay_preprocess", _noop_hand_off)

    result = await ingest._run_ingest_fast(
        {
            "payload": {
                "novel_path": str(tmp_path / "novel.txt"),
                "config": dict(config),
            }
        },
        _ctx(tmp_path),
    )

    assert result == {"status": "source_ready"}
    assert seen["require_scene_headers"] is expected
