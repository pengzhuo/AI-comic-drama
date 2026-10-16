"""Per-episode identity-image progress in the shared pipeline status.

捏导 used to guess a per-episode identity route (``/episodes/{n}/identities``,
which 404s) to find out which identities still lacked an image. The status now
carries the planned/done counts and the exact missing entries.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo import pipeline_status as pipeline_status_module


class _Manager:
    def get_task(self, *_args, **_kwargs):
        return None

    def get_task_for_project(self, *_args, **_kwargs):
        return None


class _Store:
    def __init__(self, characters, episodes) -> None:
        self._characters = characters
        self._episodes = episodes

    def get_all_characters(self):
        return self._characters

    def get_all_episodes(self):
        return self._episodes

    def get_episode(self, _number: int):
        return self._episodes[0]

    async def get_beats_as_dicts(self, _episode: int):
        return []


def _character(name: str, identities: list[tuple[str, str]]):
    return SimpleNamespace(
        name=name,
        face_prompt="face",
        is_main=name == "林建军",
        identities=[
            SimpleNamespace(identity_id=identity_id, identity_name=identity_name)
            for identity_id, identity_name in identities
        ],
    )


def _patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, existing: set[tuple[str, str]]):
    monkeypatch.setattr(pipeline_status_module, "get_task_manager", lambda: _Manager())
    monkeypatch.setattr(pipeline_status_module, "_user_has_configured", lambda *_: True)
    monkeypatch.setattr(
        pipeline_status_module,
        "compute_portrait_path",
        lambda _dir, name: tmp_path / f"{name}_portrait.png",
    )
    marker = tmp_path / "identity.png"
    monkeypatch.setattr(
        pipeline_status_module,
        "compute_identity_path",
        lambda _dir, name, identity_name: (
            marker if (name, identity_name) in existing else None
        ),
    )


async def _status(tmp_path: Path):
    characters = [
        _character("林建军", [("林建军_青年时期", "青年时期")]),
        _character("苏婉", [("苏婉_青年时期", "青年时期")]),
        _character("二狗子", [("二狗子_青年时期", "青年时期")]),
    ]
    episodes = [
        SimpleNamespace(
            number=1,
            identity_ids=[
                "林建军_青年时期",
                "苏婉_青年时期",
                "二狗子_青年时期",
            ],
        )
    ]
    return await pipeline_status_module.compute_pipeline_status(
        store=_Store(characters, episodes),
        username="alice",
        project_name="demo",
        project_dir=tmp_path,
        episode=1,
    )


@pytest.mark.asyncio
async def test_identity_progress_lists_the_missing_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch(monkeypatch, tmp_path, {("林建军", "青年时期")})

    data = await _status(tmp_path)
    status = data["episode_status"]

    assert status["identity_images"] is False
    assert status["identity_images_planned"] == 3
    assert status["identity_images_done"] == 1
    assert status["identities_missing"] == [
        {
            "character": "苏婉",
            "identity_id": "苏婉_青年时期",
            "identity_name": "青年时期",
        },
        {
            "character": "二狗子",
            "identity_id": "二狗子_青年时期",
            "identity_name": "青年时期",
        },
    ]
    assert data["next_step"] == "identity_images"


@pytest.mark.asyncio
async def test_identity_progress_is_complete_with_no_missing_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch(
        monkeypatch,
        tmp_path,
        {("林建军", "青年时期"), ("苏婉", "青年时期"), ("二狗子", "青年时期")},
    )

    data = await _status(tmp_path)
    status = data["episode_status"]

    assert status["identity_images"] is True
    assert status["identity_images_planned"] == 3
    assert status["identity_images_done"] == 3
    assert status["identities_missing"] == []
    assert data["next_step"] != "identity_images"
