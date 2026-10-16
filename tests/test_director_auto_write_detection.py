"""Write-task detection for the durable director auto run.

A continuation turn only counts as progress when it starts a real DramaClaw
write tool. Describing or searching the tool surface used to look like a write
(the bridge echoes the described tool name), and a read-only turn stopped the
whole one-click run; both are pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novelvideo.chat import director_auto
from novelvideo.chat.director_auto import (
    DirectorAutoCoordinator,
    DirectorAutoRun,
    DirectorAutoStore,
)
from novelvideo.project_context import ProjectContext


def event(name: str, **extra) -> dict:
    return {"type": "tool_started", "name": name, **extra}


def test_describe_of_a_generator_is_not_a_write() -> None:
    described = event("tool_describe", input={"name": "dramaclaw_generate_identity_image"})

    assert director_auto._event_is_write_tool(described) is False


def test_dramaclaw_prefixed_describe_is_not_a_write() -> None:
    described = event(
        "dramaclaw_tool_describe",
        input={"name": "dramaclaw_generate_portrait"},
    )

    assert director_auto._event_is_write_tool(described) is False


def test_search_is_not_a_write() -> None:
    assert director_auto._event_is_write_tool(event("tool_search", input={"query": "肖像"})) is False


def test_tool_call_bridge_unwraps_the_inner_write() -> None:
    bridged = event(
        "tool_call",
        input={"name": "dramaclaw_generate_portrait", "arguments": {"name": "林建军"}},
    )

    assert director_auto._event_is_write_tool(bridged) is True


def test_tool_call_bridge_with_json_string_arguments() -> None:
    bridged = event("tool_call", input='{"name": "dramaclaw_generate_script"}')

    assert director_auto._event_is_write_tool(bridged) is True


def test_tool_call_bridge_to_a_read_tool_is_not_a_write() -> None:
    bridged = event("tool_call", input={"name": "dramaclaw_get"})

    assert director_auto._event_is_write_tool(bridged) is False


def test_tool_call_bridge_without_an_inner_name_is_not_a_write() -> None:
    assert director_auto._event_is_write_tool(event("tool_call")) is False


def test_direct_write_tool_event_still_counts() -> None:
    assert (
        director_auto._event_is_write_tool(
            event("dramaclaw_generate_portrait", input={"name": "林建军"})
        )
        is True
    )


def test_server_prefixed_tool_name_is_collapsed() -> None:
    assert (
        director_auto._event_is_write_tool(event("dramaclaw.dramaclaw_generate_portrait"))
        is True
    )


@pytest.mark.parametrize(
    "name",
    [
        "dramaclaw_get",
        "dramaclaw_list_tasks",
        "dramaclaw_pipeline_status",
        "dramaclaw_get_character_media",
        "skill_view",
        "freezone_run_node_action",
    ],
)
def test_read_and_meta_tools_never_count_as_writes(name: str) -> None:
    assert director_auto._event_is_write_tool(event(name)) is False


def test_failure_detail_reads_status_code_from_a_string_payload() -> None:
    detail = director_auto._event_failure_detail(
        {"output": '{"ok": false, "status_code": 404, "error": "Not Found"}'}
    )

    assert "404" in detail and "Not Found" in detail


def test_failure_detail_is_empty_for_a_successful_payload() -> None:
    assert director_auto._event_failure_detail({"output": {"ok": True, "data": []}}) == ""


def context(tmp_path: Path) -> ProjectContext:
    return ProjectContext(
        project_id="project-1",
        project_name="demo",
        owner_type="user",
        owner_id="user-1",
        owner_username="alice",
        requester_user_id="user-1",
        requester_username="alice",
        requester_principals=(("user", "user-1"),),
        effective_role="owner",
        home_node_id="local",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        is_home_node=True,
    )


@pytest.mark.asyncio
async def test_read_only_continuation_retries_then_pauses_with_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A read-only turn must not kill the run on the first try.

    The observed production failure was a single 404 read followed by an
    immediate hard pause; the run now retries within a bounded budget and the
    pause reason carries the last tool call for diagnosis.
    """
    import asyncio

    ctx = context(tmp_path)

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return []

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.continuations = 0

        async def _notify(self, run, text):
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _continue_owned(self, run):
            self.continuations += 1
            self._remember_continuation(
                run.run_id,
                wrote=False,
                note="dramaclaw_get（404 Not Found）",
            )
            return False

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        DirectorAutoRun(
            run_id="run-1",
            username="alice",
            project_id=ctx.project_id,
            episode=1,
            status="running",
            activated_at="2026-08-14T00:00:00+00:00",
            updated_at="2026-08-14T00:00:00+00:00",
            context_json=director_auto._serialize_context(ctx),
            baseline_task_ids=(),
            handled_task_ids=(),
            scope="project",
        )
    )
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        for _ in range(400):
            saved = store.get("alice", "project-1")
            if saved is not None and saved.status == "paused":
                break
            await asyncio.sleep(0.01)

        saved = store.get("alice", "project-1")
        assert saved is not None
        assert saved.status == "paused"
        # Every tolerated retry ran before the run paused for the user.
        assert coordinator.continuations == director_auto.MAX_TASKLESS_CONTINUATIONS
        assert "自动续跑未产生写任务" in saved.last_error
        assert "dramaclaw_get" in saved.last_error
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_write_turn_that_creates_no_task_uses_the_other_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A write without a task is reported differently from a read-only turn."""

    import asyncio

    ctx = context(tmp_path)

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return []

    class RecordingCoordinator(DirectorAutoCoordinator):
        async def _notify(self, run, text):
            return {"content": text}

        async def _continue_owned(self, run):
            self._remember_continuation(run.run_id, wrote=True, note="")
            return True

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        DirectorAutoRun(
            run_id="run-1",
            username="alice",
            project_id=ctx.project_id,
            episode=1,
            status="running",
            activated_at="2026-08-14T00:00:00+00:00",
            updated_at="2026-08-14T00:00:00+00:00",
            context_json=director_auto._serialize_context(ctx),
            baseline_task_ids=(),
            handled_task_ids=(),
            scope="project",
        )
    )
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        for _ in range(400):
            saved = store.get("alice", "project-1")
            if saved is not None and saved.status == "paused":
                break
            await asyncio.sleep(0.01)

        saved = store.get("alice", "project-1")
        assert saved is not None
        assert saved.status == "paused"
        assert "自动续跑未产生新的任务" in saved.last_error
    finally:
        await coordinator.shutdown()
