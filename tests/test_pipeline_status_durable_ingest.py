"""摄入阶段必须按持久产物判断，而不是按会过期的 ``ingest_fast`` 任务行。

回归 nn_1021_1：导入完成一小时之后行被惰性删除，项目状态退回「小说摄入」，
自动流程也会白跑一次导入。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import novelvideo.pipeline_status as pipeline_status_module
from novelvideo.novel_source import novel_source_digest
from novelvideo.project_config import load_project_config_file_from_state_dir
from novelvideo.task_backend.runners.ingest import remember_ingest_completion


class _EmptyStore:
    """只走到「还没有角色/分集」的早退分支，所以只需要这两个读口。"""

    def get_all_characters(self) -> list:
        return []

    def get_all_episodes(self) -> list:
        return []


class _NoTaskManager:
    def get_task(self, *_args, **_kwargs):
        return None

    def get_task_for_project(self, *_args, **_kwargs):
        return None


def _project_dirs(tmp_path: Path) -> tuple[Path, Path]:
    project_dir = tmp_path / "output" / "local" / "nn_demo"
    state_dir = tmp_path / "state" / "local" / "nn_demo"
    project_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    return project_dir, state_dir


def test_remember_ingest_completion_persists_source_digest(tmp_path):
    project_dir, state_dir = _project_dirs(tmp_path)
    (project_dir / "novel.txt").write_text("第 1 章\n正文\n", encoding="utf-8")

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.output_dir = project_dir
    ctx.state_dir = state_dir

    remember_ingest_completion(ctx)

    config = load_project_config_file_from_state_dir(state_dir)
    assert config["ingest_source_digest"] == novel_source_digest(project_dir)


def test_durable_ingest_digest_only_matches_the_same_text(tmp_path, monkeypatch):
    project_dir, _state_dir = _project_dirs(tmp_path)
    novel = project_dir / "novel.txt"
    novel.write_text("第一部小说的正文", encoding="utf-8")
    digest = novel_source_digest(project_dir)
    assert digest

    monkeypatch.setattr(
        "novelvideo.project_config.load_project_config",
        lambda _u, _p: {"ingest_source_digest": digest},
    )
    assert (
        pipeline_status_module._ingest_artifact_digest_matches(
            "local", "nn_demo", project_dir
        )
        is True
    )

    # 重新上传换了文本：旧指纹不再代表「已导入」，自动流程会重新摄入。
    novel.write_text("换了一部小说", encoding="utf-8")
    assert (
        pipeline_status_module._ingest_artifact_digest_matches(
            "local", "nn_demo", project_dir
        )
        is False
    )

    # 没有标记（从未成功摄入过）不算已导入。
    monkeypatch.setattr(
        "novelvideo.project_config.load_project_config", lambda _u, _p: {}
    )
    assert (
        pipeline_status_module._ingest_artifact_digest_matches(
            "local", "nn_demo", project_dir
        )
        is False
    )


@pytest.mark.asyncio
async def test_pipeline_status_stays_ingested_after_task_row_expires(
    tmp_path, monkeypatch
):
    project_dir, state_dir = _project_dirs(tmp_path)
    (project_dir / "novel.txt").write_text("第 1 章\n正文\n", encoding="utf-8")

    # 摄入完成时记下的指纹（任务行早就被惰性删掉了）。
    monkeypatch.setattr(
        "novelvideo.project_config.load_project_config",
        lambda _u, _p: {"ingest_source_digest": novel_source_digest(project_dir)},
    )
    monkeypatch.setattr(
        pipeline_status_module, "get_task_manager", lambda: _NoTaskManager()
    )

    status = await pipeline_status_module.compute_pipeline_status(
        store=_EmptyStore(),
        username="local",
        project_name="nn_demo",
        project_dir=project_dir,
        ctx=None,
    )

    assert status["global"]["ingested"] is True
    # 导入之后先做预处理，再做剧本处理（技能阶段）。
    assert status["next_step"] == "screenplay_preprocess"
    assert status["global"]["screenplay_preprocess_pending"] is True
    assert status["global"]["script_process_pending"] is True


@pytest.mark.asyncio
async def test_pipeline_status_requires_ingest_after_the_text_changed(
    tmp_path, monkeypatch
):
    project_dir, _state_dir = _project_dirs(tmp_path)
    novel = project_dir / "novel.txt"
    novel.write_text("换了一部小说", encoding="utf-8")

    monkeypatch.setattr(
        "novelvideo.project_config.load_project_config",
        lambda _u, _p: {"ingest_source_digest": "stale-digest"},
    )
    monkeypatch.setattr(
        pipeline_status_module, "get_task_manager", lambda: _NoTaskManager()
    )

    status = await pipeline_status_module.compute_pipeline_status(
        store=_EmptyStore(),
        username="local",
        project_name="nn_demo",
        project_dir=project_dir,
        ctx=None,
    )

    assert status["global"]["ingested"] is False
    # STEP_MAP 暴露的是任务类型本身。
    assert status["next_step"] == "ingest_fast"


def test_novel_source_digest_matches_the_canonical_source_hash(tmp_path):
    """指纹必须与脚本结果里的 ``source_sha256`` 同算法，存量项目才能对上。"""

    from novelvideo.script_process import (
        load_script_process_result,
        save_script_process_result,
    )
    from novelvideo.story_analysis import source_sha256

    project_dir, _state_dir = _project_dirs(tmp_path)
    text = "第 1 章\n正文\n"
    (project_dir / "novel.txt").write_text(text, encoding="utf-8")

    digest = novel_source_digest(project_dir)
    assert digest == source_sha256(text)

    save_script_process_result(
        project_dir,
        payload={"source_sha256": source_sha256(text)},
        production_script="# 制作剧本\n",
    )
    assert load_script_process_result(project_dir)["source_sha256"] == digest


def test_ingest_status_falls_back_to_the_persisted_script_digest(tmp_path, monkeypatch):
    """存量项目没有配置标记，但脚本结果里记着它分析的文本指纹。"""

    from novelvideo.script_process import save_script_process_result

    project_dir, _state_dir = _project_dirs(tmp_path)
    novel = project_dir / "novel.txt"
    novel.write_text("第一部小说的正文", encoding="utf-8")
    save_script_process_result(
        project_dir,
        payload={"source_sha256": novel_source_digest(project_dir)},
        production_script="# 制作剧本\n",
    )
    monkeypatch.setattr(
        "novelvideo.project_config.load_project_config", lambda _u, _p: {}
    )
    assert (
        pipeline_status_module._ingest_artifact_digest_matches(
            "local", "nn_demo", project_dir
        )
        is True
    )

    # 重新上传换了文本：旧结果不再代表「已摄入」。
    novel.write_text("换了一部小说", encoding="utf-8")
    assert (
        pipeline_status_module._ingest_artifact_digest_matches(
            "local", "nn_demo", project_dir
        )
        is False
    )
