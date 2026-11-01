"""True isolation: a studio client only ever sees projects created under its phone."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from novelvideo.ports.local.project import AllowAllProjectAccess, SQLiteProjectRegistry

PHONE_A = "13800000000"
PHONE_B = "13900000000"
ID_A = f"client:{PHONE_A}"
ID_B = f"client:{PHONE_B}"


def _set_roots(monkeypatch, tmp_path):
    import novelvideo.config as config

    output = tmp_path / "output"
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(config, "OUTPUT_DIR", str(output))
    monkeypatch.setattr(config, "STATE_DIR", str(state))
    monkeypatch.setattr(config, "RUNTIME_DIR", str(runtime))
    return output, state, runtime


@pytest.fixture
def registry(monkeypatch, tmp_path):
    _set_roots(monkeypatch, tmp_path)
    return SQLiteProjectRegistry()


@pytest.fixture
def access():
    return AllowAllProjectAccess()


async def _principals(access, user_id):
    principal_list = await access.resolve_requester_principals(user_id)
    return [(p.type, p.id) for p in principal_list]


@pytest.mark.asyncio
async def test_client_projects_are_scoped_per_phone(registry, access):
    first = await registry.create_project(
        owner_user_id=ID_A, owner_username=PHONE_A, name="drama"
    )
    second = await registry.create_project(
        owner_user_id=ID_B, owner_username=PHONE_B, name="drama"
    )

    # The phone is the owner: same project name is fine for two clients, and
    # the storage path is namespaced by the phone.
    assert first.id != second.id
    assert (first.owner_type, first.owner_id) == ("client", PHONE_A)
    assert (second.owner_type, second.owner_id) == ("client", PHONE_B)
    assert f"/{PHONE_A}/" in first.output_dir.replace("\\", "/")

    listed_a = await registry.list_accessible_projects(await _principals(access, ID_A))
    listed_b = await registry.list_accessible_projects(await _principals(access, ID_B))
    assert [record.id for record in listed_a] == [first.id]
    assert [record.id for record in listed_b] == [second.id]

    assert await registry.get_project_by_owner_name(ID_A, "drama") == first
    assert await registry.get_project_by_owner_name(ID_B, "drama") == second
    assert await registry.get_project_by_owner_name("anonymous", "drama") is None


@pytest.mark.asyncio
async def test_internal_sees_every_project_anonymous_sees_none(registry, access):
    client_project = await registry.create_project(
        owner_user_id=ID_A, owner_username=PHONE_A, name="client-drama"
    )
    await registry.create_project(
        owner_user_id="local", owner_username="local", name="internal-drama"
    )

    internal = await registry.list_accessible_projects(await _principals(access, "local"))
    assert {record.name for record in internal} == {"client-drama", "internal-drama"}

    assert await registry.list_accessible_projects(await _principals(access, "anonymous")) == []

    anonymous = await access.resolve_requester_principals("anonymous")
    assert anonymous == []
    record = await registry.get_project(client_project.id)
    assert record is not None
    assert await access.effective_project_role(record, anonymous) is None


@pytest.mark.asyncio
async def test_create_project_rejects_unmappable_owner(registry):
    with pytest.raises(ValueError):
        await registry.create_project(
            owner_user_id="ignored-user-id", owner_username="alice", name="drama"
        )


@pytest.mark.asyncio
async def test_resolve_project_context_hides_foreign_projects(registry, access, monkeypatch):
    from novelvideo import project_context
    from novelvideo import studio_access

    monkeypatch.setattr(project_context, "get_project_registry", lambda: registry)
    monkeypatch.setattr(project_context, "get_project_access", lambda: access)

    project = await registry.create_project(
        owner_user_id=ID_A, owner_username=PHONE_A, name="drama"
    )

    owner_ctx = await project_context.resolve_project_context(
        user={"user_id": ID_A, "username": PHONE_A}, project_id=project.id
    )
    assert owner_ctx.effective_role == "owner"
    assert owner_ctx.owner_username == PHONE_A

    internal_ctx = await project_context.resolve_project_context(
        user={"user_id": "local", "username": "local"}, project_id=project.id
    )
    assert internal_ctx.effective_role == "owner"

    for user in ({"user_id": ID_B, "username": PHONE_B}, studio_access.anonymous_identity()):
        with pytest.raises(HTTPException) as exc:
            await project_context.resolve_project_context(user=user, project_id=project.id)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_projects_and_create_respect_gate(registry, access, monkeypatch):
    from novelvideo.api.routes import projects as projects_route

    monkeypatch.setattr(projects_route, "get_project_registry", lambda: registry)
    monkeypatch.setattr(projects_route, "get_project_access", lambda: access)

    await registry.create_project(owner_user_id=ID_A, owner_username=PHONE_A, name="drama")

    anonymous = await projects_route.list_projects(
        user={"user_id": "anonymous", "username": "anonymous"}
    )
    assert anonymous["data"] == []

    mine = await projects_route.list_projects(user={"user_id": ID_A, "username": PHONE_A})
    assert [row["name"] for row in mine["data"]] == ["drama"]

    everything = await projects_route.list_projects(user={"user_id": "local", "username": "local"})
    assert [row["name"] for row in everything["data"]] == ["drama"]

    with pytest.raises(HTTPException) as exc:
        await projects_route.create_project(
            projects_route.ProjectCreate(name="new_drama"),
            user={"user_id": "anonymous", "username": "anonymous"},
        )
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# request identity wiring
# --------------------------------------------------------------------------- #


def _request(cookies: dict[str, str] | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookies:
        headers.append(
            (
                b"cookie",
                "; ".join(f"{name}={value}" for name, value in cookies.items()).encode("utf-8"),
            )
        )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/projects",
        "raw_path": b"/api/v1/projects",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("203.0.113.5", 4321),
    }
    return Request(scope)


@pytest.fixture
def gate_secret(monkeypatch, tmp_path):
    path = tmp_path / "studio-access.secret"
    path.write_text("client_secret=test-client-secret\ninternal_key=0123456789abcdef\n", encoding="utf-8")
    monkeypatch.delenv("STUDIO_ACCESS_SECRET", raising=False)
    monkeypatch.setenv("STUDIO_ACCESS_SECRET_FILE", str(path))
    from novelvideo import studio_access

    monkeypatch.setattr(studio_access, "_secret_cache", None)
    return studio_access


@pytest.mark.asyncio
async def test_get_api_user_is_anonymous_without_cookie(gate_secret):
    from novelvideo.api.auth import get_api_user

    user = await get_api_user(_request())
    assert user["user_id"] == "anonymous"
    assert user["credential_kind"] == "anonymous"


@pytest.mark.asyncio
async def test_get_api_user_reads_studio_and_internal_cookies(gate_secret):
    from novelvideo.api.auth import get_api_user

    client_cookie = gate_secret.create_client_cookie(PHONE_A)
    user = await get_api_user(_request({gate_secret.CLIENT_COOKIE_NAME: client_cookie}))
    assert user["user_id"] == ID_A
    assert user["username"] == PHONE_A

    internal_cookie = gate_secret.create_internal_cookie()
    internal = await get_api_user(_request({gate_secret.INTERNAL_COOKIE_NAME: internal_cookie}))
    assert internal["user_id"] == "local"


@pytest.mark.asyncio
async def test_get_api_user_keeps_legacy_session_when_gate_is_off(monkeypatch):
    import importlib

    # ``novelvideo.api.auth`` is shadowed by the routes module on the package,
    # so import the real auth module explicitly.
    auth_module = importlib.import_module("novelvideo.api.auth")
    from novelvideo.ports import registry as port_registry

    class _FakeAuthPort:
        async def verify_session(self, raw_cookie):
            return {"id": "local", "user_id": "local", "username": "alice", "role": "owner"}

    monkeypatch.delenv("STUDIO_ACCESS_SECRET", raising=False)
    monkeypatch.setenv("STUDIO_ACCESS_SECRET_FILE", "does-not-exist.secret")
    monkeypatch.setattr(auth_module.studio_access, "_secret_cache", None)
    port_registry.register_port("auth", _FakeAuthPort())

    user = await auth_module.get_api_user(_request())
    assert user["user_id"] == "local"
    assert user["username"] == "alice"
