"""Studio access gate: code links, signed cookies and deny-by-default identity."""

from __future__ import annotations

import hashlib
import time

import pytest
from starlette.requests import Request

from novelvideo import studio_access
from novelvideo.api.routes import access as access_routes

PHONE = "13800000000"
PHONE_B = "13900000000"
SECRET = "test-client-secret"
INTERNAL_KEY = "0123456789abcdef0123456789abcdef"


def _write_secret(tmp_path, *, client_secret: str = SECRET, internal_key: str = INTERNAL_KEY):
    path = tmp_path / "studio-access.secret"
    lines = []
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
    monkeypatch.setenv("STUDIO_ACCESS_SECRET_FILE", str(tmp_path / "studio-access.secret"))
    monkeypatch.setattr(studio_access, "_secret_cache", None)
    return tmp_path


def _request(cookies: dict[str, str] | None = None, *, scheme: str = "http", method: str = "GET"):
    headers: list[tuple[bytes, bytes]] = []
    if cookies:
        cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
        headers.append((b"cookie", cookie_header.encode("utf-8")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": "/api/v1/access/session",
        "raw_path": b"/api/v1/access/session",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("203.0.113.5", 4321),
    }
    return Request(scope)


def _code(phone: str = PHONE, secret: str = SECRET) -> str:
    return hashlib.md5(f"{phone}{secret}".encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# code / secret handling
# --------------------------------------------------------------------------- #


def test_gate_disabled_without_secret(gate_env):
    assert studio_access.enabled() is False
    assert studio_access.resolve_identity({}) is None
    assert studio_access.verify_code(PHONE, _code()) is False


def test_code_is_md5_of_phone_plus_secret(gate_env):
    _write_secret(gate_env)

    assert studio_access.enabled() is True
    assert studio_access.expected_code(PHONE) == _code()
    assert studio_access.verify_code(PHONE, _code()) is True
    # Case-insensitive hex.
    assert studio_access.verify_code(PHONE, _code().upper()) is True
    assert studio_access.verify_code(f" {PHONE} ", _code()) is True
    assert studio_access.verify_code(PHONE, "0" * 32) is False
    assert studio_access.verify_code(PHONE_B, _code()) is False
    assert studio_access.verify_code(PHONE, "not-a-hash") is False
    assert studio_access.verify_code(PHONE, "") is False


def test_phone_normalization():
    assert studio_access.normalize_phone("13800000000") == PHONE
    assert studio_access.normalize_phone("138 0000 0000") == PHONE
    assert studio_access.normalize_phone("138-0000-0000") == PHONE
    assert studio_access.normalize_phone("+8613800000000") == PHONE
    assert studio_access.normalize_phone("1380000000") == ""
    assert studio_access.normalize_phone("138000000000") == ""
    assert studio_access.normalize_phone("abc") == ""


def test_env_secret_overrides_file(gate_env, monkeypatch):
    _write_secret(gate_env, client_secret="file-secret")
    monkeypatch.setenv("STUDIO_ACCESS_SECRET", "env-secret")

    assert studio_access.client_secret() == "env-secret"


def test_secret_file_change_is_picked_up(gate_env):
    _write_secret(gate_env, client_secret="first-secret")
    assert studio_access.client_secret() == "first-secret"

    # A different content size guarantees the (path, mtime, size) stamp changes
    # even on coarse filesystems.
    _write_secret(gate_env, client_secret="second-secret-longer")
    assert studio_access.client_secret() == "second-secret-longer"


# --------------------------------------------------------------------------- #
# cookies
# --------------------------------------------------------------------------- #


def test_client_cookie_roundtrip_and_tamper(gate_env):
    _write_secret(gate_env)

    value = studio_access.create_client_cookie(PHONE)
    assert studio_access.read_client_cookie(value) == PHONE

    phone, exp, signature = value.split(".")
    assert phone == PHONE
    assert studio_access.read_client_cookie(f"{PHONE_B}.{exp}.{signature}") == ""
    assert studio_access.read_client_cookie(f"{PHONE}.{exp}.{'0' * len(signature)}") == ""
    assert studio_access.read_client_cookie(f"{PHONE}.{int(exp) + 1}.{signature}") == ""
    assert studio_access.read_client_cookie("garbage") == ""
    assert studio_access.read_client_cookie("") == ""


def test_client_cookie_expires(gate_env, monkeypatch):
    _write_secret(gate_env)
    value = studio_access.create_client_cookie(PHONE)

    monkeypatch.setattr(
        studio_access,
        "_now",
        lambda: int(time.time()) + studio_access.CLIENT_SESSION_TTL_SECONDS + 1,
    )
    assert studio_access.read_client_cookie(value) == ""


def test_secret_rotation_invalidates_existing_sessions(gate_env, monkeypatch):
    _write_secret(gate_env)
    value = studio_access.create_client_cookie(PHONE)
    assert studio_access.read_client_cookie(value) == PHONE

    _write_secret(gate_env, client_secret="another-secret-entirely")
    monkeypatch.setattr(studio_access, "_secret_cache", None)
    assert studio_access.read_client_cookie(value) == ""


def test_internal_key_and_cookie(gate_env, monkeypatch):
    _write_secret(gate_env)

    assert studio_access.verify_internal_key(INTERNAL_KEY) is True
    assert studio_access.verify_internal_key("wrong") is False
    assert studio_access.verify_internal_key("") is False

    value = studio_access.create_internal_cookie()
    assert studio_access.verify_internal_cookie(value) is True
    assert studio_access.verify_internal_cookie("garbage") is False

    exp, _, signature = value.partition(".")
    assert studio_access.verify_internal_cookie(f"{exp}.{'0' * len(signature)}") is False

    monkeypatch.setattr(
        studio_access,
        "_now",
        lambda: int(time.time()) + studio_access.INTERNAL_SESSION_TTL_SECONDS + 1,
    )
    assert studio_access.verify_internal_cookie(value) is False


def test_resolve_identity_precedence(gate_env):
    _write_secret(gate_env)

    client_cookie = studio_access.create_client_cookie(PHONE)
    internal_cookie = studio_access.create_internal_cookie()

    assert studio_access.resolve_identity({}) == studio_access.anonymous_identity()
    assert studio_access.resolve_identity({studio_access.CLIENT_COOKIE_NAME: "bogus"}) == (
        studio_access.anonymous_identity()
    )

    client = studio_access.resolve_identity({studio_access.CLIENT_COOKIE_NAME: client_cookie})
    assert client is not None
    assert client["user_id"] == f"client:{PHONE}"
    assert client["username"] == PHONE

    # Internal wins when both cookies are present.
    internal = studio_access.resolve_identity(
        {
            studio_access.CLIENT_COOKIE_NAME: client_cookie,
            studio_access.INTERNAL_COOKIE_NAME: internal_cookie,
        }
    )
    assert internal is not None
    assert internal["user_id"] == "local"


def test_identity_user_ids_map_to_owners():
    from novelvideo.ports.project import is_scoped_user_id, owner_pair_from_user_id

    assert owner_pair_from_user_id("local") == ("user", "local")
    assert owner_pair_from_user_id(f"client:{PHONE}") == ("client", PHONE)
    assert owner_pair_from_user_id("anonymous") is None
    assert owner_pair_from_user_id("") is None

    assert is_scoped_user_id("anonymous") is True
    assert is_scoped_user_id(f"client:{PHONE}") is True
    assert is_scoped_user_id("local") is False
    assert is_scoped_user_id("") is False


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_client_session_endpoint_sets_http_only_cookie(gate_env):
    _write_secret(gate_env)
    body = access_routes.ClientSessionBody(phone=PHONE, code=_code())

    response = await access_routes.create_client_session(body, _request())

    assert response.status_code == 200
    header = response.headers["set-cookie"]
    assert f"{studio_access.CLIENT_COOKIE_NAME}=" in header
    assert "HttpOnly" in header
    assert "Path=/" in header
    # Plain-http request: a Secure cookie would be dropped by the browser.
    assert "Secure" not in header


@pytest.mark.asyncio
async def test_client_session_endpoint_marks_secure_over_https(gate_env):
    _write_secret(gate_env)
    body = access_routes.ClientSessionBody(phone=PHONE, code=_code())

    response = await access_routes.create_client_session(body, _request(scheme="https"))

    assert "Secure" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_client_session_rejects_bad_code_and_disabled_gate(gate_env):
    _write_secret(gate_env)
    bad = await access_routes.create_client_session(
        access_routes.ClientSessionBody(phone=PHONE, code="0" * 32), _request()
    )
    assert bad.status_code == 400
    assert "set-cookie" not in bad.headers

    bad_phone = await access_routes.create_client_session(
        access_routes.ClientSessionBody(phone="123", code=_code()), _request()
    )
    assert bad_phone.status_code == 400

    # Removing the secret file turns the gate off again.
    (gate_env / "studio-access.secret").unlink()
    disabled = await access_routes.create_client_session(
        access_routes.ClientSessionBody(phone=PHONE, code=_code()), _request()
    )
    assert disabled.status_code == 404


@pytest.mark.asyncio
async def test_internal_session_endpoint(gate_env):
    _write_secret(gate_env)

    wrong = await access_routes.create_internal_session(
        access_routes.InternalSessionBody(key="nope"), _request()
    )
    assert wrong.status_code == 400

    ok = await access_routes.create_internal_session(
        access_routes.InternalSessionBody(key=INTERNAL_KEY), _request()
    )
    assert ok.status_code == 200
    assert f"{studio_access.INTERNAL_COOKIE_NAME}=" in ok.headers["set-cookie"]


@pytest.mark.asyncio
async def test_session_probe_and_logout(gate_env):
    _write_secret(gate_env)

    anonymous = await access_routes.read_access_session(_request())
    assert anonymous["data"] == {"enabled": True, "kind": "none"}

    client = await access_routes.read_access_session(
        _request({studio_access.CLIENT_COOKIE_NAME: studio_access.create_client_cookie(PHONE)})
    )
    assert client["data"] == {"enabled": True, "kind": "client", "phone": PHONE}

    internal = await access_routes.read_access_session(
        _request({studio_access.INTERNAL_COOKIE_NAME: studio_access.create_internal_cookie()})
    )
    assert internal["data"] == {"enabled": True, "kind": "internal"}

    logged_out = await access_routes.logout(_request())
    cookies = logged_out.headers.getlist("set-cookie")
    assert any(studio_access.CLIENT_COOKIE_NAME in value for value in cookies)
    assert any(studio_access.INTERNAL_COOKIE_NAME in value for value in cookies)
