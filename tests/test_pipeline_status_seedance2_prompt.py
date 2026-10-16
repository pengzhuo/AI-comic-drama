"""The Seedance 2.0 prompt step in the shared pipeline status.

A Seedance 2.0-family backend refuses to render a beat whose 2.0 final prompt was
never composed, so the status has to gate the video step on it. Every other
backend treats the step as vacuously complete and keeps its original ordering.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo import pipeline_status as pipeline_status_module
from novelvideo.seedance2_i2v.models import dump_seedance2_config

IDENTITY_ID = "林建军_青年时期"


class _Manager:
    def get_task(self, *_args, **_kwargs):
        return None

    def get_task_for_project(self, *_args, **_kwargs):
        return None


def _character():
    return SimpleNamespace(
        name="林建军",
        face_prompt="face",
        is_main=True,
        identities=[
            SimpleNamespace(identity_id=IDENTITY_ID, identity_name="青年时期"),
        ],
    )


def _episode():
    return SimpleNamespace(number=1, identity_ids=[IDENTITY_ID])


class _Store:
    def __init__(self, beats) -> None:
        self._beats = beats
        self._episode = _episode()

    def get_all_characters(self):
        return [_character()]

    def get_all_episodes(self):
        return [self._episode]

    def get_episode(self, _number: int):
        return self._episode

    async def get_beats_as_dicts(self, _episode: int):
        return list(self._beats)


def _beat(number: int, *, final_prompt: str = "") -> dict:
    return {
        "beat_number": number,
        "narration_segment": f"第 {number} 幕",
        "audio_type": "narration",
        "detected_identities": [],
        "detected_props": [],
        "video_mode": "first_frame",
        "video_prompt": "他看着远方",
        "seedance2_config_json": dump_seedance2_config({"final_prompt": final_prompt}),
    }


def _write_media(tmp_path: Path, beat_numbers: tuple[int, ...]) -> None:
    for folder, suffix in (
        ("frames/ep001", "png"),
        ("audio/ep001", "mp3"),
        ("sketches/ep001", "png"),
    ):
        target = tmp_path / folder
        target.mkdir(parents=True, exist_ok=True)
        for beat_num in beat_numbers:
            (target / f"beat_{beat_num:02d}.{suffix}").write_bytes(b"x")


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    requires_prompt: bool,
) -> None:
    monkeypatch.setattr(pipeline_status_module, "get_task_manager", lambda: _Manager())
    monkeypatch.setattr(pipeline_status_module, "_user_has_configured", lambda *_: True)
    monkeypatch.setattr(
        pipeline_status_module,
        "compute_portrait_path",
        lambda _dir, name: tmp_path / f"{name}_portrait.png",
    )
    monkeypatch.setattr(
        pipeline_status_module,
        "compute_identity_path",
        lambda _dir, name, identity_name: tmp_path / "identity.png",
    )
    monkeypatch.setattr(
        pipeline_status_module,
        "seedance2_prompt_required",
        lambda *_args, **_kwargs: requires_prompt,
    )


async def _status(tmp_path: Path, beats: list[dict]):
    return await pipeline_status_module.compute_pipeline_status(
        store=_Store(beats),
        username="alice",
        project_name="demo",
        project_dir=tmp_path,
        episode=1,
    )


@pytest.mark.asyncio
async def test_status_blocks_the_video_step_until_every_beat_has_a_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch(monkeypatch, tmp_path, requires_prompt=True)
    _write_media(tmp_path, (1, 2))
    beats = [_beat(1, final_prompt="已有提示词"), _beat(2)]

    data = await _status(tmp_path, beats)

    assert data["episode_status"]["script"] is True
    assert data["episode_status"]["tts"] is True
    assert data["episode_status"]["seedance2_prompt"] is False
    assert data["next_step"] == "seedance2_prompt"
    assert data["next_step_name"] == "Seedance 2.0 提示词"


@pytest.mark.asyncio
async def test_status_advances_once_every_beat_has_a_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch(monkeypatch, tmp_path, requires_prompt=True)
    _write_media(tmp_path, (1, 2))
    beats = [_beat(1, final_prompt="第一幕"), _beat(2, final_prompt="第二幕")]

    data = await _status(tmp_path, beats)

    assert data["episode_status"]["seedance2_prompt"] is True
    assert data["next_step"] == "single_video"


@pytest.mark.asyncio
async def test_status_skips_the_prompt_step_for_other_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """1.x backends never read ``final_prompt``, so the step must stay vacuous."""

    _patch(monkeypatch, tmp_path, requires_prompt=False)
    _write_media(tmp_path, (1, 2))
    beats = [_beat(1), _beat(2)]

    data = await _status(tmp_path, beats)

    assert data["episode_status"]["seedance2_prompt"] is True
    assert data["next_step"] == "single_video"


@pytest.mark.asyncio
async def test_previous_steps_still_win_over_the_prompt_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The prompt step sits after TTS, so missing audio keeps its own step."""

    _patch(monkeypatch, tmp_path, requires_prompt=True)
    _write_media(tmp_path, (1, 2))
    (tmp_path / "audio" / "ep001" / "beat_02.mp3").unlink()
    beats = [_beat(1), _beat(2)]

    data = await _status(tmp_path, beats)

    assert data["episode_status"]["seedance2_prompt"] is False
    assert data["next_step"] == "tts"


def test_seedance2_prompt_required_follows_the_effective_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo import project_config

    monkeypatch.setattr(
        project_config,
        "effective_video_backend",
        lambda *_args, **_kwargs: "newapi_seedance-2.0-fast",
    )
    assert pipeline_status_module.seedance2_prompt_required("alice", "demo") is True

    monkeypatch.setattr(
        project_config,
        "effective_video_backend",
        lambda *_args, **_kwargs: "newapi_seedance-1.0-pro-fast",
    )
    assert pipeline_status_module.seedance2_prompt_required("alice", "demo") is False


def test_beat_seedance2_prompt_ready_reads_the_stored_final_prompt() -> None:
    ready = pipeline_status_module.beat_seedance2_prompt_ready

    assert ready({"seedance2_config_json": dump_seedance2_config({"final_prompt": "x"})}) is True
    assert ready({"seedance2_config_json": ""}) is False
    assert ready({}) is False
    # A row that no longer parses must not take the whole status call down.
    assert ready({"seedance2_config_json": '{"mode": "not-a-mode"}'}) is False
