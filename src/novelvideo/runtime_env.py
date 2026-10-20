"""Point the system interpreter at the app environment's third-party packages.

Hermes worker scripts (the ``.hermes`` plugins and skill scripts) run under the
image's *system* interpreter, while DramaClaw's third-party dependencies live in
the uv-managed project environment (``UV_PROJECT_ENVIRONMENT``, normally
``/app/.venv``). Source-only packages such as ``novelvideo`` import fine once the
checkout is on ``sys.path``, but their dependencies (jsonschema / ulid /
portalocker / referencing / rpds / mcp ...) do not resolve, which reaches the
user as "drafts are unavailable" or an import error inside a worker.

Appending (never inserting) keeps the interpreter's own distributions first, so
only packages that are genuinely missing fall through to the app environment and
Hermes' own environment keeps its versions.

Standalone and stdlib-only on purpose: it must be importable *before* any
third-party dependency is available. Unrelated to
``novelvideo.shared.runtime_env``, which reads edition / task-backend settings.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_ENVIRONMENT_ENV = "UV_PROJECT_ENVIRONMENT"
DEFAULT_APP_ENVIRONMENT_ROOTS = ("/app/.venv",)


def _site_packages_in(root: Path) -> Path | None:
    for version_dir in sorted((root / "lib").glob("python3.*")):
        candidate = version_dir / "site-packages"
        if candidate.is_dir():
            return candidate
    # Windows-style venv layout; local development and tests only.
    candidate = root / "Lib" / "site-packages"
    if candidate.is_dir():
        return candidate
    return None


def app_environment_site_packages() -> Path | None:
    """The first app-environment ``site-packages`` that exists, if any."""
    roots: list[Path] = []
    configured = (os.environ.get(APP_ENVIRONMENT_ENV) or "").strip()
    if configured:
        roots.append(Path(configured))
    roots.extend(Path(root) for root in DEFAULT_APP_ENVIRONMENT_ROOTS)
    for root in roots:
        candidate = _site_packages_in(root)
        if candidate is not None:
            return candidate
    return None


def ensure_app_environment_site_packages() -> bool:
    """Append the app environment's packages to ``sys.path``.

    Returns ``True`` only when it added the path, so callers can log the fallback
    without having to diff ``sys.path`` themselves. Missing paths are not an
    error: when the script already runs inside the app environment (tests, the
    API process) there is nothing to add.
    """
    candidate = app_environment_site_packages()
    if candidate is None:
        return False
    target = str(candidate)
    if target in sys.path:
        return False
    sys.path.append(target)
    return True
