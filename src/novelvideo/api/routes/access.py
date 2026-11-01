"""Studio (/studio) and internal console (/console) access sessions.

These endpoints are the only way to obtain the scoped cookies that
``novelvideo.studio_access`` verifies:

* ``POST /access/client-session``  — exchange a phone link (phone + md5 code)
* ``POST /access/internal-session`` — exchange the internal console key
* ``GET  /access/session``         — probe the current identity (never fails)
* ``POST /access/logout``          — clear both cookies

The internal console portals on this module for its settings page:

* ``GET  /access/config``          — masked gate settings (never plaintext)
* ``POST /access/config``          — rotate the client secret / internal key
* ``POST /access/config/link``     — build a client link for a phone number
* ``POST /access/gate``            — turn the gate off (or back on)

Verification is server-side only. Responses deliberately avoid 401 so the SPA's
global session-expiry handler (any 401 -> /login) is never triggered by a bad
code or a probe.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from novelvideo import studio_access
from novelvideo.shared.runtime_env import cookie_secure as runtime_cookie_secure

router = APIRouter()
logger = logging.getLogger("novelvideo.api.access")


class ClientSessionBody(BaseModel):
    phone: str = ""
    code: str = ""


class InternalSessionBody(BaseModel):
    key: str = ""


def _cookie_secure_for(request: Request) -> bool:
    """Mirror the operator's ST_COOKIE_SECURE, but never set Secure on http.

    A Secure cookie on a plain-HTTP origin is silently dropped by the browser,
    which would loop the gate forever.
    """
    return runtime_cookie_secure() and studio_access.request_is_https(request)


def _set_scoped_cookie(
    response: JSONResponse,
    *,
    name: str,
    value: str,
    max_age: int,
    request: Request,
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure_for(request),
        max_age=max_age,
        path="/",
    )


def _clear_scoped_cookie(response: JSONResponse, *, name: str, request: Request) -> None:
    response.delete_cookie(
        key=name,
        path="/",
        samesite="lax",
        secure=_cookie_secure_for(request),
    )


class AccessConfigBody(BaseModel):
    """Empty string means "leave this key alone" (an empty key is invalid)."""

    internal_key: str = ""
    client_secret: str = ""


class AccessGateBody(BaseModel):
    enabled: bool = True
    confirm_key: str = ""
    internal_key: str = ""
    client_secret: str = ""


class AccessLinkBody(BaseModel):
    phone: str = ""


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"ok": False, "error": message})


# ---------------------------------------------------------------------------
# Brute-force guard
# ---------------------------------------------------------------------------
# The console is reachable from the public internet, so the two places that
# accept a secret (internal-session, settings writes) get a tiny in-process
# limiter: failures only, 8 tries per IP per 5 minutes, no new dependency.
# One uvicorn process in practice, so process-local state is enough.

_RATE_WINDOW_SECONDS = 300.0
_RATE_MAX_FAILURES = 8
_RATE_SCOPE_INTERNAL = "internal-session"
_RATE_SCOPE_SETTINGS = "access-settings"
_RATE_MAX_KEYS = 512

_rate_lock = threading.Lock()
_rate_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    """First ``X-Forwarded-For`` hop (nginx appends the real peer last)."""
    forwarded = str(request.headers.get("x-forwarded-for") or "")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    client = request.client
    return client.host if client else "unknown"


def _rate_key(request: Request, scope: str) -> str:
    return f"{scope}:{_client_ip(request)}"


def _prune(hits: list[float], now: float) -> list[float]:
    return [stamp for stamp in hits if now - stamp < _RATE_WINDOW_SECONDS]


def _rate_limited(request: Request, scope: str) -> bool:
    now = time.monotonic()
    key = _rate_key(request, scope)
    with _rate_lock:
        hits = _prune(_rate_failures.get(key, []), now)
        if hits:
            _rate_failures[key] = hits
        else:
            _rate_failures.pop(key, None)
        return len(hits) >= _RATE_MAX_FAILURES


def _note_failure(request: Request, scope: str) -> None:
    now = time.monotonic()
    key = _rate_key(request, scope)
    with _rate_lock:
        hits = _prune(_rate_failures.get(key, []), now)
        hits.append(now)
        _rate_failures[key] = hits
        if len(_rate_failures) > _RATE_MAX_KEYS:
            for stale, stamps in list(_rate_failures.items()):
                if not _prune(stamps, now):
                    _rate_failures.pop(stale, None)


def _clear_failures(request: Request, scope: str) -> None:
    with _rate_lock:
        _rate_failures.pop(_rate_key(request, scope), None)


def _rate_limited_response() -> JSONResponse:
    return _error(429, "尝试次数过多，请 5 分钟后再试")


# ---------------------------------------------------------------------------
# Settings guards
# ---------------------------------------------------------------------------


def _has_internal_session(request: Request) -> bool:
    return studio_access.verify_internal_cookie(request.cookies.get(studio_access.INTERNAL_COOKIE_NAME))


def _settings_access_error(request: Request) -> JSONResponse | None:
    """Authorise the settings API, or return the response to send instead.

    Gate on  -> an internal console session is required (403, never 401, so the
                SPA's global session-expiry handler stays out of it).
    Gate off -> 404 for anyone else: the operator's still-valid internal session
                can flip the gate back on, nobody else can reach the masks.
    """
    if _has_internal_session(request):
        return None
    if not studio_access.enabled():
        return _error(404, "studio access is not enabled")
    return _error(403, "仅内部端可操作访问设置")


def _studio_link_base(request: Request) -> str:
    """Absolute base for client links (``STUDIO_BASE_URL`` wins when set)."""
    configured = os.environ.get("STUDIO_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    host = str(request.headers.get("x-forwarded-host") or request.headers.get("host") or "")
    host = host.split(",")[0].strip()
    if not host:
        return "https://localhost"
    scheme = "https" if studio_access.request_is_https(request) else "http"
    return f"{scheme}://{host}"


def _client_link(request: Request, phone: str) -> str:
    code = studio_access.expected_code(phone)
    return f"{_studio_link_base(request)}/studio?phone={phone}&code={code}"


def _verify_confirm_key(request: Request, confirm_key: str) -> bool:
    current = studio_access.internal_key()
    return bool(current) and hmac.compare_digest(current, str(confirm_key or "").strip())


@router.post("/access/client-session")
async def create_client_session(body: ClientSessionBody, request: Request):
    """Activate a client session from a phone + code link."""
    if not studio_access.enabled():
        return _error(404, "studio access is not enabled")
    phone = studio_access.normalize_phone(body.phone)
    if not phone:
        return _error(400, "手机号需为 11 位数字")
    if not studio_access.verify_code(phone, body.code):
        logger.warning("studio link rejected (phone ****%s)", phone[-4:])
        return _error(400, "访问链接无效，请使用收到的专属链接")
    response = JSONResponse({"ok": True, "data": {"kind": "client", "phone": phone}})
    _set_scoped_cookie(
        response,
        name=studio_access.CLIENT_COOKIE_NAME,
        value=studio_access.create_client_cookie(phone),
        max_age=studio_access.CLIENT_SESSION_TTL_SECONDS,
        request=request,
    )
    logger.info("studio session activated (phone ****%s)", phone[-4:])
    return response


@router.post("/access/internal-session")
async def create_internal_session(body: InternalSessionBody, request: Request):
    """Activate an internal console session from the internal key."""
    if not studio_access.enabled():
        return _error(404, "studio access is not enabled")
    if _rate_limited(request, _RATE_SCOPE_INTERNAL):
        return _rate_limited_response()
    if not studio_access.internal_key():
        return _error(503, "internal access key is not configured")
    if not studio_access.verify_internal_key(body.key):
        logger.warning("internal console key rejected")
        _note_failure(request, _RATE_SCOPE_INTERNAL)
        return _error(400, "内部访问口令不正确")
    _clear_failures(request, _RATE_SCOPE_INTERNAL)
    response = JSONResponse({"ok": True, "data": {"kind": "internal"}})
    _set_scoped_cookie(
        response,
        name=studio_access.INTERNAL_COOKIE_NAME,
        value=studio_access.create_internal_cookie(),
        max_age=studio_access.INTERNAL_SESSION_TTL_SECONDS,
        request=request,
    )
    logger.info("internal console session activated")
    return response


@router.get("/access/session")
async def read_access_session(request: Request):
    """Report which identity this browser currently holds."""
    if not studio_access.enabled():
        return {"ok": True, "data": {"enabled": False, "kind": "open"}}
    cookies = request.cookies
    if studio_access.verify_internal_cookie(cookies.get(studio_access.INTERNAL_COOKIE_NAME)):
        return {"ok": True, "data": {"enabled": True, "kind": "internal"}}
    phone = studio_access.read_client_cookie(cookies.get(studio_access.CLIENT_COOKIE_NAME))
    if phone:
        return {"ok": True, "data": {"enabled": True, "kind": "client", "phone": phone}}
    return {"ok": True, "data": {"enabled": True, "kind": "none"}}


@router.post("/access/logout")
async def logout(request: Request):
    """Clear the studio and internal cookies (best effort, always succeeds)."""
    response = JSONResponse({"ok": True})
    _clear_scoped_cookie(response, name=studio_access.CLIENT_COOKIE_NAME, request=request)
    _clear_scoped_cookie(response, name=studio_access.INTERNAL_COOKIE_NAME, request=request)
    return response


@router.get("/access/config")
async def read_access_config(request: Request):
    """Masked gate settings for the internal console (never returns plaintext)."""
    if _rate_limited(request, _RATE_SCOPE_SETTINGS):
        return _rate_limited_response()
    denied = _settings_access_error(request)
    if denied is not None:
        _note_failure(request, _RATE_SCOPE_SETTINGS)
        return denied
    return {"ok": True, "data": studio_access.settings_status()}


@router.post("/access/config")
async def update_access_config(body: AccessConfigBody, request: Request):
    """Rotate the client secret and/or the internal key (hot, no restart)."""
    if _rate_limited(request, _RATE_SCOPE_SETTINGS):
        return _rate_limited_response()
    denied = _settings_access_error(request)
    if denied is not None:
        _note_failure(request, _RATE_SCOPE_SETTINGS)
        return denied

    updates: dict[str, str] = {}
    if body.client_secret.strip():
        updates["client_secret"] = body.client_secret
    if body.internal_key.strip():
        updates["internal_key"] = body.internal_key
    if not updates:
        return _error(400, "没有需要保存的修改")

    try:
        status = studio_access.update_settings(updates)
    except ValueError as exc:
        _note_failure(request, _RATE_SCOPE_SETTINGS)
        return _error(400, str(exc))
    except OSError:
        logger.exception("studio access settings write failed")
        return _error(500, "写入配置文件失败，请检查 /data 目录权限")

    notices: list[str] = []
    if "client_secret" in updates:
        notices.append("客户密钥已更新：此前发出的所有客户链接立即失效，请重新发送。")
    if "internal_key" in updates:
        notices.append("内部口令已更新：其他设备上的内部登录已失效，本页面已自动续期。")

    response = JSONResponse({"ok": True, "data": status, "notices": notices})
    if "internal_key" in updates:
        # Re-issue the caller's own session under the new key so rotating a
        # password does not immediately log the operator out.
        _set_scoped_cookie(
            response,
            name=studio_access.INTERNAL_COOKIE_NAME,
            value=studio_access.create_internal_cookie(),
            max_age=studio_access.INTERNAL_SESSION_TTL_SECONDS,
            request=request,
        )
    _clear_failures(request, _RATE_SCOPE_SETTINGS)
    logger.info("studio access settings updated: %s", ", ".join(sorted(updates)))
    return response


@router.post("/access/config/link")
async def create_access_link(body: AccessLinkBody, request: Request):
    """Build the /studio link for one phone number (internal console only)."""
    if _rate_limited(request, _RATE_SCOPE_SETTINGS):
        return _rate_limited_response()
    denied = _settings_access_error(request)
    if denied is not None:
        _note_failure(request, _RATE_SCOPE_SETTINGS)
        return denied
    if not studio_access.enabled():
        return _error(404, "studio access is not enabled")
    phone = studio_access.normalize_phone(body.phone)
    if not phone:
        return _error(400, "手机号需为 11 位数字")
    link = _client_link(request, phone)
    logger.info("studio link generated (phone ****%s)", phone[-4:])
    return {"ok": True, "data": {"phone": phone, "code": studio_access.expected_code(phone), "link": link}}


@router.post("/access/gate")
async def set_access_gate(body: AccessGateBody, request: Request):
    """Turn verification off (or back on) without dropping the configured keys."""
    if _rate_limited(request, _RATE_SCOPE_SETTINGS):
        return _rate_limited_response()
    denied = _settings_access_error(request)
    if denied is not None:
        _note_failure(request, _RATE_SCOPE_SETTINGS)
        return denied

    try:
        if body.enabled:
            updates: dict[str, str] = {}
            if body.client_secret.strip():
                updates["client_secret"] = body.client_secret
            if body.internal_key.strip():
                updates["internal_key"] = body.internal_key
            if updates:
                studio_access.update_settings(updates)
            if not studio_access.client_secret():
                return _error(400, "开启访问验证前需要先设置客户密钥")
            status = studio_access.set_gate_enabled(True)
            notices = ["访问验证已重新开启，原有的客户链接继续有效。"]
        else:
            if not _verify_confirm_key(request, body.confirm_key):
                _note_failure(request, _RATE_SCOPE_SETTINGS)
                return _error(400, "请输入当前内部口令以确认关闭")
            status = studio_access.set_gate_enabled(False)
            notices = ["访问验证已关闭：任何人都能看到全部项目，内部口令与客户密钥已保留。"]
    except ValueError as exc:
        _note_failure(request, _RATE_SCOPE_SETTINGS)
        return _error(400, str(exc))
    except OSError:
        logger.exception("studio access gate toggle failed")
        return _error(500, "写入配置文件失败，请检查 /data 目录权限")

    _clear_failures(request, _RATE_SCOPE_SETTINGS)
    logger.warning("studio access gate toggled to %s", "on" if body.enabled else "off")
    return {"ok": True, "data": status, "notices": notices}
