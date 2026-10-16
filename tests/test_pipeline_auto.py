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
async def test_dispatch_seedance2_prompt_picks_the_first_beat_without_a_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.seedance2_i2v.models import dump_seedance2_config

    patch_status(monkeypatch, "seedance2_prompt", episode=1)
    calls = capture_enqueue(monkeypatch)

    class Store:
        async def get_beats_as_dicts(self, episode: int):
            assert episode == 1
            return [
                {
                    "beat_number": 1,
                    "seedance2_config_json": dump_seedance2_config(
                        {"final_prompt": "已有"}
                    ),
                },
                {"beat_number": 2, "seedance2_config_json": ""},
                {
                    "beat_number": 3,
                    "seedance2_config_json": dump_seedance2_config(
                        {"final_prompt": "也有"}
                    ),
                },
            ]

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=Store(), episode=1
    )

    assert task_type == "seedance2_prompt"
    assert signature == "dispatch:seedance2_prompt:1:2"
    assert calls[0]["queue_kind"] == "default"
    assert calls[0]["beat_num"] == 2
    assert calls[0]["payload"]["beat_num"] == 2
    assert calls[0]["payload"]["episode"] == 1


@pytest.mark.asyncio
async def test_dispatch_seedance2_prompt_is_complete_when_all_beats_have_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.seedance2_i2v.models import dump_seedance2_config

    patch_status(monkeypatch, "seedance2_prompt", episode=1)
    calls = capture_enqueue(monkeypatch)
    beats = [
        {
            "beat_number": number,
            "seedance2_config_json": dump_seedance2_config({"final_prompt": "有"}),
        }
        for number in (1, 2)
    ]

    class Store:
        async def get_beats_as_dicts(self, _episode: int):
            return beats

    result = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=Store(), episode=1
    )

    assert result == (None, None)
    assert calls == []


def _write_video_inputs(tmp_path: Path, beat_numbers: tuple[int, ...]) -> None:
    frames = tmp_path / "frames" / "ep001"
    frames.mkdir(parents=True, exist_ok=True)
    for beat_num in beat_numbers:
        (frames / f"beat_{beat_num:02d}.png").write_bytes(b"x")


def _video_store(beat_numbers: tuple[int, ...]):
    class Store:
        async def get_beats_as_dicts(self, _episode: int):
            return [{"beat_number": number} for number in beat_numbers]

    return Store()


def _capture_single_video_enqueues(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    from novelvideo.api.routes import generation

    calls: list[dict] = []

    async def fake_enqueue_single_video_task(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "task_type": "single_video"}

    monkeypatch.setattr(generation, "enqueue_single_video_task", fake_enqueue_single_video_task)
    return calls


@pytest.mark.asyncio
async def test_dispatch_single_videos_only_sends_ready_beats(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A beat without a first frame cannot render, so the agent gets that step."""

    patch_status(monkeypatch, "single_video", episode=1)
    calls = _capture_single_video_enqueues(monkeypatch)
    _write_video_inputs(tmp_path, (1, 2, 3))
    videos = tmp_path / "videos" / "beats" / "ep001"
    videos.mkdir(parents=True)
    (videos / "beat_02.mp4").write_bytes(b"x")

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path),
        username="alice",
        store=_video_store((1, 2, 3, 4)),
        episode=1,
    )

    assert task_type == "single_video"
    assert signature == "dispatch:single_video:1:1-3"
    assert [call["beat_num"] for call in calls] == [1, 3]
    assert {call["episode_num"] for call in calls} == {1}


@pytest.mark.asyncio
async def test_dispatch_single_videos_caps_the_chunk_at_the_runtime_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_status(monkeypatch, "single_video", episode=1)
    calls = _capture_single_video_enqueues(monkeypatch)
    beat_numbers = tuple(range(1, 13))
    _write_video_inputs(tmp_path, beat_numbers)

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path),
        username="alice",
        store=_video_store(beat_numbers),
        episode=1,
    )

    assert task_type == "single_video"
    assert signature == "dispatch:single_video:1:1-9"
    assert [call["beat_num"] for call in calls] == list(range(1, 10))


@pytest.mark.asyncio
async def test_dispatch_single_videos_does_not_repeat_a_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_status(monkeypatch, "single_video", episode=1)
    calls = _capture_single_video_enqueues(monkeypatch)
    _write_video_inputs(tmp_path, (1, 2))

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path),
        username="alice",
        store=_video_store((1, 2)),
        episode=1,
        attempted={"dispatch:single_video:1:1-2"},
    )

    assert task_type is None
    assert signature == "dispatch:single_video:1:1-2"
    assert calls == []


@pytest.mark.asyncio
async def test_dispatch_single_videos_without_ready_beats_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_status(monkeypatch, "single_video", episode=1)
    calls = _capture_single_video_enqueues(monkeypatch)

    result = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path),
        username="alice",
        store=_video_store((1, 2)),
        episode=1,
    )

    assert result == (None, None)
    assert calls == []


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
