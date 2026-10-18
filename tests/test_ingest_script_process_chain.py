"""The import stage hands the imported script to the film-process pack.

The hand-off lives in the ingest runner rather than in the REST handler so the
automatic stage survives a closed browser tab.  It must be best effort: a
rejected hand-off can never fail an import that already succeeded.
"""

from pathlib import Path

import pytest

from novelvideo.project_context import ProjectContext

pytestmark = pytest.mark.m07


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


class _Task:
    """Stand-in for ``TaskState`` (only ``status`` is read by the hand-off)."""

    def __init__(self, status: str) -> None:
        self.status = status


class _RecordingTaskManager:
    def __init__(self, existing: _Task | None = None) -> None:
        self.existing = existing
        self.lookups: list[tuple[str, int]] = []

    def update_progress_for_project(self, *args, **kwargs) -> None:
        return None

    def get_task_for_project(self, ctx, task_type, episode, **kwargs):
        self.lookups.append((task_type, episode))
        return self.existing


class _FakeStore:
    instance: "_FakeStore | None" = None

    def __init__(self, *args, **kwargs) -> None:
        self.closed = False
        type(self).instance = self

    async def initialize(self) -> None:
        return None

    async def ingest_novel_fast(
        self, novel_path, rebuild=False, spine_template=None, on_progress=None, on_log=None
    ):
        if on_progress:
            on_progress(1.0, "导入完成")
        return {"status": "graph_ready"}

    async def close(self) -> None:
        self.closed = True


def _patch_ingest(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manager: _RecordingTaskManager,
    enqueued: list[dict],
    has_result: bool = False,
    enqueue=None,
):
    from novelvideo import cognee, script_process
    from novelvideo.task_backend import client as task_backend_client
    from novelvideo.task_backend.runners import ingest

    monkeypatch.setattr(ingest, "get_task_manager", lambda: manager)
    monkeypatch.setattr(cognee, "CogneeStore", _FakeStore)
    monkeypatch.setattr(
        script_process, "has_script_process_result", lambda _output_dir: has_result
    )

    async def _record_enqueue(ctx, **kwargs):
        enqueued.append(kwargs)
        return None

    monkeypatch.setattr(
        task_backend_client, "enqueue_project_task", enqueue or _record_enqueue
    )
    return ingest


async def test_ingest_chains_script_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _RecordingTaskManager()
    enqueued: list[dict] = []
    ingest = _patch_ingest(monkeypatch, manager=manager, enqueued=enqueued)

    result = await ingest._run_ingest_fast(
        {"payload": {"novel_path": str(tmp_path / "novel.txt")}}, _ctx(tmp_path)
    )

    assert result == {"status": "graph_ready"}
    assert manager.lookups == [("script_process", 0)]
    assert len(enqueued) == 1
    assert enqueued[0]["task_type"] == "script_process"
    assert enqueued[0]["product_surface"] == "mainline"
    assert enqueued[0]["queue_kind"] == "default"
    assert enqueued[0]["episode"] == 0
    assert enqueued[0]["payload"]["output_dir"] == str(tmp_path / "output")
    assert _FakeStore.instance is not None and _FakeStore.instance.closed


async def test_ingest_skips_hand_off_when_result_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueued: list[dict] = []
    ingest = _patch_ingest(
        monkeypatch,
        manager=_RecordingTaskManager(),
        enqueued=enqueued,
        has_result=True,
    )

    await ingest._run_ingest_fast(
        {"payload": {"novel_path": str(tmp_path / "novel.txt")}}, _ctx(tmp_path)
    )

    assert enqueued == []


@pytest.mark.parametrize("status", ["queued", "running"])
async def test_ingest_skips_hand_off_while_stage_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    enqueued: list[dict] = []
    ingest = _patch_ingest(
        monkeypatch,
        manager=_RecordingTaskManager(existing=_Task(status)),
        enqueued=enqueued,
    )

    await ingest._run_ingest_fast(
        {"payload": {"novel_path": str(tmp_path / "novel.txt")}}, _ctx(tmp_path)
    )

    assert enqueued == []


async def test_ingest_survives_a_rejected_hand_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queue rejection must not turn a successful import into a failure."""

    async def _reject(ctx, **kwargs):
        raise RuntimeError("project default queue is full")

    ingest = _patch_ingest(
        monkeypatch,
        manager=_RecordingTaskManager(),
        enqueued=[],
        enqueue=_reject,
    )

    result = await ingest._run_ingest_fast(
        {"payload": {"novel_path": str(tmp_path / "novel.txt")}}, _ctx(tmp_path)
    )

    assert result == {"status": "graph_ready"}
