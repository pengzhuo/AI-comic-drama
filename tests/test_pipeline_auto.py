from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo import pipeline_auto


def ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        output_dir=tmp_path,
        state_dir=tmp_path / "state",
        project_name="demo",
        requester_username="alice",
    )


def patch_status(monkeypatch: pytest.MonkeyPatch, next_step: str, episode: int = 1) -> None:
    async def fake_status(**_kwargs):
        return {"next_step": next_step, "current_episode": episode}

    monkeypatch.setattr(pipeline_auto, "compute_pipeline_status", fake_status)


def capture_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_enqueue(ctx, **kwargs):
        calls.append({"ctx": ctx, **kwargs})
        return SimpleNamespace(task_state=SimpleNamespace(task_id="t"))

    monkeypatch.setattr(pipeline_auto, "enqueue_project_task", fake_enqueue)
    return calls


@pytest.mark.asyncio
async def test_dispatch_build_characters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_status(monkeypatch, "build_characters")
    calls = capture_enqueue(monkeypatch)

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=object(), episode=0
    )

    assert task_type == "build_characters"
    assert signature == "dispatch:build_characters"
    assert calls[0]["task_type"] == "build_characters"
    assert calls[0]["episode"] == 0
    assert calls[0]["payload"] == {"output_dir": str(tmp_path)}


@pytest.mark.asyncio
async def test_dispatch_build_episodes_uses_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_status(monkeypatch, "build_episodes")
    calls = capture_enqueue(monkeypatch)

    task_type, _ = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=object(), episode=0, target_episodes=7
    )

    assert task_type == "build_episodes"
    assert calls[0]["payload"]["config"]["target_episodes"] == 7
    assert calls[0]["payload"]["config"]["planning_mode"] == "chapters"


@pytest.mark.asyncio
async def test_dispatch_identity_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_status(monkeypatch, "identity_plan", episode=3)
    calls = capture_enqueue(monkeypatch)

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=object(), episode=3
    )

    assert task_type == "identity_planner"
    assert signature == "dispatch:identity_planner:3"
    assert calls[0]["episode"] == 3
    assert calls[0]["payload"] == {"episode": 3}


@pytest.mark.asyncio
async def test_dispatch_compose_needs_beats(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_status(monkeypatch, "compose_episode", episode=2)
    calls = capture_enqueue(monkeypatch)

    class Store:
        async def get_beats_as_dicts(self, episode: int):
            assert episode == 2
            return [{"beat_number": 1}]

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=Store(), episode=2
    )

    assert task_type == "compose_episode"
    assert signature == "dispatch:compose_episode:2"
    assert calls[0]["queue_kind"] == "ffmpeg"
    assert calls[0]["payload"]["beats"] == [{"beat_number": 1}]


@pytest.mark.asyncio
async def test_dispatch_compose_without_beats_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_status(monkeypatch, "compose_episode", episode=2)
    capture_enqueue(monkeypatch)

    class Store:
        async def get_beats_as_dicts(self, _episode: int):
            return []

    result = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=Store(), episode=2
    )

    assert result == (None, None)


@pytest.mark.asyncio
async def test_unsupported_step_falls_back_to_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_status(monkeypatch, "script_writer")
    capture_enqueue(monkeypatch)

    result = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=object(), episode=1
    )

    assert result == (None, None)


@pytest.mark.asyncio
async def test_already_attempted_signature_is_not_redispatched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_status(monkeypatch, "build_characters")
    calls = capture_enqueue(monkeypatch)

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path),
        username="alice",
        store=object(),
        episode=0,
        attempted={"dispatch:build_characters"},
    )

    assert task_type is None
    assert signature == "dispatch:build_characters"
    assert calls == []


@pytest.mark.asyncio
async def test_dispatch_portrait_picks_first_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_status(monkeypatch, "portraits")
    calls = capture_enqueue(monkeypatch)
    monkeypatch.setattr(
        pipeline_auto,
        "compute_portrait_path",
        lambda _dir, name: tmp_path / f"{name}.png" if name == "Done" else None,
    )
    monkeypatch.setattr(
        pipeline_auto,
        "_character_image_payload_bits",
        lambda *_a, **_k: ("anime", {"style": "anime", "model": "m", "billing": {}}),
    )

    class Store:
        def get_all_characters(self):
            return [
                SimpleNamespace(name="Done", is_main=True, face_prompt="face", identities=[]),
                SimpleNamespace(name="Bob", is_main=True, face_prompt="face", identities=[]),
            ]

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=Store(), episode=1
    )

    assert task_type == "character_portrait"
    assert signature == "dispatch:character_portrait:Bob"
    assert calls[0]["payload"]["character_name"] == "Bob"
    assert calls[0]["scope"] == "character:Bob:portrait"


@pytest.mark.asyncio
async def test_dispatch_portrait_covers_non_main_characters_with_identities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Identity images reuse the character portrait, so non-main characters that
    own identities must also get one; extras without a face prompt are skipped."""
    patch_status(monkeypatch, "portraits")
    calls = capture_enqueue(monkeypatch)
    monkeypatch.setattr(
        pipeline_auto,
        "compute_portrait_path",
        lambda _dir, name: tmp_path / f"{name}.png" if name == "Lead" else None,
    )
    monkeypatch.setattr(
        pipeline_auto,
        "_character_image_payload_bits",
        lambda *_a, **_k: ("anime", {"style": "anime", "model": "m", "billing": {}}),
    )

    class Store:
        def get_all_characters(self):
            return [
                SimpleNamespace(name="Lead", is_main=True, face_prompt="face", identities=[]),
                SimpleNamespace(name="Extra", is_main=False, face_prompt="", identities=[]),
                SimpleNamespace(
                    name="Villager",
                    is_main=False,
                    face_prompt="face",
                    identities=[SimpleNamespace(identity_id="Villager_中年时期")],
                ),
            ]

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=Store(), episode=1
    )

    assert task_type == "character_portrait"
    assert signature == "dispatch:character_portrait:Villager"
    assert calls[0]["payload"]["character_name"] == "Villager"
