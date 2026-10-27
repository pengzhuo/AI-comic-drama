from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from novelvideo import pipeline_status as pipeline_status_module
from novelvideo import project_config
from novelvideo.chat import director_auto
from novelvideo.chat.director_auto import (
    DirectorAutoCoordinator,
    DirectorAutoRun,
    DirectorAutoStore,
)
from novelvideo.project_context import ProjectContext
from novelvideo.task_state import TaskState


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


def run_record(tmp_path: Path, **overrides) -> DirectorAutoRun:
    ctx = context(tmp_path)
    base = DirectorAutoRun(
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
    return replace(base, **overrides)


def task(
    task_id: str,
    status: str,
    *,
    task_type: str = "compose_episode",
    episode: int = 1,
) -> TaskState:
    return TaskState(
        task_id=task_id,
        task_type=task_type,
        username="alice",
        project="demo",
        project_id="project-1",
        episode=episode,
        status=status,
        updated_at="2026-08-14T01:00:00+00:00",
        completed_at=("2026-08-14T01:00:00+00:00" if status in {"completed", "failed"} else ""),
    )


def test_store_round_trips_project_scope_and_target(tmp_path: Path) -> None:
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    run = run_record(tmp_path, target_episodes=6, config_json='{"visual_style": "anime"}')
    store.upsert(run)

    loaded = store.get("alice", "project-1")

    assert loaded == run
    assert loaded is not None
    assert loaded.is_project_scope is True
    assert loaded.target_episodes == 6
    assert loaded.config_json == '{"visual_style": "anime"}'


@pytest.mark.asyncio
async def test_pause_gate_pauses_when_project_not_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(pipeline_status_module, "_user_has_configured", lambda *_: False)
    coordinator = DirectorAutoCoordinator(DirectorAutoStore(tmp_path / "director-auto.db"))

    reason = await coordinator._pause_gate(run_record(tmp_path))

    assert reason is not None
    assert "配置" in reason


@pytest.mark.asyncio
async def test_pause_gate_requires_voice_policy_before_tts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(pipeline_status_module, "_user_has_configured", lambda *_: True)

    async def fake_status(**_kwargs):
        return {"next_step": "voice_setup"}

    monkeypatch.setattr(pipeline_status_module, "compute_pipeline_status", fake_status)
    coordinator = DirectorAutoCoordinator(DirectorAutoStore(tmp_path / "director-auto.db"))

    async def fake_open(_ctx):
        return object()

    monkeypatch.setattr(coordinator, "_open_ctx_store", fake_open)

    reason = await coordinator._pause_gate(run_record(tmp_path, voice_policy=""))

    assert reason is not None
    assert "声线" in reason


@pytest.mark.asyncio
async def test_pause_gate_allows_when_voice_policy_is_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(pipeline_status_module, "_user_has_configured", lambda *_: True)

    async def fake_status(**_kwargs):
        return {"next_step": "voice_setup"}

    monkeypatch.setattr(pipeline_status_module, "compute_pipeline_status", fake_status)
    coordinator = DirectorAutoCoordinator(DirectorAutoStore(tmp_path / "director-auto.db"))

    async def fake_open(_ctx):
        return object()

    monkeypatch.setattr(coordinator, "_open_ctx_store", fake_open)

    reason = await coordinator._pause_gate(run_record(tmp_path, voice_policy="system"))

    assert reason is None


@pytest.mark.asyncio
async def test_pause_gate_requires_character_images_before_portraits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """一键成片渲染肖像之前，先让用户交自己的角色图片（或跳过）。"""

    monkeypatch.setattr(pipeline_status_module, "_user_has_configured", lambda *_: True)
    monkeypatch.setattr(
        project_config, "character_images_confirmed", lambda *_args, **_kwargs: False
    )

    async def fake_status(**_kwargs):
        return {"next_step": "portraits"}

    monkeypatch.setattr(pipeline_status_module, "compute_pipeline_status", fake_status)
    coordinator = DirectorAutoCoordinator(DirectorAutoStore(tmp_path / "director-auto.db"))

    async def fake_open(_ctx):
        return object()

    monkeypatch.setattr(coordinator, "_open_ctx_store", fake_open)

    reason = await coordinator._pause_gate(run_record(tmp_path))

    assert reason is not None
    assert reason.startswith(director_auto.CHARACTER_IMAGES_GATE_TAG)
    assert "肖像" in reason


@pytest.mark.asyncio
async def test_pause_gate_allows_once_character_images_are_answered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(pipeline_status_module, "_user_has_configured", lambda *_: True)
    monkeypatch.setattr(
        project_config, "character_images_confirmed", lambda *_args, **_kwargs: True
    )

    async def fake_status(**_kwargs):
        return {"next_step": "portraits"}

    monkeypatch.setattr(pipeline_status_module, "compute_pipeline_status", fake_status)
    coordinator = DirectorAutoCoordinator(DirectorAutoStore(tmp_path / "director-auto.db"))

    async def fake_open(_ctx):
        return object()

    monkeypatch.setattr(coordinator, "_open_ctx_store", fake_open)

    assert await coordinator._pause_gate(run_record(tmp_path)) is None


@pytest.mark.asyncio
async def test_pause_gate_requires_identity_images_for_the_planned_episode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """身份图闸门问的是「本集」：其它集答过不算数。"""

    monkeypatch.setattr(pipeline_status_module, "_user_has_configured", lambda *_: True)
    asked: list[int] = []

    def fake_confirmed(_username, _project, episode):
        asked.append(episode)
        return episode == 2

    monkeypatch.setattr(project_config, "identity_images_confirmed", fake_confirmed)

    async def fake_status(**_kwargs):
        return {"next_step": "identity_images", "current_episode": 3}

    monkeypatch.setattr(pipeline_status_module, "compute_pipeline_status", fake_status)
    coordinator = DirectorAutoCoordinator(DirectorAutoStore(tmp_path / "director-auto.db"))

    async def fake_open(_ctx):
        return object()

    monkeypatch.setattr(coordinator, "_open_ctx_store", fake_open)

    reason = await coordinator._pause_gate(run_record(tmp_path, episode=3))

    assert asked == [3]
    assert reason is not None
    assert reason.startswith(director_auto.IDENTITY_IMAGES_GATE_TAG)
    assert "身份图" in reason


@pytest.mark.asyncio
async def test_pause_gate_allows_when_this_episode_is_answered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(pipeline_status_module, "_user_has_configured", lambda *_: True)
    monkeypatch.setattr(
        project_config, "identity_images_confirmed", lambda *_args, **_kwargs: True
    )

    async def fake_status(**_kwargs):
        return {"next_step": "identity_images", "current_episode": 2}

    monkeypatch.setattr(pipeline_status_module, "compute_pipeline_status", fake_status)
    coordinator = DirectorAutoCoordinator(DirectorAutoStore(tmp_path / "director-auto.db"))

    async def fake_open(_ctx):
        return object()

    monkeypatch.setattr(coordinator, "_open_ctx_store", fake_open)

    assert await coordinator._pause_gate(run_record(tmp_path, episode=2)) is None


def test_pause_reason_display_strips_the_gate_marker() -> None:
    for tag, reason in (
        (director_auto.CHARACTER_IMAGES_GATE_TAG, director_auto.CHARACTER_IMAGES_REQUIRED_ERROR),
        (director_auto.IDENTITY_IMAGES_GATE_TAG, director_auto.IDENTITY_IMAGES_REQUIRED_ERROR),
    ):
        assert director_auto._pause_reason_display(reason) == reason[len(tag) :].strip()

    assert director_auto._pause_reason_display("项目配置缺失") == "项目配置缺失"


def test_director_auto_payload_exposes_the_gate() -> None:
    from novelvideo.api.routes.chat import _director_auto_gate

    assert _director_auto_gate(director_auto.CHARACTER_IMAGES_REQUIRED_ERROR) == "character_images"
    assert _director_auto_gate(director_auto.IDENTITY_IMAGES_REQUIRED_ERROR) == "identity_images"
    assert _director_auto_gate("项目配置缺失（如视觉风格、叙事方式等），请先完成配置") is None
    assert _director_auto_gate("") is None


@pytest.mark.asyncio
async def test_next_incomplete_episode_respects_target_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        pipeline_status_module,
        "incomplete_episodes",
        lambda _store, _dir: [5, 6, 7],
    )
    coordinator = DirectorAutoCoordinator(DirectorAutoStore(tmp_path / "director-auto.db"))

    async def fake_open(_ctx):
        return object()

    monkeypatch.setattr(coordinator, "_open_ctx_store", fake_open)

    assert await coordinator._next_incomplete_episode(run_record(tmp_path)) == 5
    assert (
        await coordinator._next_incomplete_episode(run_record(tmp_path, target_episodes=6)) == 5
    )
    # A bound at 4 excludes every incomplete episode.
    assert await coordinator._next_incomplete_episode(run_record(tmp_path, target_episodes=4)) is None


@pytest.mark.asyncio
async def test_project_scope_advances_to_next_episode_after_compose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    composed = task("compose-ep1", "completed", episode=1)

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [composed]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.continuations = 0
            self.episodes: list[int] = []

        async def _next_incomplete_episode(self, run):
            return 2

        async def _notify(self, run, text):
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False, instruction=None):
            self.continuations += 1
            self.episodes.append(run.episode)
            await self.pause(
                username=run.username,
                project_id=run.project_id,
                reason="test-stop",
            )
            return True

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path))
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        for _ in range(200):
            saved = store.get("alice", "project-1")
            if saved is not None and saved.status == "paused":
                break
            await asyncio.sleep(0.01)

        saved = store.get("alice", "project-1")
        assert saved is not None
        assert coordinator.continuations == 1
        assert coordinator.episodes == [2]
        assert saved.episode == 2
    finally:
        await coordinator.shutdown()
