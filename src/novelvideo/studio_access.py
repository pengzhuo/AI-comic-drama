"""Studio access gate: phone + code links, signed browser identities.

Design notes
------------
The CE build ships one shared UI for two audiences: the client ``/studio``
entry (one link per phone number) and the internal ``/console`` entry. This
module is the single place that turns a request's cookies into a *scoped*
identity so a project list never leaks across owners.

Opt-in per instance. The gate is only active when a client secret is
configured (env ``STUDIO_ACCESS_SECRET`` or the file
``/data/studio-access.secret``) and not switched off with ``gate=off`` in that
file. Without it the process keeps its historical CE behavior (``local``
single-user, no isolation), so upstream CE checkouts and other deployments are
untouched.

The internal console edits the same file through ``GET|POST /access/config``
(see :mod:`novelvideo.api.routes.access`): reads are masked, writes are atomic
and keep the file owner-only, and editing a key that an environment variable
pins is refused rather than silently ignored.

Isolation is deny-by-default while the gate is on:

* ``st_internal`` cookie (HMAC over ``internal|<exp>`` signed with the internal
  key) -> ``("user", "local")``: the internal console sees every project.
* ``st_studio`` cookie (HMAC over ``studio-client|<phone>|<exp>`` signed with
  the client secret) -> ``client:<phone>``: sees only its own projects.
* neither -> ``anonymous``: owns nothing, so lists come back empty and single
  projects 404.

Code verification happens only here (server side); the browser never sees the
secret. The client link is ``/studio?phone=<11 digits>&code=<md5 hex>`` where
``code = md5(phone + secret)`` — deliberately time-independent, so a link can
be forwarded/reused. The session cookie is what expires (7 days).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from novelvideo.ports.project import (
    ANONYMOUS_USER_ID,
    CLIENT_USER_PREFIX,
    LOCAL_USER_ID,
    client_user_id,
    is_client_user_id,
    phone_from_client_user_id,
)

logger = logging.getLogger("novelvideo.studio_access")

CLIENT_COOKIE_NAME = "st_studio"
INTERNAL_COOKIE_NAME = "st_internal"

CLIENT_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
INTERNAL_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60

CREDENTIAL_KIND_INTERNAL = "internal_console"
CREDENTIAL_KIND_CLIENT = "studio_client"
CREDENTIAL_KIND_ANONYMOUS = "anonymous"

INTERNAL_USER_ID = LOCAL_USER_ID

DEFAULT_SECRET_FILE = "/data/studio-access.secret"

_PHONE_RE = re.compile(r"^\d{11}$")
_CODE_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_COOKIE_SEPARATOR = "."

# (path, mtime_ns, size) -> parsed settings. Cheap mtime check keeps the
# operator story simple: edit the file, every existing session is invalidated
# on the next request without a restart.
_secret_cache: tuple[tuple[str, int, int], dict[str, str]] | None = None


def _secret_file_path() -> Path:
    raw = os.environ.get("STUDIO_ACCESS_SECRET_FILE", "").strip()
    return Path(raw or DEFAULT_SECRET_FILE)


def _parse_secret_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip().lower()] = value.strip()
    return values


def _file_settings() -> dict[str, str]:
    global _secret_cache
    path = _secret_file_path()
    try:
        stat = path.stat()
    except OSError:
        _secret_cache = None
        return {}
    stamp = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    if _secret_cache is not None and _secret_cache[0] == stamp:
        return _secret_cache[1]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("studio access secret file unreadable: %s", path)
        return {}
    values = _parse_secret_file(text)
    _secret_cache = (stamp, values)
    return values


def _setting(name: str, file_key: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    return _file_settings().get(file_key, "").strip()


def client_secret() -> str:
    return _setting("STUDIO_ACCESS_SECRET", "client_secret")


def internal_key() -> str:
    return _setting("STUDIO_INTERNAL_KEY", "internal_key")


def enabled() -> bool:
    """Gate is on whenever a client secret is configured and not flagged off."""
    return bool(client_secret()) and not gate_disabled()


# ---------------------------------------------------------------------------
# Settings: live read + hot write (what the internal console edits)
# ---------------------------------------------------------------------------
#
# The operator story stays "change a line, the next request picks it up" (see
# ``_file_settings``): the console settings page just writes that same file.
# Everything here is deny-by-default about secrets — reads return masks; the
# only place a plaintext value appears is the input box the operator types it
# into. Writes are atomic and keep the file owner-only.

# `gate=off` turns verification off without throwing the keys away, so flipping
# it back on restores the previous links/keys. Deleting the file still disables
# everything (the historical escape hatch).
GATE_FLAG_KEY = "gate"
GATE_OFF_VALUES = frozenset({"off", "false", "0", "no", "disabled"})

# Writing one of these is refused while the matching env var is set, because
# env wins over the file (``_setting``) and a silent no-op would be worse.
_ENV_FOR_KEY = {
    "client_secret": "STUDIO_ACCESS_SECRET",
    "internal_key": "STUDIO_INTERNAL_KEY",
}

_WRITABLE_KEYS = frozenset(_ENV_FOR_KEY) | {GATE_FLAG_KEY}

_KEY_LABEL = {
    "client_secret": "客户专属链接密钥",
    "internal_key": "内部访问口令",
}

_HEADER_COMMENT = """# Studio access gate settings (novelvideo.studio_access).
# Format: key=value, one per line, `#` starts a comment.
# Picked up on the next request; no restart needed.
# client_secret = 客户专属链接的签名密钥（md5(手机号 + client_secret)）
# internal_key  = 内部端 /console 的访问口令
# gate          = on/off，off 表示暂时关闭访问验证（密钥保留）
"""

_MAX_SECRET_CHARS = 200


def secret_file_path() -> Path:
    """The live settings file (``STUDIO_ACCESS_SECRET_FILE`` respected)."""
    return _secret_file_path()


def gate_disabled() -> bool:
    """True when the settings file carries ``gate=off``."""
    return _file_settings().get(GATE_FLAG_KEY, "").strip().lower() in GATE_OFF_VALUES


def _invalidate_secret_cache() -> None:
    global _secret_cache
    _secret_cache = None


def mask_secret(value: str) -> str:
    """``L8OEEqd_…45ww``: enough to recognise a value, never enough to reuse it."""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "…"
    return f"{text[:4]}…{text[-4:]}"


def generate_internal_key() -> str:
    """A 32-character URL-safe random key (24 bytes -> 32 base64url chars)."""
    return secrets.token_urlsafe(24)


def _env_value(env_name: str) -> str:
    return os.environ.get(env_name, "").strip()


def _value_source(file_key: str) -> str:
    if _env_value(_ENV_FOR_KEY[file_key]):
        return "env"
    if _file_settings().get(file_key, "").strip():
        return "file"
    return "none"


def _file_timestamp(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")


def settings_status() -> dict[str, Any]:
    """Masked settings snapshot for the internal console (never plaintext)."""
    live = _secret_file_path()
    return {
        "enabled": enabled(),
        "gate_disabled": gate_disabled(),
        "internal_key": {
            "configured": bool(internal_key()),
            "mask": mask_secret(internal_key()),
            "source": _value_source("internal_key"),
            "env_locked": bool(_env_value(_ENV_FOR_KEY["internal_key"])),
        },
        "client_secret": {
            "configured": bool(client_secret()),
            "mask": mask_secret(client_secret()),
            "source": _value_source("client_secret"),
            "env_locked": bool(_env_value(_ENV_FOR_KEY["client_secret"])),
        },
        "file": {
            "path": str(live),
            "exists": live.exists(),
            "updated_at": _file_timestamp(live),
        },
    }


def env_locked_keys() -> list[str]:
    """Keys whose env override is set, i.e. that the console must not edit."""
    return sorted(key for key, env in _ENV_FOR_KEY.items() if _env_value(env))


def clean_secret_value(key: str, value: str) -> str:
    """Validate an operator-supplied secret; raise ``ValueError`` when bad."""
    label = _KEY_LABEL.get(key, key)
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label}不能为空")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{label}不能包含换行")
    if len(text) > _MAX_SECRET_CHARS:
        raise ValueError(f"{label}不能超过 {_MAX_SECRET_CHARS} 个字符")
    return text


def _render_settings_text(current_text: str, updates: Mapping[str, str]) -> str:
    """Replace the given keys in place, keeping comments; append the new ones."""
    remaining = dict(updates)
    lines: list[str] = []
    for line in (current_text or _HEADER_COMMENT).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip().lower()
            if key in remaining:
                lines.append(f"{key}={remaining.pop(key)}")
                continue
        lines.append(line)
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    return "\n".join(lines).rstrip("\n") + "\n"


def _write_text_atomic(path: Path, text: str) -> None:
    """Owner-only atomic replace: temp beside the target, then ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _invalidate_secret_cache()


def update_settings(updates: Mapping[str, str]) -> dict[str, Any]:
    """Write validated keys into the live settings file and return the status.

    Refuses keys pinned by an environment variable (env beats file) rather than
    writing a value that would silently never apply.
    """
    unknown = sorted(set(updates) - _WRITABLE_KEYS)
    if unknown:
        raise ValueError(f"未知的设置项：{', '.join(unknown)}")
    locked = [key for key in updates if key in env_locked_keys()]
    if locked:
        names = "、".join(_ENV_FOR_KEY[key] for key in sorted(locked))
        raise ValueError(f"{names} 由环境变量提供，改文件不生效")

    cleaned = {
        key: (value if key == GATE_FLAG_KEY else clean_secret_value(key, value))
        for key, value in updates.items()
    }
    if not cleaned:
        return settings_status()

    live = _secret_file_path()
    try:
        current = live.read_text(encoding="utf-8")
    except OSError:
        current = ""
    _write_text_atomic(live, _render_settings_text(current, cleaned))
    return settings_status()


def set_gate_enabled(value: bool) -> dict[str, Any]:
    """Flag the gate on/off in the settings file, keeping the keys intact.

    Off is the historical single-user behavior (no isolation) without losing
    the client links already sent, so flipping it back on is a one-liner.
    """
    status = update_settings({GATE_FLAG_KEY: "on" if value else "off"})
    logger.info("studio access gate %s", "enabled" if value else "disabled")
    return status


def normalize_phone(raw: str | None) -> str:
    """Return the canonical 11-digit phone, or ``""`` when it is not one."""
    text = re.sub(r"[\s\-]", "", str(raw or ""))
    if text.startswith("+"):
        text = text[1:]
    if len(text) == 13 and text.startswith("86"):
        text = text[2:]
    return text if _PHONE_RE.match(text) else ""


def expected_code(phone: str) -> str:
    """``md5(phone + secret)`` as lowercase hex, or ``""`` when disabled."""
    secret = client_secret()
    if not secret:
        return ""
    return hashlib.md5(f"{phone}{secret}".encode("utf-8")).hexdigest()


def verify_code(phone: str, code: str | None) -> bool:
    """Constant-time, case-insensitive check of a client link code."""
    normalized = normalize_phone(phone)
    candidate = str(code or "").strip()
    if not normalized or not _CODE_RE.match(candidate):
        return False
    return hmac.compare_digest(expected_code(normalized), candidate.lower())


def _sign(key: str, payload: str) -> str:
    return hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _now() -> int:
    return int(time.time())


def create_client_cookie(phone: str) -> str:
    secret = client_secret()
    exp = _now() + CLIENT_SESSION_TTL_SECONDS
    payload = f"studio-client|{phone}|{exp}"
    return f"{phone}{_COOKIE_SEPARATOR}{exp}{_COOKIE_SEPARATOR}{_sign(secret, payload)}"


def read_client_cookie(raw_value: str | None) -> str:
    """Return the signed phone, or ``""`` when the cookie is absent/invalid."""
    secret = client_secret()
    if not secret or not raw_value:
        return ""
    phone, _, rest = str(raw_value).partition(_COOKIE_SEPARATOR)
    exp_text, _, signature = rest.partition(_COOKIE_SEPARATOR)
    if not phone or not exp_text or not signature:
        return ""
    try:
        exp = int(exp_text)
    except ValueError:
        return ""
    if exp <= _now():
        return ""
    payload = f"studio-client|{phone}|{exp}"
    if not hmac.compare_digest(_sign(secret, payload), signature):
        return ""
    return normalize_phone(phone)


def create_internal_cookie() -> str:
    key = internal_key()
    exp = _now() + INTERNAL_SESSION_TTL_SECONDS
    payload = f"studio-internal|{exp}"
    return f"{exp}{_COOKIE_SEPARATOR}{_sign(key, payload)}"


def verify_internal_cookie(raw_value: str | None) -> bool:
    key = internal_key()
    if not key or not raw_value:
        return False
    exp_text, _, signature = str(raw_value).partition(_COOKIE_SEPARATOR)
    if not exp_text or not signature:
        return False
    try:
        exp = int(exp_text)
    except ValueError:
        return False
    if exp <= _now():
        return False
    payload = f"studio-internal|{exp}"
    return hmac.compare_digest(_sign(key, payload), signature)


def verify_internal_key(candidate: str | None) -> bool:
    key = internal_key()
    text = str(candidate or "").strip()
    if not key or not text:
        return False
    return hmac.compare_digest(key, text)


def _local_username() -> str:
    return os.environ.get("ST_LOCAL_USERNAME", "").strip() or INTERNAL_USER_ID


def internal_identity() -> dict[str, Any]:
    """The internal console operator: sees every project."""
    return {
        "id": INTERNAL_USER_ID,
        "user_id": INTERNAL_USER_ID,
        "username": _local_username(),
        "role": "owner",
        "credential_kind": CREDENTIAL_KIND_INTERNAL,
    }


def client_identity(phone: str) -> dict[str, Any]:
    """A client link holder: only the projects created under this phone."""
    user_id = client_user_id(phone)
    return {
        "id": user_id,
        "user_id": user_id,
        "username": phone,
        "role": "owner",
        "credential_kind": CREDENTIAL_KIND_CLIENT,
        "phone": phone,
    }


def anonymous_identity() -> dict[str, Any]:
    """Deny-by-default requester: owns nothing and sees nothing."""
    return {
        "id": ANONYMOUS_USER_ID,
        "user_id": ANONYMOUS_USER_ID,
        "username": ANONYMOUS_USER_ID,
        "role": "viewer",
        "credential_kind": CREDENTIAL_KIND_ANONYMOUS,
    }


def resolve_identity(cookies: Mapping[str, str] | None) -> dict[str, Any] | None:
    """Resolve the gate identity from cookies.

    Returns ``None`` when the gate is disabled so callers can keep their
    historical credential path (and ``anonymous`` when the gate is on but the
    request carries no valid credential).
    """
    if not enabled():
        return None
    jar: Mapping[str, str] = cookies or {}
    if verify_internal_cookie(jar.get(INTERNAL_COOKIE_NAME)):
        return internal_identity()
    phone = read_client_cookie(jar.get(CLIENT_COOKIE_NAME))
    if phone:
        return client_identity(phone)
    return anonymous_identity()


def request_is_https(request: Any) -> bool:
    """Whether the browser actually reached us over TLS.

    ``ST_COOKIE_SECURE`` defaults to true, which is right for public HTTPS
    deploys but silently drops cookies on a plain-HTTP origin (the browser
    refuses to store a Secure cookie) and loops the gate forever. Deriving it
    from the request keeps both setups working.
    """
    forwarded = str(request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if forwarded:
        return forwarded.lower() == "https"
    try:
        return str(request.url.scheme).lower() == "https"
    except Exception:
        return False


__all__ = [
    "ANONYMOUS_USER_ID",
    "CLIENT_COOKIE_NAME",
    "CLIENT_SESSION_TTL_SECONDS",
    "CLIENT_USER_PREFIX",
    "CREDENTIAL_KIND_ANONYMOUS",
    "CREDENTIAL_KIND_CLIENT",
    "CREDENTIAL_KIND_INTERNAL",
    "INTERNAL_COOKIE_NAME",
    "INTERNAL_SESSION_TTL_SECONDS",
    "anonymous_identity",
    "client_identity",
    "client_user_id",
    "clean_secret_value",
    "create_client_cookie",
    "create_internal_cookie",
    "enabled",
    "env_locked_keys",
    "expected_code",
    "gate_disabled",
    "generate_internal_key",
    "internal_identity",
    "is_client_user_id",
    "mask_secret",
    "normalize_phone",
    "phone_from_client_user_id",
    "read_client_cookie",
    "request_is_https",
    "resolve_identity",
    "secret_file_path",
    "set_gate_enabled",
    "settings_status",
    "update_settings",
    "verify_code",
    "verify_internal_cookie",
    "verify_internal_key",
]
