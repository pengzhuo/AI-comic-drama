"""逐行剧本 prompt 的「剧本处理理解账」注入：接对目录 + 项目级/部署级开关。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo import script_process
from novelvideo.workflows.literal_script_writing import LiteralScriptWritingWorkflow
from novelvideo.workflows.script_writing import create_script_writing_workflow


def _payload() -> dict:
    return {
        "understanding": "题材：复仇短剧；主角：林建军。",
        "characters": [{"name": "林建军", "aliases": ["建军"], "role": "主角"}],
        "gaps": ["女主年龄未明确"],
    }


def _write_result(project_dir: Path) -> None:
    script_process.save_script_process_result(
        project_dir, payload=_payload(), production_script="正文"
    )


def _store(**kwargs) -> SimpleNamespace:
    """``CogneeStore`` 的形状：有 ``project_dir``/``state_dir``，没有 ``output_dir``。"""

    return SimpleNamespace(**kwargs)


def _workflow(tmp_path: Path, *, state_dir: Path | None = None) -> LiteralScriptWritingWorkflow:
    store = _store(project_dir=str(tmp_path), state_dir=str(state_dir or tmp_path / "state"))
    return LiteralScriptWritingWorkflow(
        cognee_store=None,
        sqlite_store=store,
        output_dir=str(tmp_path),
    )


def test_create_script_writing_workflow_injects_through_store_project_dir(tmp_path, monkeypatch):
    """回归：生产 runner 的 store 只有 ``project_dir``，注入不能因此变成空操作。"""

    monkeypatch.setenv("SCRIPT_PROCESS_DIGEST", "1")
    _write_result(tmp_path)

    workflow = create_script_writing_workflow(
        _store(project_dir=str(tmp_path), state_dir=str(tmp_path / "state"))
    )

    # 工厂仍旧不碰 ``output_dir``（保持既有语义），目录解析落在 workflow 内。
    assert workflow.output_dir == ""
    section = workflow._script_process_digest_section()
    assert "剧本理解账" in section
    assert "林建军" in section


def test_digest_section_injects_understanding_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIPT_PROCESS_DIGEST", "1")
    _write_result(tmp_path)

    section = _workflow(tmp_path)._script_process_digest_section()

    assert "剧本处理理解账" in section
    assert "林建军" in section
    assert "女主年龄未明确" in section
    # 护栏必须跟着一起走：只标注当前行，不得据此脑补。
    assert "不得据此补出原文没有的人、事、物" in section


def test_digest_section_is_empty_when_deployment_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIPT_PROCESS_DIGEST", "0")
    _write_result(tmp_path)

    assert _workflow(tmp_path)._script_process_digest_section() == ""


def test_digest_section_is_empty_without_a_result(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIPT_PROCESS_DIGEST", "1")

    assert _workflow(tmp_path)._script_process_digest_section() == ""


def test_digest_section_is_empty_without_output_dir(monkeypatch):
    monkeypatch.setenv("SCRIPT_PROCESS_DIGEST", "1")

    workflow = LiteralScriptWritingWorkflow(cognee_store=None)

    assert workflow._script_process_digest_section() == ""


def _write_project_switch(state_dir: Path, value: bool) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "project_config.json").write_text(
        json.dumps({"script_process_digest": value}), encoding="utf-8"
    )


def test_project_switch_off_beats_deployment_default_on(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIPT_PROCESS_DIGEST", "1")
    _write_result(tmp_path)
    state_dir = tmp_path / "state"
    _write_project_switch(state_dir, False)

    assert _workflow(tmp_path, state_dir=state_dir)._script_process_digest_section() == ""


def test_project_switch_on_beats_deployment_default_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIPT_PROCESS_DIGEST", "0")
    _write_result(tmp_path)
    state_dir = tmp_path / "state"
    _write_project_switch(state_dir, True)

    section = _workflow(tmp_path, state_dir=state_dir)._script_process_digest_section()

    assert "剧本理解账" in section
    assert "林建军" in section


@pytest.mark.parametrize("raw", ["on", "true", "yes", 1])
def test_project_switch_accepts_loose_truthy_values(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("SCRIPT_PROCESS_DIGEST", "0")
    _write_result(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "project_config.json").write_text(
        json.dumps({"script_process_digest": raw}), encoding="utf-8"
    )

    section = _workflow(tmp_path, state_dir=state_dir)._script_process_digest_section()

    assert "剧本理解账" in section
