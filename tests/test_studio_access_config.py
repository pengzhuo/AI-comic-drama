"""Studio access settings page backend: masked reads, hot writes, gate toggle.

The console edits ``/data/studio-access.secret`` through these endpoints, so the
tests here pin the safety properties an operator relies on: reads never leak a
plaintext secret, writes are atomic and keep the file owner-only, rotating a key
invalidates what depended on it, turning the gate off keeps the keys, and the
public endpoints are rate limited.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys

import pytest
from starlette.requests import Request

from novelvideo import studio_access
from novelvideo.api.routes import access as access_routes
from novelvideo.api.routes import auth as auth_routes

PHONE = "13800000000"
SECRET = "test-client-secret"
INTERNAL_KEY = "0123456789abcdef0123456789abcdef"
NEW_INTERNAL_KEY = "zyxwvutsrqponmlkjihgfedcba987654"


def _secret_path(tmp_path):
    return tmp_path / "studio-access.secret"


def _write_secret(
    path,
    *,
    client_secret: str = SECRET,
    internal_key: str = INTERNAL_KEY,
    comments: bool = True,
):
    lines = []
    if comments:
        lines.append("# Studio access gate settings")
        lines.append("# keep me")
    if client_secret:
        lines.append(f"client_secret={client_secret}")
    if internal_key:
        lines.append(f"internal_key={internal_key}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def gate_env(monkeypatch, tmp_path):
    monkeypatch.delenv("STUDIO_ACCESS_SECRET", raising=False)
    monkeypatch.delenv("STUDIO_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("STUDIO_BASE_URL", raising=False)
    monkeypatch.setenv("STUDIO_ACCESS_SECRET_FILE", str(_secret_path(tmp_path)))
    _reset_cache()
    with access_routes._rate_lock:
        access_routes._rate_failures.clear()
    yield tmp_path
    _reset_cache()
    with access_routes._rate_lock:
        access_routes._rate_failures.clear()


def _reset_cache() -> None:
    studio_access._secret_cache = None


def _internal_cookies() -> dict[str, str]:
    return {studio_access.INTERNAL_COOKIE_NAME: studio_access.create_internal_cookie()}


def _request(
    cookies: dict[str, str] | None = None,
    *,
    extra_headers: dict[str, str] | None = None,
    client: tuple[str, int] = ("203.0.113.5", 4321),
):
    headers: list[tuple[bytes, bytes]] = []
    if cookies:
        cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
        headers.append((b"cookie", cookie_header.encode("utf-8")))
    for name, value in (extra_headers or {}).items():
        headers.append((name.lower().encode("utf-8"), str(value).encode("utf-8")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/access/config",
        "raw_path": b"/api/v1/access/config",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "server": ("testserver", 80),
        "client": client,
    }
    return Request(scope)


def _code(phone: str = PHONE, secret: str = SECRET) -> str:
    return hashlib.md5(f"{phone}{secret}".encode("utf-8")).hexdigest()


def _cookie_value(set_cookie_header: str, name: str) -> str:
    prefix = f"{name}="
    assert set_cookie_header.startswith(prefix), set_cookie_header
    return set_cookie_header[len(prefix) :].split(";")[0]


def _body(response) -> dict:
    """Route handlers return either a JSONResponse or a plain dict on success."""
    if isinstance(response, dict):
        return response
    return json.loads(response.body)


def _status(response) -> int:
    return getattr(response, "status_code", 200)


# --------------------------------------------------------------------------- #
# masked reads
# --------------------------------------------------------------------------- #


def test_settings_status_masks_secrets(gate_env):
    _write_secret(_secret_path(gate_env))

    status = studio_access.settings_status()

    assert status["enabled"] is True
    assert status["gate_disabled"] is False
    assert status["client_secret"]["mask"] == "test…cret"
    assert status["internal_key"]["mask"] == "0123…cdef"
    assert status["client_secret"]["source"] == "file"
    assert status["internal_key"]["source"] == "file"
    assert status["client_secret"]["env_locked"] is False
    assert status["file"]["exists"] is True
    assert status["file"]["updated_at"]

    dumped = json.dumps(status, ensure_ascii=False)
    assert SECRET not in dumped
    assert INTERNAL_KEY not in dumped


def test_mask_secret_edge_cases():
    assert studio_access.mask_secret("") == ""
    assert studio_access.mask_secret("short") == "…"
    assert studio_access.mask_secret("12345678") == "…"
    assert studio_access.mask_secret("123456789") == "1234…6789"


def test_settings_status_reports_env_source(gate_env, monkeypatch):
    monkeypatch.setenv("STUDIO_INTERNAL_KEY", "env-internal-key")
    _reset_cache()

    status = studio_access.settings_status()

    assert status["internal_key"]["source"] == "env"
    assert status["internal_key"]["env_locked"] is True
    assert studio_access.env_locked_keys() == ["internal_key"]


def test_generate_internal_key_is_32_urlsafe_chars():
    key = studio_access.generate_internal_key()

    assert len(key) == 32
    assert key.isascii()
    assert not set(key) & set("=/+")
    assert key != studio_access.generate_internal_key()


# --------------------------------------------------------------------------- #
# hot writes
# --------------------------------------------------------------------------- #


def test_update_settings_rotates_client_secret(gate_env):
    path = _write_secret(_secret_path(gate_env))
    old_cookie = studio_access.create_client_cookie(PHONE)
    old_code = _code()

    status = studio_access.update_settings({"client_secret": "brand-new-secret"})

    assert status["enabled"] is True
    assert status["client_secret"]["mask"] == "bran…cret"
    # Old links and old cookies are dead, the new code works.
    assert studio_access.verify_code(PHONE, old_code) is False
    assert studio_access.read_client_cookie(old_cookie) == ""
    assert studio_access.verify_code(PHONE, studio_access.expected_code(PHONE)) is True
    assert studio_access.expected_code(PHONE) != old_code

    text = path.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert text.count("client_secret=") == 1
    assert "brand-new-secret" in text
    assert INTERNAL_KEY in text


def test_update_settings_rotates_internal_key(gate_env):
    path = _write_secret(_secret_path(gate_env))
    old_cookie = studio_access.create_internal_cookie()

    studio_access.update_settings({"internal_key": NEW_INTERNAL_KEY})

    assert studio_access.verify_internal_key(INTERNAL_KEY) is False
    assert studio_access.verify_internal_key(NEW_INTERNAL_KEY) is True
    assert studio_access.verify_internal_cookie(old_cookie) is False
    # The client side is untouched by an internal rotation.
    assert studio_access.client_secret() == SECRET
    assert studio_access.read_client_cookie(studio_access.create_client_cookie(PHONE)) == PHONE
    assert INTERNAL_KEY not in path.read_text(encoding="utf-8")


def test_update_settings_creates_file_from_header_template(gate_env):
    status = studio_access.update_settings({"client_secret": SECRET, "internal_key": INTERNAL_KEY})

    path = _secret_path(gate_env)
    text = path.read_text(encoding="utf-8")
    assert status["enabled"] is True
    assert text.startswith("# Studio access gate settings")
    assert f"client_secret={SECRET}" in text
    assert f"internal_key={INTERNAL_KEY}" in text
    # Atomic write leaves no temp file behind.
    assert list(gate_env.glob(".*.tmp")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_update_settings_keeps_file_owner_only(gate_env):
    path = _write_secret(_secret_path(gate_env))
    os.chmod(path, 0o644)

    studio_access.update_settings({"internal_key": NEW_INTERNAL_KEY})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_update_settings_refuses_env_locked_key(gate_env, monkeypatch):
    _write_secret(_secret_path(gate_env))
    monkeypatch.setenv("STUDIO_ACCESS_SECRET", "env-secret")
    _reset_cache()

    with pytest.raises(ValueError) as excinfo:
        studio_access.update_settings({"client_secret": "nope"})

    assert "STUDIO_ACCESS_SECRET" in str(excinfo.value)
    assert (gate_env / "studio-access.secret").read_text(encoding="utf-8").count("client_secret=") == 1


def test_update_settings_rejects_bad_values(gate_env):
    _write_secret(_secret_path(gate_env))

    with pytest.raises(ValueError):
        studio_access.update_settings({"client_secret": "   "})
    with pytest.raises(ValueError):
        studio_access.update_settings({"internal_key": "line\nbreak"})
    with pytest.raises(ValueError):
        studio_access.update_settings({"internal_key": "x" * 300})
    with pytest.raises(ValueError):
        studio_access.update_settings({"nope": "x"})

    assert studio_access.client_secret() == SECRET


# --------------------------------------------------------------------------- #
# gate flag
# --------------------------------------------------------------------------- #


def test_gate_off_keeps_keys_and_reenables_the_same_links(gate_env):
    path = _write_secret(_secret_path(gate_env))
    client_cookie = studio_access.create_client_cookie(PHONE)
    internal_cookie = studio_access.create_internal_cookie()

    off = studio_access.set_gate_enabled(False)

    assert off["enabled"] is False
    assert studio_access.gate_disabled() is True
    assert studio_access.enabled() is False
    # Legacy single-user behavior is back...
    assert studio_access.resolve_identity({}) is None
    assert studio_access.resolve_identity({studio_access.CLIENT_COOKIE_NAME: client_cookie}) is None
    # ...but nothing was thrown away.
    assert studio_access.client_secret() == SECRET
    assert studio_access.internal_key() == INTERNAL_KEY
    assert studio_access.verify_code(PHONE, _code()) is True
    # The operator's own console session survives, which is what lets the
    # console page flip the gate back on.
    assert studio_access.verify_internal_cookie(internal_cookie) is True
    assert "gate=off" in path.read_text(encoding="utf-8")

    on = studio_access.set_gate_enabled(True)

    assert on["enabled"] is True
    assert studio_access.gate_disabled() is False
    assert studio_access.enabled() is True
    assert studio_access.read_client_cookie(client_cookie) == PHONE
    assert "gate=on" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# config endpoints
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_config_read_requires_internal_session(gate_env):
    _write_secret(_secret_path(gate_env))

    anonymous = await access_routes.read_access_config(_request())
    assert anonymous.status_code == 403
    assert _body(anonymous)["ok"] is False

    internal = await access_routes.read_access_config(_request(_internal_cookies()))
    assert internal["ok"] is True
    assert internal["data"]["enabled"] is True
    assert SECRET not in json.dumps(internal["data"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_config_read_404_when_gate_off_except_for_the_operator(gate_env):
    _write_secret(_secret_path(gate_env))
    studio_access.set_gate_enabled(False)

    stranger = await access_routes.read_access_config(_request())
    assert stranger.status_code == 404

    operator = await access_routes.read_access_config(_request(_internal_cookies()))
    assert operator["ok"] is True
    assert operator["data"]["enabled"] is False
    assert operator["data"]["gate_disabled"] is True


@pytest.mark.asyncio
async def test_config_update_rotates_client_secret_and_warns(gate_env):
    _write_secret(_secret_path(gate_env))
    old_cookie = studio_access.create_client_cookie(PHONE)

    response = await access_routes.update_access_config(
        access_routes.AccessConfigBody(client_secret="rotated-secret-2026"),
        _request(_internal_cookies()),
    )

    assert response.status_code == 200
    payload = _body(response)
    assert payload["data"]["enabled"] is True
    assert payload["data"]["client_secret"]["mask"] == "rota…2026"
    assert any("失效" in notice for notice in payload["notices"])
    # The internal key was not touched, so the session cookie is not reissued.
    assert "set-cookie" not in response.headers
    assert studio_access.read_client_cookie(old_cookie) == ""
    assert studio_access.verify_code(PHONE, studio_access.expected_code(PHONE)) is True


@pytest.mark.asyncio
async def test_config_update_rotates_internal_key_and_reissues_session(gate_env):
    _write_secret(_secret_path(gate_env))
    old_internal = studio_access.create_internal_cookie()

    response = await access_routes.update_access_config(
        access_routes.AccessConfigBody(internal_key=NEW_INTERNAL_KEY),
        _request({studio_access.INTERNAL_COOKIE_NAME: old_internal}),
    )

    assert response.status_code == 200
    payload = _body(response)
    assert payload["data"]["internal_key"]["mask"] == "zyxw…7654"
    assert any("重新进入" in notice or "续期" in notice for notice in payload["notices"])
    reissued = _cookie_value(response.headers["set-cookie"], studio_access.INTERNAL_COOKIE_NAME)
    assert studio_access.verify_internal_cookie(reissued) is True
    assert studio_access.verify_internal_cookie(old_internal) is False
    assert studio_access.verify_internal_key(NEW_INTERNAL_KEY) is True
    assert studio_access.verify_internal_key(INTERNAL_KEY) is False


@pytest.mark.asyncio
async def test_config_update_rejects_anonymous_and_empty_bodies(gate_env):
    _write_secret(_secret_path(gate_env))

    anonymous = await access_routes.update_access_config(
        access_routes.AccessConfigBody(client_secret="new-secret"), _request()
    )
    assert anonymous.status_code == 403
    assert studio_access.client_secret() == SECRET

    empty = await access_routes.update_access_config(
        access_routes.AccessConfigBody(), _request(_internal_cookies())
    )
    assert empty.status_code == 400

    bad_value = await access_routes.update_access_config(
        access_routes.AccessConfigBody(client_secret="  "), _request(_internal_cookies())
    )
    assert bad_value.status_code == 400


# --------------------------------------------------------------------------- #
# gate endpoint
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gate_endpoint_requires_the_current_key_to_disable(gate_env):
    _write_secret(_secret_path(gate_env))
    cookies = _internal_cookies()

    wrong = await access_routes.set_access_gate(
        access_routes.AccessGateBody(enabled=False, confirm_key="not-the-key"), _request(cookies)
    )
    assert wrong.status_code == 400
    assert studio_access.enabled() is True

    disabled = await access_routes.set_access_gate(
        access_routes.AccessGateBody(enabled=False, confirm_key=INTERNAL_KEY), _request(cookies)
    )
    assert _status(disabled) == 200
    assert _body(disabled)["data"]["enabled"] is False
    assert studio_access.enabled() is False
    assert studio_access.internal_key() == INTERNAL_KEY

    enabled = await access_routes.set_access_gate(
        access_routes.AccessGateBody(enabled=True), _request(cookies)
    )
    assert _status(enabled) == 200
    assert _body(enabled)["data"]["enabled"] is True
    assert studio_access.enabled() is True


@pytest.mark.asyncio
async def test_gate_endpoint_needs_a_client_secret_to_enable(gate_env):
    _write_secret(_secret_path(gate_env), client_secret="")
    cookies = _internal_cookies()
    assert studio_access.enabled() is False

    refused = await access_routes.set_access_gate(
        access_routes.AccessGateBody(enabled=True), _request(cookies)
    )
    assert refused.status_code == 400

    accepted = await access_routes.set_access_gate(
        access_routes.AccessGateBody(enabled=True, client_secret="fresh-secret"), _request(cookies)
    )
    assert _status(accepted) == 200
    assert _body(accepted)["data"]["enabled"] is True
    assert studio_access.client_secret() == "fresh-secret"


@pytest.mark.asyncio
async def test_gate_endpoint_rejects_anonymous_callers(gate_env):
    _write_secret(_secret_path(gate_env))

    response = await access_routes.set_access_gate(
        access_routes.AccessGateBody(enabled=False, confirm_key=INTERNAL_KEY), _request()
    )

    assert response.status_code == 403
    assert studio_access.enabled() is True


# --------------------------------------------------------------------------- #
# link generator
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_link_endpoint_builds_absolute_link(gate_env):
    _write_secret(_secret_path(gate_env))
    headers = {"x-forwarded-host": "aistory.nanie168.com", "x-forwarded-proto": "https"}

    response = await access_routes.create_access_link(
        access_routes.AccessLinkBody(phone="138 0000 0000"),
        _request(_internal_cookies(), extra_headers=headers),
    )

    data = response["data"]
    assert data["phone"] == PHONE
    assert data["code"] == _code()
    assert data["link"] == f"https://aistory.nanie168.com/studio?phone={PHONE}&code={data['code']}"
    assert data["canvas_link"] == f"https://aistory.nanie168.com/canvas?phone={PHONE}&code={data['code']}"

    bad_phone = await access_routes.create_access_link(
        access_routes.AccessLinkBody(phone="123"), _request(_internal_cookies())
    )
    assert bad_phone.status_code == 400

    anonymous = await access_routes.create_access_link(
        access_routes.AccessLinkBody(phone=PHONE), _request()
    )
    assert anonymous.status_code == 403


@pytest.mark.asyncio
async def test_link_endpoint_canvas_link_targets_project(gate_env):
    _write_secret(_secret_path(gate_env))

    response = await access_routes.create_access_link(
        access_routes.AccessLinkBody(phone=PHONE, project="01M6GSBBZJ0ZTQ3XNFYSFNCE05"),
        _request(_internal_cookies()),
    )

    data = response["data"]
    assert data["canvas_link"] == (
        f"https://localhost/canvas/01M6GSBBZJ0ZTQ3XNFYSFNCE05"
        f"?phone={PHONE}&code={data['code']}"
    )

    bad_project = await access_routes.create_access_link(
        access_routes.AccessLinkBody(phone=PHONE, project="../etc/passwd"),
        _request(_internal_cookies()),
    )
    assert bad_project.status_code == 400


@pytest.mark.asyncio
async def test_link_endpoint_respects_base_url_override(gate_env, monkeypatch):
    _write_secret(_secret_path(gate_env))
    monkeypatch.setenv("STUDIO_BASE_URL", "https://links.example.com/")

    response = await access_routes.create_access_link(
        access_routes.AccessLinkBody(phone=PHONE), _request(_internal_cookies())
    )

    assert response["data"]["link"].startswith(f"https://links.example.com/studio?phone={PHONE}&code=")


@pytest.mark.asyncio
async def test_link_endpoint_404_when_gate_off(gate_env):
    _write_secret(_secret_path(gate_env))
    studio_access.set_gate_enabled(False)

    response = await access_routes.create_access_link(
        access_routes.AccessLinkBody(phone=PHONE), _request(_internal_cookies())
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# rate limits
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_internal_session_endpoint_rate_limits_failures(gate_env):
    _write_secret(_secret_path(gate_env))

    for _ in range(access_routes._RATE_MAX_FAILURES):
        failed = await access_routes.create_internal_session(
            access_routes.InternalSessionBody(key="wrong"), _request()
        )
        assert failed.status_code == 400

    limited = await access_routes.create_internal_session(
        access_routes.InternalSessionBody(key="wrong"), _request()
    )
    assert limited.status_code == 429
    # Even the right key waits for the window to pass.
    blocked = await access_routes.create_internal_session(
        access_routes.InternalSessionBody(key=INTERNAL_KEY), _request()
    )
    assert blocked.status_code == 429


@pytest.mark.asyncio
async def test_settings_endpoints_rate_limit_strangers(gate_env):
    _write_secret(_secret_path(gate_env))

    for _ in range(access_routes._RATE_MAX_FAILURES):
        denied = await access_routes.read_access_config(_request())
        assert denied.status_code == 403

    assert (await access_routes.read_access_config(_request())).status_code == 429

    # A different caller (IP) is unaffected, and the operator still works.
    other_ip = await access_routes.read_access_config(_request(client=("198.51.100.9", 5000)))
    assert other_ip.status_code == 403
    operator = await access_routes.read_access_config(
        _request(_internal_cookies(), client=("198.51.100.9", 5000))
    )
    assert operator["data"]["enabled"] is True
    assert access_routes._rate_limited(
        _request(_internal_cookies(), extra_headers={"x-forwarded-for": "203.0.113.5, 172.18.0.1"}),
        access_routes._RATE_SCOPE_SETTINGS,
    )


# --------------------------------------------------------------------------- #
# session cookies + logout
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_access_cookies_are_session_scoped(gate_env):
    """关掉浏览器就该失效：访问 cookie 不带 Max-Age/Expires。"""
    _write_secret(_secret_path(gate_env))

    client = await access_routes.create_client_session(
        access_routes.ClientSessionBody(phone=PHONE, code=_code()), _request()
    )
    assert _status(client) == 200
    client_header = client.headers["set-cookie"].lower()
    assert client_header.startswith(f"{studio_access.CLIENT_COOKIE_NAME}=")
    assert "max-age" not in client_header
    assert "expires" not in client_header

    internal = await access_routes.create_internal_session(
        access_routes.InternalSessionBody(key=INTERNAL_KEY),
        _request(client=("203.0.113.6", 4321)),
    )
    assert _status(internal) == 200
    internal_header = internal.headers["set-cookie"].lower()
    assert internal_header.startswith(f"{studio_access.INTERNAL_COOKIE_NAME}=")
    assert "max-age" not in internal_header
    assert "expires" not in internal_header


@pytest.mark.asyncio
async def test_client_session_clears_internal_identity(gate_env):
    """兑换客户链接必须当场清掉内部身份：internal 优先于 client，留着它就等于
    客户链接被忽略（页头回落到用户名 local）。"""
    _write_secret(_secret_path(gate_env))

    response = await access_routes.create_client_session(
        access_routes.ClientSessionBody(phone=PHONE, code=_code()),
        _request(_internal_cookies()),
    )
    assert _status(response) == 200
    headers = response.headers.getlist("set-cookie")
    joined = " ".join(headers).lower()
    assert f"{studio_access.CLIENT_COOKIE_NAME}=" in joined

    cleared = [
        header
        for header in headers
        if header.split("=")[0].strip() == studio_access.INTERNAL_COOKIE_NAME
    ]
    assert len(cleared) == 1, headers
    assert "max-age=0" in cleared[0].lower()


@pytest.mark.asyncio
async def test_logout_clears_studio_access_cookies(gate_env):
    """退出（/auth/logout）必须连访问身份一起清，否则 30 天 cookie 还在。"""
    _write_secret(_secret_path(gate_env))

    response = await auth_routes.logout(_request())
    assert _status(response) == 200
    headers = response.headers.getlist("set-cookie")
    joined = " ".join(headers).lower()
    assert f"{studio_access.CLIENT_COOKIE_NAME}=" in joined
    assert f"{studio_access.INTERNAL_COOKIE_NAME}=" in joined
    # 下发的每个访问 cookie 都是「立即过期」形态。
    wanted = {studio_access.CLIENT_COOKIE_NAME, studio_access.INTERNAL_COOKIE_NAME}
    cleared = [h for h in headers if h.split("=")[0].strip() in wanted]
    assert len(cleared) == 2
    for header in cleared:
        assert "max-age=0" in header.lower()
