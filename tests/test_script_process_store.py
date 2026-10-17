"""剧本处理产物存储（script_process module）的读写与摘要。"""

from __future__ import annotations

import json
from pathlib import Path

from novelvideo import script_process


def _payload() -> dict:
    return {
        "understanding": "题材：复仇短剧；主角：林建军。",
        "characters": [
            {
                "name": "林建军",
                "aliases": ["建军"],
                "role": "主角",
                "first_appearance": "第1场",
                "notes": "",
            }
        ],
        "scenes": [{"name": "老宅", "time_of_day": "夜", "description": "堂屋"}],
        "gaps": ["女主年龄未明确"],
    }


def test_result_is_absent_until_saved(tmp_path: Path) -> None:
    assert script_process.has_script_process_result(tmp_path) is False
    assert script_process.load_script_process_result(tmp_path) is None
    assert script_process.read_production_script(tmp_path) == ""


def test_save_writes_both_files_and_publishes_result_last(tmp_path: Path) -> None:
    path = script_process.save_script_process_result(
        tmp_path,
        payload=_payload(),
        production_script="# 结构化制作剧本\n\n第一场。\n",
    )

    assert path == script_process.result_path(tmp_path)
    assert script_process.has_script_process_result(tmp_path) is True
    assert (
        script_process.read_production_script(tmp_path)
        == "# 结构化制作剧本\n\n第一场。\n"
    )

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["version"] == script_process.RESULT_VERSION
    assert stored["production_script_path"] == script_process.PRODUCTION_SCRIPT_FILENAME
    assert stored["understanding"].startswith("题材")


def test_production_script_alone_is_not_a_completion_marker(tmp_path: Path) -> None:
    """只有 ``result.json`` 才是完成标志，单独存在的正文不算完成。"""

    script_process.script_process_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    script_process.production_script_path(tmp_path).write_text("正文", encoding="utf-8")

    assert script_process.has_script_process_result(tmp_path) is False
    assert script_process.read_production_script(tmp_path) == "正文"


def test_corrupt_result_does_not_raise(tmp_path: Path) -> None:
    script_process.script_process_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    script_process.result_path(tmp_path).write_text("{not json", encoding="utf-8")

    assert script_process.has_script_process_result(tmp_path) is True
    assert script_process.load_script_process_result(tmp_path) is None


def test_downstream_digest_carries_understanding_people_and_gaps(tmp_path: Path) -> None:
    script_process.save_script_process_result(
        tmp_path, payload=_payload(), production_script="正文"
    )

    digest = script_process.build_downstream_digest(tmp_path)

    assert "剧本理解账" in digest
    assert "林建军" in digest
    assert "别名：建军" in digest
    assert "女主年龄未明确" in digest


def test_downstream_digest_is_empty_without_a_result(tmp_path: Path) -> None:
    assert script_process.build_downstream_digest(tmp_path) == ""


def test_downstream_digest_truncates(tmp_path: Path) -> None:
    payload = _payload()
    payload["understanding"] = "很长" * 500
    script_process.save_script_process_result(
        tmp_path, payload=payload, production_script="正文"
    )

    digest = script_process.build_downstream_digest(tmp_path, max_chars=50)

    assert len(digest) == 50
    assert digest.endswith("…")
