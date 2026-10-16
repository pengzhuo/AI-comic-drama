"""Omitting ``video_backend`` must follow the project's configured default.

A frozen request-level default made agent/batch callers request a model the
configured gateway does not serve, so the route has to fall back to the
project's effective ``video_backend`` (which itself defaults to the instance's
``VIDEO_BACKEND``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class _Store:
    def __init__(self, beats):
        self.beats = beats
        self.state_dir = "/state/alice/demo"

    async def get_beats_as_dicts(self, episode: int):
        assert episode == 3
        return self.beats

    async def update_beat_asset(self, **kwargs):
        return True


def _patch_project(monkeypatch, generation, tmp_path, store):
    ctx = SimpleNamespace(project_id="proj-1", state_dir=tmp_path / "state")

    async def fake_resolve_generation_project(
        project_arg, user, required_role="editor"
    ):
        return SimpleNamespace(
            ctx=ctx,
            username="alice",
            project_name="demo",
            project_dir=tmp_path,
            output_dir=str(tmp_path),
            state_dir=str(tmp_path / "state"),
            runtime_dir=str(tmp_path / "runtime"),
        )

    async def fake_make_sqlite_store_for_context(ctx_arg):
        assert ctx_arg is ctx
        return store

    monkeypatch.setattr(
        generation, "_resolve_generation_project", fake_resolve_generation_project
    )
    monkeypatch.setattr(
        generation, "make_sqlite_store_for_context", fake_make_sqlite_store_for_context
    )
    return ctx


def _capture_enqueue(monkeypatch, generation):
    calls = []

    async def fake_enqueue_project_task(
        ctx, *, task_type, queue_kind, episode, payload, **extra
    ):
        calls.append({"task_type": task_type, "payload": payload, **extra})
        return SimpleNamespace(
            task_state=SimpleNamespace(task_id="task-1"),
            backend="celery",
            queue="default",
        )

    monkeypatch.setattr(
        generation,
        "get_task_backend",
        lambda: SimpleNamespace(enqueue_project_task=fake_enqueue_project_task),
    )
    return calls


def _write_frame(tmp_path):
    frame = tmp_path / "frames" / "ep003" / "beat_02.png"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"frame")
    return frame


def _stub_single_video_deps(monkeypatch, generation, frame):
    from novelvideo.seedance2_i2v.models import Seedance2I2VMode

    async def fake_prepare(**_kwargs):
        return SimpleNamespace(
            prompt="configured prompt",
            seedance2_config_json='{"final_prompt": "configured prompt"}',
            duration=5,
            mode=Seedance2I2VMode.FIRST_FRAME,
            image_path=str(frame),
            last_frame_path=None,
            references=[],
        )

    async def fake_audio_duration(*_args, **_kwargs):
        return None

    monkeypatch.setattr(generation, "prepare_seedance2_generation_inputs", fake_prepare)
    monkeypatch.setattr(generation, "_api_audio_duration_seconds", fake_audio_duration)


@pytest.mark.asyncio
async def test_single_video_follows_project_default_backend(monkeypatch, tmp_path):
    from novelvideo.api.routes import generation
    from novelvideo.api.schemas import SingleVideoRequest

    frame = _write_frame(tmp_path)
    store = _Store(
        [
            {
                "beat_number": 2,
                "video_mode": "first_frame",
                "video_prompt": "镜头缓慢推近。",
            }
        ]
    )
    _patch_project(monkeypatch, generation, tmp_path, store)
    calls = _capture_enqueue(monkeypatch, generation)
    _stub_single_video_deps(monkeypatch, generation, frame)
    monkeypatch.setattr(
        generation,
        "load_project_config",
        lambda username, project: {"video_backend": "newapi_seedance-2.0-fast"},
    )

    response = await generation.generate_single_video(
        project="demo",
        episode_num=3,
        beat_num=2,
        body=SingleVideoRequest(),
        user={"username": "alice"},
    )

    assert response["ok"] is True
    assert calls[0]["payload"]["config"]["video_backend"] == "newapi_seedance-2.0-fast"
    assert calls[0]["payload"]["billing"]["video_backend"] == "newapi_seedance-2.0-fast"


@pytest.mark.asyncio
async def test_single_video_explicit_backend_wins_over_project_default(
    monkeypatch, tmp_path
):
    from novelvideo.api.routes import generation
    from novelvideo.api.schemas import SingleVideoRequest

    frame = _write_frame(tmp_path)
    store = _Store(
        [
            {
                "beat_number": 2,
                "video_mode": "first_frame",
                "video_prompt": "镜头缓慢推近。",
            }
        ]
    )
    _patch_project(monkeypatch, generation, tmp_path, store)
    calls = _capture_enqueue(monkeypatch, generation)
    _stub_single_video_deps(monkeypatch, generation, frame)

    def _unexpected_load(*_args, **_kwargs):
        raise AssertionError("explicit video_backend must not read project config")

    monkeypatch.setattr(generation, "load_project_config", _unexpected_load)

    response = await generation.generate_single_video(
        project="demo",
        episode_num=3,
        beat_num=2,
        body=SingleVideoRequest(video_backend="huimeng_seedance-2.0-fast"),
        user={"username": "alice"},
    )

    assert response["ok"] is True
    assert (
        calls[0]["payload"]["config"]["video_backend"] == "huimeng_seedance-2.0-fast"
    )


def test_schema_video_backend_default_follows_instance_config():
    from novelvideo.api.schemas import SingleVideoRequest, VideoGenerateRequest
    from novelvideo.config import VIDEO_BACKEND

    assert SingleVideoRequest().video_backend == VIDEO_BACKEND
    assert VideoGenerateRequest().video_backend == VIDEO_BACKEND
    assert "video_backend" not in SingleVideoRequest().model_fields_set
