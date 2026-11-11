from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import replace
from pathlib import Path

import pytest

from novelvideo.chat import director_auto
from novelvideo.chat import moderation_prompt_repair
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


def task(
    task_id: str,
    status: str,
    *,
    task_type: str = "identity_image",
    error: str | None = None,
) -> TaskState:
    return TaskState(
        task_id=task_id,
        task_type=task_type,
        username="alice",
        project="demo",
        project_id="project-1",
        episode=1,
        status=status,
        updated_at="2026-08-14T01:00:00+00:00",
        completed_at=("2026-08-14T01:00:00+00:00" if status in {"completed", "failed"} else ""),
        error=error,
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
    )
    return replace(base, **overrides)


def test_store_round_trips_durable_run(tmp_path: Path) -> None:
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    blocked = (
        {
            "episode": 1,
            "beat": 12,
            "stage": "single_video",
            "code": "CONTENT_MODERATION_FAILED",
            "reason": "内容审核未通过",
            "degraded": True,
            "at": "2026-08-14T00:00:00+00:00",
        },
    )
    run = run_record(
        tmp_path,
        baseline_task_ids=("old",),
        handled_task_ids=("done",),
        voice_policy="system",
        stage_key="single_video",
        stage_attempts=2,
        next_attempt_at=1755000000.5,
        blocked_shots=blocked,
    )
    store.upsert(run)

    loaded = store.get("alice", "project-1")

    assert loaded == run
    assert loaded is not None
    assert loaded.stage_key == "single_video"
    assert loaded.stage_attempts == 2
    assert loaded.next_attempt_at == 1755000000.5
    assert loaded.blocked_shots == blocked
    assert loaded.context.output_dir == context(tmp_path).output_dir
    assert store.active() == [run]


def test_store_migrates_a_run_table_without_retry_columns(tmp_path: Path) -> None:
    """A running deployment's DB predates the stage retry columns."""

    import sqlite3

    db_path = tmp_path / "director-auto.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE director_auto_runs (
          run_id TEXT PRIMARY KEY,
          username TEXT NOT NULL,
          project_id TEXT NOT NULL,
          episode INTEGER NOT NULL,
          status TEXT NOT NULL,
          activated_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          context_json TEXT NOT NULL,
          baseline_task_ids_json TEXT NOT NULL,
          handled_task_ids_json TEXT NOT NULL,
          recovery_keys_json TEXT NOT NULL DEFAULT '[]',
          voice_policy TEXT NOT NULL DEFAULT '',
          last_error TEXT NOT NULL DEFAULT '',
          scope TEXT NOT NULL DEFAULT 'episode',
          target_episodes INTEGER NOT NULL DEFAULT 0,
          config_json TEXT NOT NULL DEFAULT '{}',
          driver TEXT NOT NULL DEFAULT 'agent',
          owner_token TEXT NOT NULL DEFAULT '',
          lease_expires_at REAL NOT NULL DEFAULT 0,
          UNIQUE(username, project_id)
        )
        """
    )
    legacy.commit()
    legacy.close()

    store = DirectorAutoStore(db_path)
    store.upsert(run_record(tmp_path))
    loaded = store.get("alice", "project-1")

    assert loaded is not None
    assert loaded.stage_key == ""
    assert loaded.stage_attempts == 0
    assert loaded.next_attempt_at == 0.0


def test_store_lease_claim_is_atomic_and_expired_lease_can_be_reclaimed(tmp_path: Path) -> None:
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    run = run_record(tmp_path)
    store.upsert(run)

    assert store.claim_lease(run.run_id, "worker-a", lease_seconds=10, now=100) is True
    assert store.claim_lease(run.run_id, "worker-b", lease_seconds=10, now=101) is False
    assert store.owns_lease(run.run_id, "worker-a", now=101) is True
    assert store.claim_lease(run.run_id, "worker-b", lease_seconds=10, now=111) is True
    assert store.owns_lease(run.run_id, "worker-a", now=111) is False
    assert store.owns_lease(run.run_id, "worker-b", now=111) is True


@pytest.mark.asyncio
async def test_stale_owner_cannot_overwrite_pause_or_release_new_owner(
    tmp_path: Path,
) -> None:
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    original = run_record(tmp_path)
    store.upsert(original)
    base = time.time()

    assert store.claim_lease(original.run_id, "worker-a", lease_seconds=10, now=base)
    assert store.claim_lease(
        original.run_id,
        "worker-b",
        lease_seconds=120,
        now=base + 11,
    )
    owned_by_b = replace(original, handled_task_ids=("handled-by-b",))
    assert store.update_if_owned(owned_by_b, "worker-b", now=base + 12)

    stale_snapshot = replace(original, handled_task_ids=("stale-a",))
    assert not store.update_if_owned(stale_snapshot, "worker-a", now=base + 12)
    assert not store.release_lease(original.run_id, "worker-a")
    assert store.owns_lease(original.run_id, "worker-b", now=base + 12)

    stale = DirectorAutoCoordinator(DirectorAutoStore(store.db_path))
    stale.owner_token = "worker-a"
    assert (
        await stale._pause_owned(
            run_id=original.run_id,
            username=original.username,
            project_id=original.project_id,
            reason="stale worker timeout",
        )
        is None
    )
    saved = store.get(original.username, original.project_id)
    assert saved is not None
    assert saved.status == "running"
    assert saved.handled_task_ids == ("handled-by-b",)
    assert store.owns_lease(original.run_id, "worker-b", now=base + 12)


@pytest.mark.asyncio
async def test_two_coordinators_only_one_process_continues_the_same_run(
    tmp_path: Path,
) -> None:
    entered: list[str] = []
    release = asyncio.Event()

    class RecordingCoordinator(DirectorAutoCoordinator):
        async def _run(self, run_id, _username, _project_id):
            entered.append(self.owner_token)
            await release.wait()

    db_path = tmp_path / "director-auto.db"
    store_a = DirectorAutoStore(db_path)
    store_a.upsert(run_record(tmp_path))
    first = RecordingCoordinator(store_a)
    second = RecordingCoordinator(DirectorAutoStore(db_path))
    run = store_a.get("alice", "project-1")
    assert run is not None

    first._ensure_worker(run)
    second._ensure_worker(run)
    await asyncio.sleep(0.05)

    assert len(entered) == 1
    assert entered[0] in {first.owner_token, second.owner_token}
    release.set()
    await first.shutdown()
    await second.shutdown()


@pytest.mark.asyncio
async def test_final_delivery_turn_carries_the_film_pack_sound_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """成片交付这一步带上流程包第 06 份（合成本身没有发往模型的提示词入口）。"""

    from novelvideo.skills import ai_film_process

    prompts: list[str] = []
    ctx = context(tmp_path)

    async def fake_resolve_project_context(**_kwargs):
        return ctx

    async def fake_stream_reply(_username, _project_id, prompt, _on_event, **_kwargs):
        prompts.append(prompt)

    monkeypatch.setattr(director_auto, "resolve_project_context", fake_resolve_project_context)
    monkeypatch.setattr(
        director_auto.chat_service,
        "chat_run_lock_is_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        director_auto.chat_service,
        "stream_assistant_reply",
        fake_stream_reply,
    )
    coordinator = DirectorAutoCoordinator(DirectorAutoStore(tmp_path / "director-auto.db"))
    run = run_record(tmp_path, episode=2)

    assert await coordinator._agent_continue(run, final_delivery=True) is False
    assert "制作流程包规范" in prompts[0]
    assert "成片验收" in prompts[0]

    # 普通推进回合不带第 06 份，避免每回合都多背一段规范。
    prompts.clear()
    assert await coordinator._agent_continue(run) is False
    assert "制作流程包规范" not in prompts[0]

    # 总开关关掉后连交付回合也不注入。
    prompts.clear()
    monkeypatch.setenv(ai_film_process.PACK_INJECTION_ENV, "0")
    assert await coordinator._agent_continue(run, final_delivery=True) is False
    assert "制作流程包规范" not in prompts[0]


@pytest.mark.asyncio
async def test_agent_continuation_is_pinned_to_the_auto_run_episode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prompts: list[str] = []
    ctx = context(tmp_path)

    async def fake_resolve_project_context(**_kwargs):
        return ctx

    async def fake_stream_reply(_username, _project_id, prompt, _on_event, **_kwargs):
        prompts.append(prompt)

    monkeypatch.setattr(director_auto, "resolve_project_context", fake_resolve_project_context)
    monkeypatch.setattr(
        director_auto.chat_service,
        "chat_run_lock_is_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        director_auto.chat_service,
        "stream_assistant_reply",
        fake_stream_reply,
    )
    coordinator = DirectorAutoCoordinator(DirectorAutoStore(tmp_path / "director-auto.db"))
    run = run_record(tmp_path, episode=2)

    wrote = await coordinator._agent_continue(run)

    assert wrote is False
    assert "只继续第 2 集的下一步" in prompts[0]
    assert "不得启动、修改或推进其他集" in prompts[0]


@pytest.mark.asyncio
async def test_resume_only_restarts_persisted_running_sessions(tmp_path: Path) -> None:
    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.resumed: list[str] = []

        def _ensure_worker(self, run):
            self.resumed.append(run.run_id)

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path, run_id="running"))
    store.upsert(
        run_record(
            tmp_path,
            run_id="paused",
            project_id="project-2",
            status="paused",
        )
    )
    coordinator = RecordingCoordinator(store)

    await coordinator.resume()

    assert coordinator.resumed == ["running"]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_start_adopts_existing_active_task_but_ignores_terminal_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [task("old-complete", "completed"), task("already-running", "running")]

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    coordinator = DirectorAutoCoordinator(store)

    run = await coordinator.start(username="alice", ctx=context(tmp_path), episode=1)
    await coordinator.pause(username="alice", project_id="project-1")

    assert run.baseline_task_ids == ("old-complete",)
    assert "already-running" not in run.baseline_task_ids
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_completed_task_notifies_and_triggers_one_server_continuation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    completed = task("new-complete", "completed")

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [completed]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []
            self.continuations = 0

        async def _notify(self, run, text):
            self.notifications.append(text)
            return {"content": text}

        async def _agent_continue(self, run, *, final_delivery=False):
            self.continuations += 1
            await self.pause(username=run.username, project_id=run.project_id, reason="test-stop")
            return True

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path))
    coordinator = RecordingCoordinator(store)

    coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
    await asyncio.sleep(0.08)

    assert coordinator.continuations == 1
    assert coordinator.notifications == ["✅ [角色身份图] 已完成。本集自动正在继续下一步。"]
    saved = store.get("alice", "project-1")
    assert saved is not None
    assert saved.handled_task_ids == ("new-complete",)
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_completed_task_that_does_not_advance_the_stage_still_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A completed task that leaves the same stage pending must not spin forever.

    The realistic shape: one task for the stage finishes, the stage is still the
    next step, and the dispatcher refuses to enqueue it again (its dedupe
    signature is already recorded). The coordinator must spend a bounded budget
    and then pause, both because a retry can fix a written-but-unseen output and
    because an unbounded loop would burn LLM turns and provider credits.
    """

    completed_task = task(
        "tts-1", "completed", task_type="audio_generation_indextts2"
    )

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [completed_task]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []
            self.continuations = 0

        async def _notify(self, run, text):
            self.notifications.append(text)
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _continue_owned(self, run):
            # Never writes a task: the coordinator has to stop on its own.
            self.continuations += 1
            self._remember_continuation(run.run_id, wrote=False, note="")
            return False

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    # ``run_record`` activates the run in the distant past, so the taskless branch
    # is reachable immediately; the retry budget is what keeps this bounded.
    monkeypatch.setattr(director_auto, "STAGE_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path))
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        for _ in range(600):
            saved = store.get("alice", "project-1")
            if saved is not None and saved.status == "paused":
                break
            await asyncio.sleep(0.01)

        saved = store.get("alice", "project-1")
        assert saved is not None and saved.status == "paused"
        assert saved.stage_attempts == director_auto.MAX_STAGE_ATTEMPTS
        assert saved.stage_key == "audio_generation_indextts2"
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_taskless_pause_reports_the_gate_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """空转暂停也要报出闸门原因，否则前端弹不出声线/肖像卡。

    现场（2026-11）：项目停在 ``voice_setup``（缺声线策略）时确定性派发不产生
    写任务，暂停文案只有「自动续跑未产生写任务」——前端 ``isVoicePolicyPause``
    认的是 ``未选择声线策略`` 子串，于是卡片不弹、客户点「继续制作」再空转一轮。

    这里刻意**不给任何任务**：有完成态任务时会先走「任务完成」那条闸门分支
    （``_run`` 里 ``3155`` 那个调用点），就测不到空转分支了。
    """

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return []

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []

        async def _notify(self, run, text):
            self.notifications.append(text)
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _continue_owned(self, run):
            # Never writes a task: the coordinator has to stop on its own.
            self._remember_continuation(run.run_id, wrote=False, note="skill")
            return False

        async def _pause_gate(self, run):
            return director_auto.VOICE_POLICY_REQUIRED_ERROR

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(director_auto, "STAGE_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path, scope="project"))
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        for _ in range(600):
            saved = store.get("alice", "project-1")
            if saved is not None and saved.status == "paused":
                break
            await asyncio.sleep(0.01)

        saved = store.get("alice", "project-1")
        assert saved is not None and saved.status == "paused"
        # 暂停原因是闸门本身，而不是「未产生写任务」——前端靠这个子串弹声线卡。
        assert saved.last_error == director_auto.VOICE_POLICY_REQUIRED_ERROR
        assert saved.pause_kind == director_auto.PAUSE_KIND_VOICE
        # 闸门在耗尽空转预算那一刻直接回报，不再多花一段「重试 3 次」。
        assert saved.stage_attempts == 0
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_taskless_turn_checks_the_gate_before_asking_the_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """闸门（例如缺声线策略）在空转的第一轮就拦住，不再先问捏导。

    现场（2026-11）：run 停在 ``voice_setup``，捏导每轮都「补一个写任务」——反复
    重出早就齐备的草图网格。它既不推进阶段，又把空转预算不断清零，run 永远落不到
    该停的闸门上，白烧钱还占住阶段。
    """

    from novelvideo import pipeline_status

    agent_turns: list[str] = []

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return []

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []

        async def _notify(self, run, text):
            self.notifications.append(text)
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(
            self, run, *, final_delivery=False, instruction=None
        ):
            agent_turns.append(run.run_id)
            self._remember_continuation(run.run_id, wrote=True, note="sketch_grid")
            return True

    async def status(**_kwargs):
        return {"next_step": "voice_setup", "current_episode": 1, "episode_status": {}}

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(pipeline_status, "_user_has_configured", lambda *_: True)
    monkeypatch.setattr(pipeline_status, "compute_pipeline_status", status)
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path, scope="project"))
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        for _ in range(600):
            saved = store.get("alice", "project-1")
            if saved is not None and saved.status == "paused":
                break
            await asyncio.sleep(0.01)

        saved = store.get("alice", "project-1")
        assert saved is not None and saved.status == "paused"
        assert saved.pause_kind == director_auto.PAUSE_KIND_VOICE
        assert saved.last_error == director_auto.VOICE_POLICY_REQUIRED_ERROR
        # 关键：闸门在问捏导之前就拦住了。
        assert agent_turns == []
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("store_read_delay", [0, 0.1])
async def test_failure_retries_before_pausing_without_agent_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store_read_delay: float,
) -> None:
    failed = task("new-failed", "failed")
    failed.error = "upstream rejected"
    notified = asyncio.Event()

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [failed]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []
            self.continuations = 0

        async def _notify(self, run, text):
            self.notifications.append(text)
            notified.set()
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False):
            self.continuations += 1
            return True

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    # Backoff is a production concern; the retry contract is what this asserts.
    monkeypatch.setattr(director_auto, "STAGE_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path))
    original_get = store.get

    def delayed_get(*args, **kwargs):
        # Slow SQLite/thread scheduling must not turn this into a timing assertion.
        time.sleep(store_read_delay)
        return original_get(*args, **kwargs)

    monkeypatch.setattr(store, "get", delayed_get)
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        await asyncio.wait_for(notified.wait(), timeout=5)

        saved = store.get("alice", "project-1")
        # The first failure must not abandon the run: the stage is retried.
        assert saved is not None and saved.status == "running"
        assert saved.stage_key == "identity_image"
        assert saved.stage_attempts >= 1
        assert "正在自动重试" in coordinator.notifications[0]

        for _ in range(600):
            saved = store.get("alice", "project-1")
            if saved is not None and saved.status == "paused":
                break
            await asyncio.sleep(0.01)

        saved = store.get("alice", "project-1")
        assert saved is not None and saved.status == "paused"
        # The pause still carries the failure that started the retry chain.
        assert "upstream rejected" in saved.last_error
        assert saved.stage_attempts == director_auto.MAX_STAGE_ATTEMPTS
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_stage_retry_can_be_disabled_for_immediate_pause(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``DIRECTOR_AUTO_STAGE_RETRY=0`` restores pause-on-first-failure."""

    failed = task("new-failed", "failed")
    failed.error = "upstream rejected"
    notified = asyncio.Event()

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [failed]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []
            self.continuations = 0

        async def _notify(self, run, text):
            self.notifications.append(text)
            notified.set()
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False):
            self.continuations += 1
            return True

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    monkeypatch.setenv(director_auto.STAGE_RETRY_ENV, "0")
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path))
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        await asyncio.wait_for(notified.wait(), timeout=5)

        saved = store.get("alice", "project-1")
        assert saved is not None and saved.status == "paused"
        assert coordinator.continuations == 0
        assert "upstream rejected" in coordinator.notifications[0]
        # The kill switch means no attempt was spent, and the message must not
        # claim otherwise.
        assert "已自动重试 0 次" in coordinator.notifications[0]
    finally:
        await coordinator.shutdown()


def test_is_fatal_failure_never_spends_the_retry_budget() -> None:
    credits = task("billing", "failed", error="insufficient credits for user u: required 3, available 0")
    credits.metadata = {"error_code": "INSUFFICIENT_CREDITS"}
    egress = task("egress", "failed", error="organization egress is denied")
    egress.metadata = {"error_code": "ORG_EGRESS_DENIED"}
    cancelled = task("cancelled", "cancelled")
    transient = task("flaky", "failed", error="HTTP 502 Bad Gateway")
    overflow = task("script_process", "failed", error="第 3/12 段输出超窗：请核实真实上下文窗口")
    overflow.metadata = {"error_code": "MODEL_CONTEXT_BUDGET_EXCEEDED"}

    assert director_auto._is_fatal_failure(credits) == "积分不足，请联系管理员充值（insufficient credits for user u: required 3, available 0）"
    assert director_auto._is_fatal_failure(egress)
    assert director_auto._is_fatal_failure(cancelled) == "任务已取消"
    assert director_auto._is_fatal_failure(transient) is None
    # 上下文超窗是确定性失败：重试整个阶段只会重复同样的失败，因此直接暂停。
    assert "上下文预算不足" in (director_auto._is_fatal_failure(overflow) or "")


def test_stage_retry_delay_walks_the_backoff_and_saturates() -> None:
    assert director_auto._stage_retry_delay(1) == 10.0
    assert director_auto._stage_retry_delay(2) == 30.0
    assert director_auto._stage_retry_delay(3) == 60.0
    assert director_auto._stage_retry_delay(9) == 60.0


def test_fatal_failure_pauses_immediately_without_retrying(
    tmp_path: Path,
) -> None:
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            stage_key="single_video",
            stage_attempts=1,
            recovery_keys=("dispatch:single_video:1:1-3", "missing-character-portrait:锦绣"),
        )
    )
    coordinator = DirectorAutoCoordinator(store)
    saved = store.get("alice", "project-1")

    assert coordinator._clear_stage_signatures(saved) == ("missing-character-portrait:锦绣",)


def test_missing_portrait_recovery_only_matches_exact_identity_failure() -> None:
    recoverable = task(
        "identity-failed",
        "failed",
        error="请先为角色「锦绣」生成 Portrait（面部特写）",
    )
    unrelated_task = task(
        "portrait-failed",
        "failed",
        task_type="character_portrait",
        error="请先为角色「锦绣」生成 Portrait（面部特写）",
    )
    unrelated_error = task("other-failed", "failed", error="模型调用失败")

    assert director_auto._missing_character_portrait(recoverable) == "锦绣"
    assert director_auto._missing_character_portrait(unrelated_task) is None
    assert director_auto._missing_character_portrait(unrelated_error) is None


@pytest.mark.asyncio
async def test_missing_portrait_is_repaired_once_without_pausing_auto_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    failed = task(
        "identity-failed",
        "failed",
        error="请先为角色「锦绣」生成 Portrait（面部特写）",
    )
    portrait = task("portrait-running", "running", task_type="character_portrait")

    class Manager:
        repair_started = False

        def list_tasks_for_project(self, _ctx):
            return [failed, portrait] if self.repair_started else [failed]

    manager = Manager()

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []
            self.instructions: list[str] = []

        async def _notify(self, run, text):
            self.notifications.append(text)
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False, instruction=None):
            assert instruction is not None
            self.instructions.append(instruction)
            manager.repair_started = True
            return True

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: manager)
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path))
    coordinator = RecordingCoordinator(store)

    coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
    await asyncio.sleep(0.08)

    saved = store.get("alice", "project-1")
    assert saved is not None and saved.status == "running"
    assert saved.handled_task_ids == ("identity-failed",)
    assert saved.recovery_keys == ("missing-character-portrait:锦绣",)
    assert len(coordinator.instructions) == 1
    assert "仅启动该角色的一个 Portrait" in coordinator.instructions[0]
    assert coordinator.notifications == [
        "检测到 [角色身份图] 缺少前置肖像，正在自动补生成「锦绣」肖像。"
    ]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_repeated_missing_portrait_failure_pauses_instead_of_looping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    failed = task(
        "identity-failed-again",
        "failed",
        error="请先为角色「锦绣」生成 Portrait（面部特写）",
    )

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [failed]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.continuations = 0

        async def _notify(self, run, text):
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False, instruction=None):
            self.continuations += 1
            return True

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(director_auto, "STAGE_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(tmp_path, recovery_keys=("missing-character-portrait:锦绣",))
    )
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        for _ in range(600):
            saved = store.get("alice", "project-1")
            if saved is not None and saved.status == "paused":
                break
            await asyncio.sleep(0.01)

        saved = store.get("alice", "project-1")
        # The one-shot portrait repair is already spent, so this failure gets the
        # bounded stage retry instead of an unbounded repair loop — and it does
        # stop on its own once the budget is exhausted.
        assert saved is not None and saved.status == "paused"
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_suspend_and_resume_preserve_the_same_auto_run(tmp_path: Path) -> None:
    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.started: list[str] = []
            self.statuses: list[str] = []

        def _ensure_worker(self, run):
            self.started.append(run.run_id)

        async def _broadcast_status(self, run, **_kwargs):
            self.statuses.append(run.status)

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    original = run_record(
        tmp_path,
        handled_task_ids=("task-already-finished",),
        recovery_keys=("missing-character-portrait:锦绣",),
    )
    store.upsert(original)
    coordinator = RecordingCoordinator(store)

    suspended = await coordinator.suspend_for_confirmation(
        username="alice",
        project_id="project-1",
        reason="用户可能要修改镜头",
    )
    assert suspended is not None
    assert suspended.status == "awaiting_confirmation"
    assert suspended.run_id == original.run_id
    assert suspended.handled_task_ids == original.handled_task_ids

    resumed = await coordinator.resume_suspended(
        username="alice",
        project_id="project-1",
    )
    assert resumed is not None
    assert resumed.status == "running"
    assert resumed.run_id == original.run_id
    assert resumed.recovery_keys == original.recovery_keys
    assert coordinator.started == [original.run_id]
    assert coordinator.statuses == ["awaiting_confirmation", "running"]


@pytest.mark.asyncio
async def test_hard_paused_auto_run_cannot_be_resumed_as_declined_change(tmp_path: Path) -> None:
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path, status="paused", last_error="生成失败"))
    coordinator = DirectorAutoCoordinator(store)

    with pytest.raises(ValueError, match="只有等待修改确认"):
        await coordinator.resume_suspended(username="alice", project_id="project-1")


@pytest.mark.asyncio
async def test_paused_auto_run_can_be_continued_with_a_voice_policy(tmp_path: Path) -> None:
    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.started: list[str] = []
            self.statuses: list[str] = []

        def _ensure_worker(self, run):
            self.started.append(run.run_id)

        async def _broadcast_status(self, run, **_kwargs):
            self.statuses.append(run.status)

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    original = run_record(
        tmp_path,
        status="paused",
        last_error=director_auto.VOICE_POLICY_REQUIRED_ERROR,
        stage_key="selected_regen",
        stage_attempts=2,
        next_attempt_at=123.0,
        handled_task_ids=("task-already-finished",),
        recovery_keys=(
            "dispatch:selected_regen:1",
            "missing-character-portrait:锦绣",
        ),
    )
    store.upsert(original)
    coordinator = RecordingCoordinator(store)

    continued = await coordinator.resume_paused(
        username="alice",
        project_id="project-1",
        voice_policy="system",
    )

    assert continued is not None
    assert continued.status == "running"
    assert continued.run_id == original.run_id
    assert continued.voice_policy == "system"
    assert continued.last_error == ""
    # ``stage_key`` is deliberately preserved: it lets the loop tell "advanced to
    # another stage" (progress → reset the recovery budget) from "the same stage
    # failed again" (no progress). Clearing it would make every resume look like
    # progress and defeat the bounded auto-recovery. Re-dispatch is unblocked by
    # clearing ``recovery_keys`` below, which is what the dispatcher dedupes on.
    assert continued.stage_key == "selected_regen"
    assert continued.stage_attempts == 0
    assert continued.next_attempt_at == 0.0
    # The dispatcher dedupe keys must go too, or the deterministic fast path
    # would treat the paused stage as already attempted and never re-enqueue it.
    assert continued.recovery_keys == ("missing-character-portrait:锦绣",)
    # The work already dispatched before the pause is not forgotten.
    assert continued.handled_task_ids == original.handled_task_ids
    assert coordinator.started == [original.run_id]
    assert coordinator.statuses == ["running"]


@pytest.mark.asyncio
async def test_continue_keeps_the_existing_policy_and_rejects_terminal_runs(
    tmp_path: Path,
) -> None:
    class QuietCoordinator(DirectorAutoCoordinator):
        def _ensure_worker(self, run):
            return None

        async def _broadcast_status(self, run, **_kwargs):
            return None

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(tmp_path, status="paused", voice_policy="custom", last_error="生成失败")
    )
    coordinator = QuietCoordinator(store)

    continued = await coordinator.resume_paused(username="alice", project_id="project-1")
    assert continued is not None and continued.voice_policy == "custom"

    for terminal in ("completed", "failed", "cancelled", "awaiting_confirmation"):
        store.upsert(run_record(tmp_path, status=terminal))
        with pytest.raises(ValueError, match="只有已暂停"):
            await coordinator.resume_paused(username="alice", project_id="project-1")

    assert await coordinator.resume_paused(username="alice", project_id="missing") is None


@pytest.mark.asyncio
async def test_continue_is_refused_while_the_cost_gate_is_unanswered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """收费闸门只能从报价卡确认：未确认时任何「继续」都要被拦下。

    线上事故：客户在工作室点横幅上的「继续制作」→ ``/director-auto/continue`` →
    ``resume_paused`` 把状态改回 ``running``，协调器下一轮直接派发 ``asset_references``
    / ``sketch_generation``——预扣伴点那一步（``_charge_points_for_episode``）只在报价卡
    的确认端点里，于是一整集白生成。
    """

    from novelvideo import project_config

    class QuietCoordinator(DirectorAutoCoordinator):
        def _ensure_worker(self, run):
            raise AssertionError("未确认的收费闸门不该唤醒 worker")

        async def _broadcast_status(self, run, **_kwargs):
            raise AssertionError("未确认的收费闸门不该广播状态")

    monkeypatch.setattr(project_config, "cost_estimate_gate_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(project_config, "cost_estimate_confirmed", lambda *_a, **_k: False)

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            status="paused",
            last_error=director_auto.COST_ESTIMATE_REQUIRED_ERROR,
        )
    )
    coordinator = QuietCoordinator(store)

    with pytest.raises(ValueError, match="尚未确认"):
        await coordinator.resume_paused(username="alice", project_id="project-1")

    # 闸门没答：run 必须留在 paused，不能被改成 running。
    still = store.get("alice", "project-1")
    assert still is not None and still.status == "paused"


@pytest.mark.asyncio
async def test_continue_after_the_cost_gate_is_answered_goes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """客户点过报价卡（答案已写）后，``resume_paused`` 照常放行。"""

    from novelvideo import project_config

    class QuietCoordinator(DirectorAutoCoordinator):
        def _ensure_worker(self, run):
            return None

        async def _broadcast_status(self, run, **_kwargs):
            return None

    monkeypatch.setattr(project_config, "cost_estimate_gate_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(project_config, "cost_estimate_confirmed", lambda *_a, **_k: True)

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            status="paused",
            last_error=director_auto.COST_ESTIMATE_REQUIRED_ERROR,
        )
    )
    coordinator = QuietCoordinator(store)

    continued = await coordinator.resume_paused(username="alice", project_id="project-1")
    assert continued is not None
    assert continued.status == "running"


@pytest.mark.asyncio
async def test_switching_the_cost_gate_off_unblocks_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运维把 ``production_estimate_gate`` 关掉就是放行（与 ``_pause_gate`` 同口径）。"""

    from novelvideo import project_config

    class QuietCoordinator(DirectorAutoCoordinator):
        def _ensure_worker(self, run):
            return None

        async def _broadcast_status(self, run, **_kwargs):
            return None

    monkeypatch.setattr(project_config, "cost_estimate_gate_enabled", lambda *_a, **_k: False)
    monkeypatch.setattr(project_config, "cost_estimate_confirmed", lambda *_a, **_k: False)

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            status="paused",
            last_error=director_auto.COST_ESTIMATE_REQUIRED_ERROR,
        )
    )
    coordinator = QuietCoordinator(store)

    continued = await coordinator.resume_paused(username="alice", project_id="project-1")
    assert continued is not None
    assert continued.status == "running"


@pytest.mark.asyncio
async def test_other_pauses_still_continue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：非收费闸门的暂停不受这道拦截影响，也不该去读成本闸门配置。"""

    from novelvideo import project_config

    class QuietCoordinator(DirectorAutoCoordinator):
        def _ensure_worker(self, run):
            return None

        async def _broadcast_status(self, run, **_kwargs):
            return None

    def _boom(*_a, **_k):
        raise AssertionError("非收费闸门不该读成本闸门配置")

    monkeypatch.setattr(project_config, "cost_estimate_gate_enabled", _boom)
    monkeypatch.setattr(project_config, "cost_estimate_confirmed", _boom)

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            status="paused",
            last_error=director_auto.VOICE_POLICY_REQUIRED_ERROR,
        )
    )
    coordinator = QuietCoordinator(store)

    continued = await coordinator.resume_paused(
        username="alice", project_id="project-1", voice_policy="system"
    )
    assert continued is not None
    assert continued.status == "running"


@pytest.mark.asyncio
async def test_continue_after_voice_setup_only_revives_the_voice_gate(tmp_path: Path) -> None:
    class QuietCoordinator(DirectorAutoCoordinator):
        def _ensure_worker(self, run):
            return None

        async def _broadcast_status(self, run, **_kwargs):
            return None

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    coordinator = QuietCoordinator(store)

    store.upsert(
        run_record(tmp_path, status="paused", last_error=director_auto.VOICE_POLICY_REQUIRED_ERROR)
    )
    revived = await coordinator._continue_paused_after_voice_setup(
        username="alice", project_id="project-1"
    )
    assert revived is not None
    assert revived.status == "running"
    assert revived.voice_policy == "system"

    # A pause with any other cause keeps its own reason and its own decision.
    store.upsert(run_record(tmp_path, status="paused", last_error="生成内容涉及版权限制"))
    untouched = await coordinator._continue_paused_after_voice_setup(
        username="alice", project_id="project-1"
    )
    assert untouched is not None and untouched.status == "paused"
    assert untouched.voice_policy == ""

    # Nothing to do when no run exists.
    assert (
        await coordinator._continue_paused_after_voice_setup(
            username="alice", project_id="missing"
        )
        is None
    )


@pytest.mark.asyncio
async def test_resume_paused_from_thread_hands_off_to_the_application_loop(
    tmp_path: Path,
) -> None:
    class QuietCoordinator(DirectorAutoCoordinator):
        def _ensure_worker(self, run):
            return None

        async def _broadcast_status(self, run, **_kwargs):
            return None

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(tmp_path, status="paused", last_error=director_auto.VOICE_POLICY_REQUIRED_ERROR)
    )
    coordinator = QuietCoordinator(store)

    # Without a captured loop the hand-off is refused rather than raising.
    assert (
        coordinator.resume_after_system_voice_setup_from_thread(
            username="alice", project_id="project-1"
        )
        is False
    )
    assert store.get("alice", "project-1").status == "paused"  # type: ignore[union-attr]

    coordinator._app_loop = asyncio.get_running_loop()
    assert (
        coordinator.resume_after_system_voice_setup_from_thread(
            username="alice", project_id="project-1"
        )
        is True
    )
    for _ in range(100):
        run = store.get("alice", "project-1")
        if run is not None and run.status == "running":
            break
        await asyncio.sleep(0.01)

    saved = store.get("alice", "project-1")
    assert saved is not None
    assert saved.status == "running"
    assert saved.voice_policy == "system"


def test_task_is_stalled_matches_timeout_error_codes() -> None:
    stalled = task("stalled", "failed")
    stalled.metadata = {"error_code": "TASK_TIMEOUT"}
    assert director_auto._task_is_stalled(stalled) is True

    stalled_with_nested = task("stalled-nested", "failed")
    stalled_with_nested.result = {"task_metadata": {"error_code": "TASK_STALLED"}}
    assert director_auto._task_is_stalled(stalled_with_nested) is True

    other = task("other", "failed")
    other.metadata = {"error_code": "BILLING_RULE_NOT_CONFIGURED"}
    assert director_auto._task_is_stalled(other) is False
    assert director_auto._task_is_stalled(task("plain", "failed")) is False


@pytest.mark.asyncio
async def test_stalled_task_suspends_run_for_user_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A task that stopped reporting progress (idle watchdog) must not abandon the
    # run outright: it is suspended so the user can choose to wait or stop.
    stalled = task("new-stalled", "failed")
    stalled.error = "任务已连续 30 分钟无进度更新，判定为卡死并暂停"
    stalled.metadata = {"error_code": "TASK_TIMEOUT", "timeout_seconds": 30 * 60}
    notified = asyncio.Event()

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [stalled]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []
            self.continuations = 0

        async def _notify(self, run, text):
            self.notifications.append(text)
            notified.set()
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False):
            self.continuations += 1
            return True

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    # The stall decision prompt is what this covers; the retry-first path is
    # exercised in test_stalled_task_is_retried_before_suspending.
    monkeypatch.setenv(director_auto.STAGE_RETRY_ENV, "0")
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path))
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        await asyncio.wait_for(notified.wait(), timeout=5)

        saved = store.get("alice", "project-1")
        assert saved is not None
        assert saved.status == "awaiting_confirmation"
        assert saved.last_error.startswith(director_auto.STALL_REASON_TAG)
        assert "无进度更新" in saved.last_error
        assert coordinator.continuations == 0
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_stalled_task_is_retried_before_suspending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stalled = task("new-stalled", "failed")
    stalled.error = "任务已连续 30 分钟无进度更新，判定为卡死并暂停"
    stalled.metadata = {"error_code": "TASK_TIMEOUT", "timeout_seconds": 30 * 60}
    notified = asyncio.Event()

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [stalled]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []

        async def _notify(self, run, text):
            self.notifications.append(text)
            notified.set()
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False):
            return True

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(director_auto, "STAGE_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path))
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        await asyncio.wait_for(notified.wait(), timeout=5)

        saved = store.get("alice", "project-1")
        assert saved is not None and saved.status == "running"
        assert "正在自动重试" in coordinator.notifications[0]
    finally:
        await coordinator.shutdown()


def test_store_running_without_lease_only_matches_free_or_expired_runs(
    tmp_path: Path,
) -> None:
    """巡检的查询面：只认「租约空闲」或「租约过期」的 running，paused 一律不算。"""

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path, run_id="free"))
    store.upsert(run_record(tmp_path, run_id="held", project_id="project-2"))
    store.upsert(run_record(tmp_path, run_id="expired", project_id="project-3"))
    store.upsert(
        run_record(tmp_path, run_id="paused", project_id="project-4", status="paused")
    )
    assert store.claim_lease("held", "worker-a", lease_seconds=200, now=1_000)
    assert store.claim_lease("expired", "worker-b", lease_seconds=60, now=1_000)

    stall_now = {run.run_id for run in store.running_without_lease(1_030)}
    stall_later = {run.run_id for run in store.running_without_lease(1_070)}

    assert stall_now == {"free"}
    assert stall_later == {"free", "expired"}


@pytest.mark.asyncio
async def test_lease_heartbeat_tolerates_transient_renewal_failures_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """单次续租失败（SQLite 写锁／线程池被堵）不再把 worker 静默取消。"""

    class FlakyStore:
        def __init__(self, failures: int) -> None:
            self.failures = failures
            self.calls = 0

        def renew_lease(self, run_id: str, owner_token: str) -> bool:
            self.calls += 1
            return self.calls > self.failures

    async def heartbeat_verdict(store) -> tuple[bool, int]:
        monkeypatch.setattr(director_auto, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(director_auto, "LEASE_RENEW_ATTEMPTS", 3)
        coordinator = DirectorAutoCoordinator(
            DirectorAutoStore(tmp_path / "director-auto.db")
        )
        coordinator.store = store  # type: ignore[assignment]
        parent = asyncio.create_task(asyncio.sleep(30))
        beat = asyncio.create_task(coordinator._lease_heartbeat("run-1", parent))
        try:
            await asyncio.sleep(0.15)
            coordinator._stopping = True
            await asyncio.wait_for(beat, timeout=5)
            # 让 parent.cancel() 真正落到任务上再判定
            await asyncio.sleep(0.01)
            return parent.cancelled(), store.calls
        finally:
            parent.cancel()
            await asyncio.gather(parent, return_exceptions=True)

    tolerant = FlakyStore(failures=2)
    with caplog.at_level("WARNING", logger=director_auto.logger.name):
        cancelled, calls = await heartbeat_verdict(tolerant)
    assert cancelled is False
    assert calls > 2
    assert any("lease renew failed" in record.message for record in caplog.records)

    dead = FlakyStore(failures=99)
    cancelled, _ = await heartbeat_verdict(dead)
    assert cancelled is True


@pytest.mark.asyncio
async def test_reaper_rearms_stalled_run_and_leaves_a_driven_run_alone(
    tmp_path: Path,
) -> None:
    """巡检把「running 但没人推进」的 run 重新 arm；有活 worker 的不碰。"""

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.armed: list[str] = []

        def _ensure_worker(self, run):
            self.armed.append(run.run_id)

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path, run_id="stalled"))
    store.upsert(run_record(tmp_path, run_id="driven", project_id="project-2"))
    store.upsert(
        run_record(tmp_path, run_id="paused", project_id="project-3", status="paused")
    )
    coordinator = RecordingCoordinator(store)
    driven = asyncio.create_task(asyncio.sleep(30))
    coordinator._workers["driven"] = driven
    try:
        await coordinator._reap_stalled_runs()
    finally:
        driven.cancel()
        await asyncio.gather(driven, return_exceptions=True)

    assert coordinator.armed == ["stalled"]


def test_store_paused_identity_gate_before_only_matches_stale_identity_pauses(
    tmp_path: Path,
) -> None:
    """巡检的查询面：只认「停在身份图闸门且已等超时」的 paused。

    角色肖像闸门（要不要交自己的图）、正在运行的 run、以及还没到宽限期的身份图
    暂停都不能被这条查询捞走——捞走就等于替客户做了本该他做的决定。
    """

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            run_id="stale",
            status="paused",
            updated_at="2026-08-14T00:00:00+00:00",
            last_error=director_auto.IDENTITY_IMAGES_REQUIRED_ERROR,
        )
    )
    store.upsert(
        run_record(
            tmp_path,
            run_id="fresh",
            project_id="project-2",
            status="paused",
            updated_at="2026-08-14T05:00:00+00:00",
            last_error=director_auto.IDENTITY_IMAGES_REQUIRED_ERROR,
        )
    )
    store.upsert(
        run_record(
            tmp_path,
            run_id="portraits",
            project_id="project-3",
            status="paused",
            updated_at="2026-08-14T00:00:00+00:00",
            last_error=director_auto.CHARACTER_IMAGES_REQUIRED_ERROR,
        )
    )
    store.upsert(
        run_record(
            tmp_path,
            run_id="running",
            project_id="project-4",
            last_error=director_auto.IDENTITY_IMAGES_REQUIRED_ERROR,
        )
    )

    stale = store.paused_identity_gate_before("2026-08-14T01:00:00+00:00")

    assert {run.run_id for run in stale} == {"stale"}


@pytest.mark.asyncio
async def test_stale_identity_gate_is_auto_answered_and_other_gates_stay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """身份图闸门是「不传就由 AI 生成」的一档：到点由服务端替客户答，绝不永久挂着。

    工作台开着时前端会先自动确认；这里覆盖「浏览器不在」的情况。角色肖像与报价
    闸门必须原样停着——那才是真需要客户点头的决定。
    """

    from novelvideo import project_config

    answered: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        project_config,
        "confirm_identity_images",
        lambda username, project, episode: answered.append(
            (username, project, episode)
        ),
    )

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store: DirectorAutoStore) -> None:
            super().__init__(store)
            self.resumed: list[tuple[str, str]] = []

        async def resume_paused(self, *, username, project_id, voice_policy=None):
            self.resumed.append((username, project_id))
            return None

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            run_id="stale",
            status="paused",
            updated_at="2026-08-14T00:00:00+00:00",
            last_error=director_auto.IDENTITY_IMAGES_REQUIRED_ERROR,
        )
    )
    store.upsert(
        run_record(
            tmp_path,
            run_id="fresh",
            project_id="project-2",
            status="paused",
            updated_at=director_auto.datetime.now(
                director_auto.timezone.utc
            ).isoformat(),
            last_error=director_auto.IDENTITY_IMAGES_REQUIRED_ERROR,
        )
    )
    store.upsert(
        run_record(
            tmp_path,
            run_id="portraits",
            project_id="project-3",
            status="paused",
            updated_at="2026-08-14T00:00:00+00:00",
            last_error=director_auto.CHARACTER_IMAGES_REQUIRED_ERROR,
        )
    )

    coordinator = RecordingCoordinator(store)
    await coordinator._auto_answer_stale_identity_gates()

    assert answered == [("alice", "demo", 1)]
    assert coordinator.resumed == [("alice", "project-1")]


@pytest.mark.asyncio
async def test_resume_starts_one_supervisor_and_shutdown_stops_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """巡检任务只能有一个，且必须随 shutdown 一起收掉。"""

    coordinator = DirectorAutoCoordinator(
        DirectorAutoStore(tmp_path / "director-auto.db")
    )
    monkeypatch.setattr(director_auto, "REAPER_SECONDS", 0.01)

    await coordinator.resume()
    first = coordinator._supervisor
    await coordinator.resume()

    assert first is not None
    assert coordinator._supervisor is first
    assert not first.done()

    await coordinator.shutdown()

    assert first.cancelled() is True


def _moderation_task(
    *,
    task_type: str = "single_video",
    beat_num: int | None = 12,
    provider_error: str = "InputTextSensitiveContentDetected",
) -> TaskState:
    """一个被上游内容审核拒绝的失败任务。"""

    failed = task("new-failed", "failed", task_type=task_type)
    failed.beat_num = beat_num
    failed.metadata = {
        "error_code": "CONTENT_MODERATION_FAILED",
        "message": "图片生成结果未通过内容审核，请调整提示词后重试",
        "provider_error": provider_error,
    }
    return failed


def test_moderation_kind_only_matches_content_moderation_failures() -> None:
    assert (
        director_auto._moderation_kind(_moderation_task())
        == "input_text"
    )
    assert (
        director_auto._moderation_kind(
            _moderation_task(provider_error="The output video may contain sensitive content")
        )
        == "output_video"
    )
    assert (
        director_auto._moderation_kind(
            _moderation_task(provider_error="InputImageSensitiveContentDetected.PolicyViolation")
        )
        == "input_image"
    )
    transient = task("plain", "failed", error="upstream rejected")
    assert director_auto._moderation_kind(transient) is None


def test_moderation_repair_target_needs_a_single_beat() -> None:
    run = run_record(Path("."))
    assert director_auto._moderation_repair_target(_moderation_task(), run) == (1, 12)
    # 批量 stage 的失败报文里没有「是哪一镜」的信息：不猜，交给调用方暂停。
    batch = _moderation_task(task_type="sketch_grid_generation", beat_num=None)
    assert director_auto._moderation_repair_target(batch, run) is None


@pytest.mark.asyncio
async def test_moderation_failure_rewrites_prompt_before_retrying(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """审核失败不许原样重投：先改提示词，改得出来才重投一次。"""

    failed = _moderation_task()
    notified = asyncio.Event()

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [failed]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []
            self.repairs: list[tuple[int, int, str]] = []

        async def _notify(self, run, text):
            self.notifications.append(text)
            notified.set()
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False):
            return True

        async def _repair_moderation_prompt(self, *, run, episode, beat_num, kind, raw_error=""):
            self.repairs.append((episode, beat_num, kind))
            return moderation_prompt_repair.PromptRepairOutcome(
                repaired=True,
                kind=kind,
                detail="已对提示词做确定性去敏",
            )

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(director_auto, "STAGE_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path))
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        await asyncio.wait_for(notified.wait(), timeout=5)

        assert coordinator.repairs == [(1, 12, "input_text")]
        saved = store.get("alice", "project-1")
        assert saved is not None and saved.status == "running"
        assert "moderation-prompt-repair:1:12" in saved.recovery_keys
        assert "已对提示词做确定性去敏" in coordinator.notifications[0]
        # 通用重试预算不能被这次改写花掉：它有自己的限次（一把 key）。
        assert saved.stage_attempts == 0
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_moderation_failure_degrades_and_continues_when_prompt_was_already_rewritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同一个 beat 只改一次词：第二次审核失败**不再停死整条 run**。

    工作台不允许假死：服务端登记该镜为 blocked、本地降级（这里没有首帧，降级落空
    但流程照走）、标记该镜的派发签名并继续推进。
    """

    failed = _moderation_task()
    notified = asyncio.Event()

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [failed]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []
            self.repairs = 0
            self.first_snapshot: DirectorAutoRun | None = None

        async def _notify(self, run, text):
            self.notifications.append(text)
            if self.first_snapshot is None:
                self.first_snapshot = self.store.get("alice", "project-1")
            notified.set()
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False):
            return True

        async def _repair_moderation_prompt(self, **_kwargs):
            self.repairs += 1
            return moderation_prompt_repair.PromptRepairOutcome(
                repaired=True, kind="input_text", detail="已对提示词做确定性去敏"
            )

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(tmp_path, recovery_keys=("moderation-prompt-repair:1:12",))
    )
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        await asyncio.wait_for(notified.wait(), timeout=5)

        saved = coordinator.first_snapshot
        assert saved is not None
        # 关键：不再停成 paused，run 继续往前走。
        assert saved.status == "running"
        assert coordinator.repairs == 0
        assert [shot["beat"] for shot in saved.blocked_shots] == [12]
        assert saved.blocked_shots[0]["code"] == "CONTENT_MODERATION_FAILED"
        # 测试环境没有首帧可降级，degraded 落空，但 blocked 仍登记、流程照走。
        assert saved.blocked_shots[0]["degraded"] is False
        assert "dispatch:single_video:1:12" in saved.recovery_keys
        assert "降级该镜" in coordinator.notifications[0]
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_moderation_failure_on_batch_stage_suspends_without_guessing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """定位不到具体镜头时不猜：不本地降级，但也不留一个会自动续跑的死循环。

    用 ``awaiting_confirmation``（需要用户决定）而不是 ``paused``——客户端不会把前者
    当硬暂停无限 ``/continue``。
    """

    failed = _moderation_task(task_type="sketch_grid_generation", beat_num=None)
    notified = asyncio.Event()

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [failed]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []
            self.repairs = 0

        async def _notify(self, run, text):
            self.notifications.append(text)
            notified.set()
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False):
            return True

        async def _repair_moderation_prompt(self, **_kwargs):
            self.repairs += 1
            return moderation_prompt_repair.PromptRepairOutcome(
                repaired=True, kind="input_text", detail="已对提示词做确定性去敏"
            )

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path))
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        # 30s：这条路径第一次会导入 api.deps（cognee）——测试环境要 8s 上下，
        # 5s 的老上限会因导入而不是逻辑超时。
        await asyncio.wait_for(notified.wait(), timeout=30)

        saved = store.get("alice", "project-1")
        assert saved is not None and saved.status == "awaiting_confirmation"
        assert coordinator.repairs == 0
        assert "无法定位到具体镜头" in coordinator.notifications[0]
        # 也没有偷偷走通用重试。
        assert saved.stage_attempts == 0
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_moderation_failure_on_complete_batch_stage_does_not_suspend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """产物已齐备的批量 stage 被上游拒（重复生成）不该把整条 run 停死。

    现场：工作台上手动重出某一格草图，5x5 里有一个 beat 触了上游内容策略，
    整个网格请求被拒。草图其实早就齐了，这次失败纯属多余的重渲——而批量 stage
    的失败报文没有镜号，旧逻辑会因此把 run 停成 ``awaiting_confirmation``
    （「无法定位到具体镜头」），整条自动流程就此卡住。
    """

    from novelvideo import pipeline_status

    failed = _moderation_task(task_type="sketch_grid_generation", beat_num=None)
    notified = asyncio.Event()

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [failed]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []

        async def _notify(self, run, text):
            self.notifications.append(text)
            notified.set()
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False):
            return True

    async def status_says_sketches_are_done(**_kwargs):
        return {"episode_status": {"sketches": True}}

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        pipeline_status, "compute_pipeline_status", status_says_sketches_are_done
    )
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path))
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        await asyncio.wait_for(notified.wait(), timeout=30)

        saved = store.get("alice", "project-1")
        assert saved is not None and saved.status == "running"
        assert "已忽略这次重复生成" in coordinator.notifications[0]
        assert saved.blocked_shots == ()
    finally:
        await coordinator.shutdown()


def test_stage_status_key_maps_runtime_task_types_to_status_keys() -> None:
    assert director_auto._stage_status_key("sketch_grid_generation") == "sketches"
    assert director_auto._stage_status_key("sketch_generation") == "sketches"
    assert director_auto._stage_status_key("selected_regen") == "first_frames"
    assert director_auto._stage_status_key("single_video") == "video"
    assert director_auto._stage_status_key("global_optimize_video") == "global_optimize"
    # 认不出来的类型不兜底。
    assert director_auto._stage_status_key("mystery_stage") is None
    assert director_auto._stage_status_key("") is None


@pytest.mark.asyncio
async def test_stage_artifacts_complete_reads_the_stage_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """兜底判断只认自己阶段的产物标志，取不到状态一律不兜底。"""

    from novelvideo import pipeline_status

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    coordinator = DirectorAutoCoordinator(store)
    run = run_record(tmp_path)

    async def status(**_kwargs):
        return {"episode_status": {"sketches": True, "first_frames": False}}

    monkeypatch.setattr(pipeline_status, "compute_pipeline_status", status)

    batch = _moderation_task(task_type="sketch_grid_generation", beat_num=None)
    assert await coordinator._stage_artifacts_complete(batch, run) is True
    # 同一份状态里 first_frames 还是 false：首帧阶段的失败照旧交回用户。
    frame_failed = _moderation_task(task_type="selected_regen")
    assert await coordinator._stage_artifacts_complete(frame_failed, run) is False
    # 状态里根本没有这一阶段（项目还没走到）也不兜底。
    video_failed = _moderation_task(task_type="single_video")
    assert await coordinator._stage_artifacts_complete(video_failed, run) is False


@pytest.mark.asyncio
async def test_every_failure_of_a_terminal_batch_is_recovered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """一批终态里同时躺着好几个失败时，每一个都要落地处理。

    旧实现把整批终态一次性标成 handled，却只处理 ``failures[0]``：同一批里的
    其它失败既不重试也不降级，而它们的派发签名已经记过一次，确定性派发再也不会
    重投——留下永远补不上的缺口（现场 1528/1533 就是这样丢的）。
    """

    second = _moderation_task(beat_num=13)
    second.task_id = "failed-13"
    first = _moderation_task(beat_num=12)
    first.task_id = "failed-12"
    handled_both = asyncio.Event()

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [second, first]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []

        async def _notify(self, run, text):
            self.notifications.append(text)
            if len(self.notifications) >= 2:
                handled_both.set()
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False):
            return True

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            recovery_keys=(
                "moderation-prompt-repair:1:12",
                "moderation-prompt-repair:1:13",
            ),
        )
    )
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        await asyncio.wait_for(handled_both.wait(), timeout=30)

        saved = store.get("alice", "project-1")
        assert saved is not None
        # 两个失败都降级落地了，不是只有第一个。
        assert sorted(shot["beat"] for shot in saved.blocked_shots) == [12, 13]
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_moderation_failure_degrades_when_prompt_cannot_be_rewritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """改写不出不同参数时也降级继续，绝不允许原样重投、也不停死。"""

    failed = _moderation_task(provider_error="Output video may contain sensitive content")
    notified = asyncio.Event()

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [failed]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []
            self.first_snapshot: DirectorAutoRun | None = None

        async def _notify(self, run, text):
            self.notifications.append(text)
            if self.first_snapshot is None:
                self.first_snapshot = self.store.get("alice", "project-1")
            notified.set()
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False):
            return True

        async def _repair_moderation_prompt(self, *, kind, **_kwargs):
            return moderation_prompt_repair.PromptRepairOutcome(
                repaired=False, kind=kind, detail="重新生成提示词失败"
            )

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(run_record(tmp_path))
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        await asyncio.wait_for(notified.wait(), timeout=5)

        saved = coordinator.first_snapshot
        assert saved is not None and saved.status == "running"
        assert [shot["beat"] for shot in saved.blocked_shots] == [12]
        assert "无法自动改写提示词" in coordinator.notifications[0]
        assert "重新生成提示词失败" in coordinator.notifications[0]
    finally:
        await coordinator.shutdown()


def test_fallback_kind_picks_the_local_degrade_ladder() -> None:
    assert director_auto._fallback_kind("single_video") == "video"
    assert director_auto._fallback_kind("selected_regen") == "frame"
    assert director_auto._fallback_kind("identity_image") == "none"
    assert director_auto._fallback_kind("sketch_grid_generation") == "none"


@pytest.mark.asyncio
async def test_retry_exhausted_video_shot_degrades_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """重试预算用尽时，可降级的单镜**不再停死整条 run**，而是降级该镜并继续。"""

    failed = task("new-failed", "failed", task_type="single_video")
    failed.beat_num = 12
    failed.error = "upstream rejected"

    class Manager:
        def list_tasks_for_project(self, _ctx):
            return [failed]

    class RecordingCoordinator(DirectorAutoCoordinator):
        def __init__(self, store):
            super().__init__(store)
            self.notifications: list[str] = []
            self.blocked_snapshot: DirectorAutoRun | None = None

        async def _notify(self, run, text):
            self.notifications.append(text)
            saved = self.store.get("alice", "project-1")
            if saved is not None and saved.blocked_shots and self.blocked_snapshot is None:
                self.blocked_snapshot = saved
            return {"content": text}

        async def _broadcast_status(self, run, **_kwargs):
            return None

        async def _agent_continue(self, run, *, final_delivery=False):
            return True

    monkeypatch.setattr(director_auto, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(director_auto, "POLL_SECONDS", 0.01)
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    # 该 stage 的重试预算已用尽：这次失败直接进入「降级 or 停」分支。
    store.upsert(
        run_record(
            tmp_path,
            stage_key="single_video",
            stage_attempts=director_auto.MAX_STAGE_ATTEMPTS,
        )
    )
    coordinator = RecordingCoordinator(store)

    try:
        coordinator._ensure_worker(store.get("alice", "project-1"))  # type: ignore[arg-type]
        for _ in range(600):
            if coordinator.blocked_snapshot is not None:
                break
            await asyncio.sleep(0.01)

        saved = coordinator.blocked_snapshot
        assert saved is not None, coordinator.notifications
        assert saved.status == "running"
        assert [shot["beat"] for shot in saved.blocked_shots] == [12]
        assert saved.blocked_shots[0]["code"] == "retries_exhausted"
        assert "dispatch:single_video:1:12" in saved.recovery_keys
    finally:
        await coordinator.shutdown()


def test_degrade_first_frame_copies_the_sketch_when_available(tmp_path: Path) -> None:
    frame = tmp_path / "frames" / "ep001" / "beat_12.png"
    sketch = tmp_path / "sketches" / "ep001" / "beat_12.png"
    sketch.parent.mkdir(parents=True, exist_ok=True)
    sketch.write_bytes(b"sketch-bytes")

    assert DirectorAutoCoordinator._degrade_first_frame(frame, sketch) is True
    assert frame.read_bytes() == b"sketch-bytes"
    # 已有首帧就不覆盖（降级只兜底缺失）。
    frame.write_bytes(b"real-frame")
    assert DirectorAutoCoordinator._degrade_first_frame(frame, sketch) is False
    assert frame.read_bytes() == b"real-frame"
    # 连草图都没有就没得降级。
    assert (
        DirectorAutoCoordinator._degrade_first_frame(
            tmp_path / "frames" / "ep001" / "beat_99.png",
            tmp_path / "sketches" / "ep001" / "beat_99.png",
        )
        is False
    )


@pytest.mark.asyncio
async def test_clear_blocked_shot_drops_only_the_named_entry(tmp_path: Path) -> None:
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            blocked_shots=(
                {"episode": 1, "beat": 12, "stage": "single_video", "degraded": True},
                {"episode": 1, "beat": 13, "stage": "single_video", "degraded": True},
            ),
        )
    )
    coordinator = DirectorAutoCoordinator(store)
    try:
        updated = await coordinator.clear_blocked_shot(
            username="alice", project_id="project-1", episode=1, beat_num=12
        )
        assert updated is not None
        assert [shot["beat"] for shot in updated.blocked_shots] == [13]
        persisted = store.get("alice", "project-1")
        assert persisted is not None
        assert [shot["beat"] for shot in persisted.blocked_shots] == [13]
        # 不存在的镜号原样返回，不报错。
        same = await coordinator.clear_blocked_shot(
            username="alice", project_id="project-1", episode=1, beat_num=99
        )
        assert same is not None and len(same.blocked_shots) == 1
    finally:
        await coordinator.shutdown()


def test_pause_kind_for_reason_maps_each_family() -> None:
    classify = director_auto._pause_kind_for_reason

    assert classify(director_auto.CHARACTER_IMAGES_REQUIRED_ERROR) == director_auto.PAUSE_KIND_GATE
    assert classify(director_auto.IDENTITY_IMAGES_REQUIRED_ERROR) == director_auto.PAUSE_KIND_GATE
    assert classify(director_auto.COST_ESTIMATE_REQUIRED_ERROR) == director_auto.PAUSE_KIND_GATE
    assert classify(director_auto.VOICE_POLICY_REQUIRED_ERROR) == director_auto.PAUSE_KIND_VOICE
    assert (
        classify("项目配置缺失（如视觉风格、叙事方式等），请先完成配置")
        == director_auto.PAUSE_KIND_NEEDS_CONFIG
    )
    assert classify("自动续跑未产生写任务") == director_auto.PAUSE_KIND_BLOCKED
    assert (
        classify("自动续跑未产生新的任务（最后一次工具调用：x）")
        == director_auto.PAUSE_KIND_BLOCKED
    )
    assert classify("项目权限校验失败：boom") == director_auto.PAUSE_KIND_FATAL
    assert classify("用户切换为手动模式") == director_auto.PAUSE_KIND_STOPPED
    assert classify("生成失败") == director_auto.PAUSE_KIND_RETRYABLE


def test_store_round_trips_pause_kind_and_recovery_attempts(tmp_path: Path) -> None:
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    run = run_record(
        tmp_path,
        status="paused",
        last_error="生成失败",
        pause_kind=director_auto.PAUSE_KIND_RETRYABLE,
        recovery_attempts=3,
    )
    store.upsert(run)

    loaded = store.get("alice", "project-1")
    assert loaded == run
    assert loaded is not None
    assert loaded.pause_kind == director_auto.PAUSE_KIND_RETRYABLE
    assert loaded.recovery_attempts == 3


def test_store_paused_kind_before_matches_kind_and_grace(tmp_path: Path) -> None:
    """兜底续跑的查询面：只认「停在该 kind 且已等超时」的 paused。"""

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    old = "2026-08-14T00:00:00+00:00"
    store.upsert(
        run_record(
            tmp_path,
            run_id="retry",
            status="paused",
            pause_kind=director_auto.PAUSE_KIND_RETRYABLE,
            updated_at=old,
        )
    )
    store.upsert(
        run_record(
            tmp_path,
            run_id="gate",
            project_id="project-2",
            status="paused",
            pause_kind=director_auto.PAUSE_KIND_GATE,
            updated_at=old,
        )
    )
    store.upsert(
        run_record(
            tmp_path,
            run_id="running",
            project_id="project-3",
            pause_kind=director_auto.PAUSE_KIND_RETRYABLE,
            updated_at=old,
        )
    )

    found = store.paused_kind_before(
        director_auto.PAUSE_KIND_RETRYABLE, "2026-08-14T01:00:00+00:00"
    )
    assert {run.run_id for run in found} == {"retry"}


class _StubCoordinator(DirectorAutoCoordinator):
    """记录 ``_ensure_worker`` / ``_broadcast_status``，避免真的起 worker。"""

    def __init__(self, store: DirectorAutoStore) -> None:
        super().__init__(store)
        self.started: list[str] = []
        self.resumed: list[tuple[str, bool]] = []

    def _ensure_worker(self, run: DirectorAutoRun) -> None:
        self.started.append(run.run_id)

    async def _broadcast_status(self, run: DirectorAutoRun, **_kwargs) -> None:
        return None


class _AutoResumeRecorder(_StubCoordinator):
    """把兜底续跑记录下来（不真的唤醒 worker）。"""

    async def resume_paused(
        self,
        *,
        username: str,
        project_id: str,
        voice_policy: str | None = None,
        auto: bool = False,
    ) -> DirectorAutoRun | None:
        self.resumed.append((project_id, auto))
        return None



@pytest.mark.asyncio
async def test_resume_paused_auto_keeps_budget_manual_resets(tmp_path: Path) -> None:
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            status="paused",
            last_error="生成失败",
            pause_kind=director_auto.PAUSE_KIND_RETRYABLE,
            recovery_attempts=2,
        )
    )
    coordinator = _StubCoordinator(store)

    resumed = await coordinator.resume_paused(
        username="alice", project_id="project-1", auto=True
    )
    assert resumed is not None
    assert resumed.recovery_attempts == 3
    assert resumed.pause_kind == ""

    # 人工点击清零预算（人已介入）。
    store.upsert(
        run_record(
            tmp_path,
            status="paused",
            last_error="生成失败",
            pause_kind=director_auto.PAUSE_KIND_RETRYABLE,
            recovery_attempts=4,
        )
    )
    manual = await coordinator.resume_paused(
        username="alice", project_id="project-1", auto=False
    )
    assert manual is not None
    assert manual.recovery_attempts == 0


@pytest.mark.asyncio
async def test_resume_paused_auto_refuses_when_budget_exhausted(tmp_path: Path) -> None:
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            status="paused",
            last_error="生成失败",
            pause_kind=director_auto.PAUSE_KIND_RETRYABLE,
            recovery_attempts=director_auto.MAX_RECOVERY_ATTEMPTS,
        )
    )
    coordinator = _StubCoordinator(store)

    with pytest.raises(ValueError, match="自动恢复次数已用尽"):
        await coordinator.resume_paused(
            username="alice", project_id="project-1", auto=True
        )


@pytest.mark.asyncio
async def test_auto_resume_stale_hard_pauses_resumes_only_retryable(
    tmp_path: Path,
) -> None:
    """浏览器不在时，可重试的硬暂停由服务端兜底续跑；闸门/阻塞/配置/致命不碰。"""

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    old = "2026-08-14T00:00:00+00:00"
    cases = [
        ("retry", "project-1", director_auto.PAUSE_KIND_RETRYABLE),
        ("gate", "project-2", director_auto.PAUSE_KIND_GATE),
        ("blocked", "project-3", director_auto.PAUSE_KIND_BLOCKED),
        ("config", "project-4", director_auto.PAUSE_KIND_NEEDS_CONFIG),
        ("fatal", "project-5", director_auto.PAUSE_KIND_FATAL),
        ("stopped", "project-6", director_auto.PAUSE_KIND_STOPPED),
    ]
    for run_id, project_id, kind in cases:
        store.upsert(
            run_record(
                tmp_path,
                run_id=run_id,
                project_id=project_id,
                status="paused",
                pause_kind=kind,
                updated_at=old,
            )
        )
    # 还没到宽限期的 retryable 不动。
    store.upsert(
        run_record(
            tmp_path,
            run_id="fresh",
            project_id="project-7",
            status="paused",
            pause_kind=director_auto.PAUSE_KIND_RETRYABLE,
            updated_at=director_auto.datetime.now(
                director_auto.timezone.utc
            ).isoformat(),
        )
    )

    coordinator = _AutoResumeRecorder(store)
    await coordinator._auto_resume_stale_hard_pauses()

    assert coordinator.resumed == [("project-1", True)]

@pytest.mark.asyncio
async def test_auto_resume_marks_needs_human_when_budget_spent(tmp_path: Path) -> None:
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            status="paused",
            pause_kind=director_auto.PAUSE_KIND_RETRYABLE,
            recovery_attempts=director_auto.MAX_RECOVERY_ATTEMPTS,
            updated_at="2026-08-14T00:00:00+00:00",
        )
    )
    coordinator = _StubCoordinator(store)

    await coordinator._auto_resume_stale_hard_pauses()

    assert coordinator.resumed == []
    loaded = store.get("alice", "project-1")
    assert loaded is not None
    assert loaded.pause_kind == director_auto.PAUSE_KIND_NEEDS_HUMAN


@pytest.mark.asyncio
async def test_reap_unprogressing_run_rearms_live_worker(tmp_path: Path) -> None:
    """租约还在、心跳还在续，但没有任何进展且无任务在飞 → 重拉一次（有界）。"""

    class C(_StubCoordinator):
        async def _has_active_task(self, run: DirectorAutoRun) -> bool:
            return False

    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        run_record(
            tmp_path,
            status="running",
            updated_at="2026-08-14T00:00:00+00:00",
        )
    )
    coordinator = C(store)

    async def _never() -> None:
        await asyncio.sleep(3600)

    worker = asyncio.create_task(_never())
    coordinator._workers["run-1"] = worker
    try:
        await coordinator._reap_unprogressing_runs()
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker

    loaded = store.get("alice", "project-1")
    assert loaded is not None
    assert loaded.recovery_attempts == 1
    assert coordinator.started == ["run-1"]




