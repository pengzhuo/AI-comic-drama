"""Tests for the system-interpreter app-environment fallback."""

from __future__ import annotations

import sys
from pathlib import Path

from novelvideo import runtime_env


def _fake_app_environment(root: Path, *, python: str = "python3.12") -> Path:
    site_packages = root / "lib" / python / "site-packages"
    site_packages.mkdir(parents=True)
    return site_packages


def test_appends_the_configured_app_environment_to_sys_path(monkeypatch, tmp_path):
    site_packages = _fake_app_environment(tmp_path / "app-venv")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "app-venv"))
    monkeypatch.setattr(sys, "path", list(sys.path))

    assert runtime_env.ensure_app_environment_site_packages() is True
    # Appended, never inserted: the interpreter's own distributions stay first.
    assert sys.path[-1] == str(site_packages)
    assert sys.path.index(str(site_packages)) > 0

    # Idempotent, so the entry points can call it defensively.
    assert runtime_env.ensure_app_environment_site_packages() is False
    assert sys.path.count(str(site_packages)) == 1


def test_falls_back_to_the_packaged_app_environment(monkeypatch, tmp_path):
    site_packages = _fake_app_environment(tmp_path / "app")
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    monkeypatch.setattr(runtime_env, "DEFAULT_APP_ENVIRONMENT_ROOTS", (str(tmp_path / "app"),))
    monkeypatch.setattr(sys, "path", list(sys.path))

    assert runtime_env.app_environment_site_packages() == site_packages
    assert runtime_env.ensure_app_environment_site_packages() is True


def test_is_a_no_op_without_an_app_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "missing"))
    monkeypatch.setattr(runtime_env, "DEFAULT_APP_ENVIRONMENT_ROOTS", (str(tmp_path / "also-missing"),))
    before = list(sys.path)

    assert runtime_env.app_environment_site_packages() is None
    assert runtime_env.ensure_app_environment_site_packages() is False
    assert sys.path == before


def test_ignores_an_app_environment_without_site_packages(monkeypatch, tmp_path):
    configured = tmp_path / "configured"
    (configured / "lib" / "python3.12").mkdir(parents=True)
    packaged = _fake_app_environment(tmp_path / "packaged")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(configured))
    monkeypatch.setattr(runtime_env, "DEFAULT_APP_ENVIRONMENT_ROOTS", (str(tmp_path / "packaged"),))

    # The configured root exists but carries no site-packages, so the packaged
    # default is still tried instead of returning nothing.
    assert runtime_env.app_environment_site_packages() == packaged


def test_hermes_skill_entry_scripts_bootstrap_the_fallback():
    """Skill scripts run on the system interpreter, so each one opts in.

    A regenerated entry script that loses this block fails at import time with a
    missing third-party distribution, which reaches the user as "drafts are
    unavailable" rather than as an obvious error.
    """
    root = Path(__file__).resolve().parents[1] / ".hermes" / "skills"
    scripts = sorted(
        path
        for directory in (
            root / "sketch-correction-worker" / "scripts",
            root / "sketch-storyboard-director" / "scripts",
        )
        for path in directory.glob("*.py")
    )
    entry_scripts = [
        path
        for path in scripts
        if "SRC_ROOT" in path.read_text(encoding="utf-8")
    ]

    assert len(entry_scripts) == 11
    for path in entry_scripts:
        text = path.read_text(encoding="utf-8")
        assert "ensure_app_environment_site_packages()" in text, path.name
