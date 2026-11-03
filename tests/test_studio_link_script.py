"""`scripts/studio_link.py` 与后端门禁必须算出同一个校验码。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from novelvideo import studio_access

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "studio_link.py"
PHONE = "13800000000"
SECRET = "test-client-secret"


@pytest.fixture(scope="module")
def link_script():
    spec = importlib.util.spec_from_file_location("studio_link_script", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_code_matches_backend(link_script, monkeypatch):
    monkeypatch.setenv("STUDIO_ACCESS_SECRET", SECRET)
    monkeypatch.setattr(studio_access, "_secret_cache", None)

    code = link_script.client_code(PHONE, SECRET)

    assert code == studio_access.expected_code(PHONE)
    assert len(code) == 32


def test_main_prints_link_with_normalized_phone(link_script, capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("STUDIO_ACCESS_SECRET", raising=False)
    monkeypatch.delenv("STUDIO_BASE_URL", raising=False)
    secret_file = tmp_path / "studio-access.secret"
    secret_file.write_text(f"# comment\nclient_secret={SECRET}\n", encoding="utf-8")

    exit_code = link_script.main(
        [
            "--phone",
            "138 0000 0000",
            "--base",
            "https://example.com/",
            "--secret-file",
            str(secret_file),
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    assert out == (
        f"https://example.com/studio?phone={PHONE}"
        f"&code={link_script.client_code(PHONE, SECRET)}"
    )


def test_main_supports_multiple_phones(link_script, capsys, monkeypatch):
    monkeypatch.setenv("STUDIO_ACCESS_SECRET", SECRET)
    monkeypatch.setenv("STUDIO_BASE_URL", "https://example.com")

    exit_code = link_script.main(["--phone", PHONE, "--phone", "13900000000"])

    assert exit_code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("https://example.com/studio?phone=13800000000&code=")
    assert lines[1].startswith("https://example.com/studio?phone=13900000000&code=")


def test_main_canvas_entry_uses_same_code(link_script, capsys, monkeypatch):
    monkeypatch.setenv("STUDIO_ACCESS_SECRET", SECRET)
    monkeypatch.setenv("STUDIO_BASE_URL", "https://example.com")

    assert link_script.main(["--phone", PHONE, "--entry", "canvas"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == (
        f"https://example.com/canvas?phone={PHONE}"
        f"&code={link_script.client_code(PHONE, SECRET)}"
    )


def test_main_canvas_entry_can_target_project(link_script, capsys, monkeypatch):
    monkeypatch.setenv("STUDIO_ACCESS_SECRET", SECRET)
    monkeypatch.setenv("STUDIO_BASE_URL", "https://example.com")

    assert (
        link_script.main(
            ["--phone", PHONE, "--entry", "canvas", "--project", "01M6GSBBZJ0ZTQ3XNFYSFNCE05"]
        )
        == 0
    )
    assert capsys.readouterr().out.strip().startswith(
        "https://example.com/canvas/01M6GSBBZJ0ZTQ3XNFYSFNCE05?phone=13800000000&code="
    )


def test_main_all_entry_prints_both_paths(link_script, capsys, monkeypatch):
    monkeypatch.setenv("STUDIO_ACCESS_SECRET", SECRET)
    monkeypatch.setenv("STUDIO_BASE_URL", "https://example.com")

    assert link_script.main(["--phone", PHONE, "--entry", "all"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("https://example.com/studio?phone=13800000000&code=")
    assert lines[1].startswith("https://example.com/canvas?phone=13800000000&code=")


def test_main_rejects_bad_project(link_script, capsys, monkeypatch):
    monkeypatch.setenv("STUDIO_ACCESS_SECRET", SECRET)

    exit_code = link_script.main(["--phone", PHONE, "--entry", "canvas", "--project", "../x"])

    assert exit_code == 2
    assert "项目 id" in capsys.readouterr().err


def test_main_rejects_bad_phone(link_script, capsys, monkeypatch):
    monkeypatch.setenv("STUDIO_ACCESS_SECRET", SECRET)

    exit_code = link_script.main(["--phone", "1380000000"])

    assert exit_code == 2
    assert "11 位数字" in capsys.readouterr().err


def test_main_reports_missing_secret(link_script, capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("STUDIO_ACCESS_SECRET", raising=False)

    exit_code = link_script.main(
        ["--phone", PHONE, "--secret-file", str(tmp_path / "missing.secret")]
    )

    assert exit_code == 2
    assert "secret" in capsys.readouterr().err
