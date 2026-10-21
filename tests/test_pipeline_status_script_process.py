"""剧本处理阶段在共享流水线状态里的位置。

导入之后、结构化之前先按内置制作流程包处理剧本；但已经建立分集的存量项目
不得被这个新阶段回灌。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo import pipeline_status as pipeline_status_module
from novelvideo import screenplay_preprocess
from novelvideo import script_process


class _Manager:
    def __init__(self, ingest_status: str | None = "completed") -> None:
        self._ingest_status = ingest_status

    def get_task(self, *_args, **_kwargs):
        if self._ingest_status is None:
            return None
        return SimpleNamespace(status=self._ingest_status, task_id="t")

    def get_task_for_project(self, *_args, **_kwargs):
        return self.get_task()


class _Store:
    def __init__(
        self,
        *,
        characters: list | None = None,
        episodes: list | None = None,
        beats: list | None = None,
    ) -> None:
        self._characters = characters or []
        self._episodes = episodes or []
        self._beats = beats or []
        self._episode = (
            self._episodes[0] if self._episodes else SimpleNamespace(number=1, identity_ids=[])
        )

    def get_all_characters(self):
        return list(self._characters)

    def get_all_episodes(self):
        return list(self._episodes)

    def get_episode(self, _number: int):
        return self._episode

    async def get_beats_as_dicts(self, _episode: int):
        return list(self._beats)


def _patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, configured: bool) -> None:
    monkeypatch.setattr(
        pipeline_status_module, "get_task_manager", lambda: _Manager()
    )
    monkeypatch.setattr(
        pipeline_status_module, "_user_has_configured", lambda *_: configured
    )
    monkeypatch.setattr(
        pipeline_status_module,
        "compute_portrait_path",
        lambda _dir, name: tmp_path / f"{name}_portrait.png",
    )


async def _status(tmp_path: Path, store: _Store):
    return await pipeline_status_module.compute_pipeline_status(
        store=store,
        username="alice",
        project_name="demo",
        project_dir=tmp_path,
        episode=None,
    )


@pytest.mark.asyncio
async def test_freshly_imported_project_goes_to_script_processing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch(monkeypatch, tmp_path, configured=False)
    # 预处理（确定性规范化）先跑，之后才是技能阶段。
    screenplay_preprocess.save_preprocess_result(
        tmp_path, payload={"changed": False}, normalized_text="1-1 车间 夜 内\n"
    )

    data = await _status(tmp_path, _Store())

    assert data["next_step"] == "script_process"
    assert data["next_step_name"] == "剧本处理"
    assert data["global"]["script_processed"] is False
    assert data["global"]["script_process_pending"] is True


@pytest.mark.asyncio
async def test_processing_advances_to_configuring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch(monkeypatch, tmp_path, configured=False)
    screenplay_preprocess.save_preprocess_result(
        tmp_path, payload={"changed": False}, normalized_text="1-1 车间 夜 内\n"
    )
    script_process.save_script_process_result(
        tmp_path,
        payload={"understanding": "理解"},
        production_script="正文",
    )

    data = await _status(tmp_path, _Store())

    assert data["global"]["script_processed"] is True
    assert data["global"]["script_process_pending"] is False
    assert data["next_step"] == "configure"


@pytest.mark.asyncio
async def test_projects_with_episodes_are_not_dragged_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """存量项目（已分集）不回灌新阶段——否则会打断正在跑的生成流程。"""

    _patch(monkeypatch, tmp_path, configured=False)

    data = await _status(
        tmp_path, _Store(episodes=[SimpleNamespace(number=1, identity_ids=[])])
    )

    assert data["global"]["script_process_pending"] is False
    assert data["next_step"] != "script_process"
    assert data["next_step"] == "configure"


@pytest.mark.asyncio
async def test_completed_global_prep_reaches_the_episode_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch(monkeypatch, tmp_path, configured=True)
    script_process.save_script_process_result(
        tmp_path,
        payload={"understanding": "理解"},
        production_script="正文",
    )
    character = SimpleNamespace(
        name="林建军", face_prompt="face", is_main=True, identities=[]
    )
    episode = SimpleNamespace(number=1, identity_ids=[])

    data = await _status(
        tmp_path, _Store(characters=[character], episodes=[episode])
    )

    assert data["global"]["portraits_done"] is True
    assert data["next_step"] == "identity_plan"
