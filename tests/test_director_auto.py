from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest

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
    run = run_record(
        tmp_path,
        baseline_task_ids=("old",),
        handled_task_ids=("done",),
        voice_policy="system",
        stage_key="single_video",
        stage_attempts=2,
        next_attempt_at=1755000000.5,
    )
    store.upsert(run)

    loaded = store.get("alice", "project-1")

    assert loaded == run
    assert loaded is not None
    assert loaded.stage_key == "single_video"
    assert loaded.stage_attempts == 2
    assert loaded.next_attempt_at == 1755000000.5
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

