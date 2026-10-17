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
async def test_dispatch_script_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_status(monkeypatch, "script_process")
    calls = capture_enqueue(monkeypatch)

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=object(), episode=0
    )

    assert task_type == "script_process"
    assert signature == "dispatch:script_process"
    assert calls[0]["task_type"] == "script_process"
    assert calls[0]["episode"] == 0
    assert calls[0]["payload"] == {"output_dir": str(tmp_path)}


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
    # ``coloring`` (sketch identity/prop detection) is the deliberate remaining
    # agent-driven stage: it is a synchronous vision call with no task type.
    patch_status(monkeypatch, "coloring")
    capture_enqueue(monkeypatch)

    result = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=object(), episode=1
    )

    assert result == (None, None)


@pytest.mark.asyncio
async def test_dispatch_script_writer_uses_the_route_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.api.routes import scripts as scripts_module

    patch_status(monkeypatch, "script_writer", episode=2)
    calls: list[dict] = []

    async def fake_enqueue_script_writer_task(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "task_type": "script_writer"}

    monkeypatch.setattr(scripts_module, "enqueue_script_writer_task", fake_enqueue_script_writer_task)

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=object(), episode=2
    )

    assert task_type == "script_writer"
    assert signature == "dispatch:script_writer:2"
    assert calls[0]["episode_num"] == 2
    assert calls[0]["output_dir"] == str(tmp_path)


@pytest.mark.asyncio
async def test_dispatch_script_writer_prerequisite_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing identity plan must not be recorded as a dispatched stage."""

    from novelvideo.api.routes import scripts as scripts_module

    patch_status(monkeypatch, "script_writer", episode=2)

    async def fake_enqueue_script_writer_task(**_kwargs):
        return {"ok": False, "code": "identity_plan_required"}

    monkeypatch.setattr(scripts_module, "enqueue_script_writer_task", fake_enqueue_script_writer_task)

    result = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=object(), episode=2
    )

    assert result == (None, None)


@pytest.mark.asyncio
async def test_dispatch_sketches_requests_every_grid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.api.routes import generation

    patch_status(monkeypatch, "sketch_generation", episode=1)
    calls: list[dict] = []

    async def fake_enqueue_sketch_grid_tasks(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "task_type": "sketch_grid_generation"}

    monkeypatch.setattr(generation, "enqueue_sketch_grid_tasks", fake_enqueue_sketch_grid_tasks)

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=object(), episode=1
    )

    assert task_type == "sketch_grid_generation"
    assert signature == "dispatch:sketch_generation:1"
    # grid_index=-1 is "all grids"; the schema default (0) would silently build
    # only the first grid.
    assert calls[0]["body"].grid_index == -1
    # The auto path must assign colours itself: sketch generation refuses to
    # start when none are assigned, which is exactly what once sent an agent
    # down the wrong (Render) path.
    assert calls[0]["ensure_colors"] is True
    assert calls[0]["username"] == "alice"
    assert calls[0]["output_dir"] == str(tmp_path)


@pytest.mark.asyncio
async def test_dispatch_global_optimize_uses_the_route_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.api.routes import generation

    patch_status(monkeypatch, "global_optimize_video", episode=4)
    calls: list[dict] = []

    async def fake_enqueue_global_optimize_video_task(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "task_type": "global_optimize_video"}

    monkeypatch.setattr(
        generation, "enqueue_global_optimize_video_task", fake_enqueue_global_optimize_video_task
    )

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=object(), episode=4
    )

    assert task_type == "global_optimize_video"
    assert signature == "dispatch:global_optimize_video:4"
    assert calls[0]["episode_num"] == 4
    assert calls[0]["output_dir"] == str(tmp_path)


def _write_sketches(tmp_path: Path, beat_numbers: tuple[int, ...]) -> None:
    sketches = tmp_path / "sketches" / "ep001"
    sketches.mkdir(parents=True, exist_ok=True)
    for beat_num in beat_numbers:
        (sketches / f"beat_{beat_num:02d}.png").write_bytes(b"x")


def _capture_selected_regen_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict], list[dict]]:
    from novelvideo.api.routes import generation

    calls: list[dict] = []
    mode_calls: list[dict] = []

    async def fake_enqueue_selected_regen_task(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "task_type": "selected_regen"}

    def fake_single_render_mode(output_dir, episode, beat_indices):
        mode_calls.append(
            {
                "output_dir": output_dir,
                "episode": episode,
                "beat_indices": list(beat_indices),
            }
        )
        return "1x1_16-9"

    monkeypatch.setattr(generation, "enqueue_selected_regen_task", fake_enqueue_selected_regen_task)
    monkeypatch.setattr(generation, "_single_render_mode_from_sketch", fake_single_render_mode)
    return calls, mode_calls


@pytest.mark.asyncio
async def test_dispatch_first_frames_only_sends_sketched_beats_without_a_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_status(monkeypatch, "selected_regen", episode=1)
    calls, _ = _capture_selected_regen_enqueues(monkeypatch)
    _write_sketches(tmp_path, (1, 2, 3))
    _write_video_inputs(tmp_path, (2,))

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path),
        username="alice",
        store=_video_store((1, 2, 3)),
        episode=1,
    )

    assert task_type == "selected_regen"
    assert signature == "dispatch:selected_regen:1:1-3"
    assert calls[0]["beat_indices"] == [1, 3]
    assert calls[0]["episode_num"] == 1


@pytest.mark.asyncio
async def test_dispatch_first_frames_uses_the_sketch_aspect_for_the_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 16:9 sketch must not be rendered with the 2:3 client default."""

    patch_status(monkeypatch, "selected_regen", episode=1)
    calls, mode_calls = _capture_selected_regen_enqueues(monkeypatch)
    _write_sketches(tmp_path, (2, 3))

    await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=_video_store((2, 3)), episode=1
    )

    assert calls[0]["mode_key"] == "1x1_16-9"
    # The mode is derived from the batch's own first beat, not from the client.
    assert mode_calls == [
        {"output_dir": str(tmp_path), "episode": 1, "beat_indices": [2]},
    ]


@pytest.mark.asyncio
async def test_dispatch_first_frames_caps_the_chunk_below_the_runtime_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_status(monkeypatch, "selected_regen", episode=1)
    calls, _ = _capture_selected_regen_enqueues(monkeypatch)
    beat_numbers = tuple(range(1, 13))
    _write_sketches(tmp_path, beat_numbers)

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path),
        username="alice",
        store=_video_store(beat_numbers),
        episode=1,
    )

    assert pipeline_auto.AUTO_FIRST_FRAME_CHUNK < pipeline_auto.MAX_SINGLE_VIDEO_BATCH
    assert task_type == "selected_regen"
    assert signature == "dispatch:selected_regen:1:1-3"
    assert calls[0]["beat_indices"] == list(range(1, 4))


@pytest.mark.asyncio
async def test_dispatch_first_frames_does_not_repeat_a_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_status(monkeypatch, "selected_regen", episode=1)
    calls, _ = _capture_selected_regen_enqueues(monkeypatch)
    _write_sketches(tmp_path, (1, 2))

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path),
        username="alice",
        store=_video_store((1, 2)),
        episode=1,
        attempted={"dispatch:selected_regen:1:1-2"},
    )

    assert task_type is None
    assert signature == "dispatch:selected_regen:1:1-2"
    assert calls == []


@pytest.mark.asyncio
async def test_dispatch_tts_uses_the_route_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.api.routes import generation

    patch_status(monkeypatch, "tts", episode=3)
    calls: list[dict] = []

    async def fake_enqueue_audio_generation_task(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "task_type": "audio_generation_indextts2"}

    monkeypatch.setattr(
        generation, "enqueue_audio_generation_task", fake_enqueue_audio_generation_task
    )

    task_type, signature = await pipeline_auto.dispatch_next_stage(
        ctx=ctx(tmp_path), username="alice", store=object(), episode=3
    )

    assert task_type == "audio_generation_indextts2"
    assert signature == "dispatch:audio_generation:3"
    assert calls[0]["episode_num"] == 3
    assert calls[0]["state_dir"] == str(tmp_path / "state")


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
