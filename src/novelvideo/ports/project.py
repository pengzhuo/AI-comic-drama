"""Project DTOs and access ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import HTTPException

PROJECT_ROLE_VIEWER = "viewer"
PROJECT_ROLE_EDITOR = "editor"
PROJECT_ROLE_ADMIN = "admin"
PROJECT_ROLE_OWNER = "owner"

# Owner namespaces. ``user``/``local`` is the single-user CE identity; studio
# link holders are scoped per phone number under ``client``. Keeping the
# mapping here (rather than in a provider) lets the registry, the access port
# and the request identity all agree on one spelling.
OWNER_TYPE_USER = "user"
OWNER_TYPE_CLIENT = "client"
LOCAL_USER_ID = "local"
ANONYMOUS_USER_ID = "anonymous"
CLIENT_USER_PREFIX = "client:"


def client_user_id(phone: str) -> str:
    return f"{CLIENT_USER_PREFIX}{phone}"


def is_client_user_id(user_id: str | None) -> bool:
    return str(user_id or "").startswith(CLIENT_USER_PREFIX)


def phone_from_client_user_id(user_id: str | None) -> str:
    text = str(user_id or "")
    return text[len(CLIENT_USER_PREFIX) :] if text.startswith(CLIENT_USER_PREFIX) else ""


def owner_pair_from_user_id(user_id: str | None) -> tuple[str, str] | None:
    """Map an API user id to the ``(owner_type, owner_id)`` it owns.

    ``local`` -> ``("user", "local")``; ``client:<phone>`` ->
    ``("client", "<phone>")``; anything else (``anonymous``, missing) -> ``None``
    because such a requester owns nothing.
    """
    text = str(user_id or "").strip()
    if not text:
        return None
    if text == LOCAL_USER_ID:
        return (OWNER_TYPE_USER, LOCAL_USER_ID)
    if text.startswith(CLIENT_USER_PREFIX):
        phone = text[len(CLIENT_USER_PREFIX) :]
        return (OWNER_TYPE_CLIENT, phone) if phone else None
    return None


def is_scoped_user_id(user_id: str | None) -> bool:
    """Whether the requester is bound to its own projects (client/anonymous).

    Used to answer 404 instead of 403 for foreign projects so their existence
    is not disclosed to link holders.
    """
    text = str(user_id or "").strip()
    return text == ANONYMOUS_USER_ID or is_client_user_id(text)

ROLE_ORDER = {
    PROJECT_ROLE_VIEWER: 10,
    PROJECT_ROLE_EDITOR: 20,
    PROJECT_ROLE_ADMIN: 30,
    PROJECT_ROLE_OWNER: 40,
}


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    owner_type: str
    owner_id: str
    owner_username: str
    name: str
    home_node_id: str
    output_dir: str
    state_dir: str
    runtime_dir: str
    status: str
    created_at: str = ""
    updated_at: str = ""
    purged_at: str | None = None
    storage_org_id: str | None = None
    storage_org_name: str | None = None


@dataclass(frozen=True)
class Principal:
    type: str
    id: str


def role_allows(actual: str, required: str) -> bool:
    return ROLE_ORDER.get(actual, 0) >= ROLE_ORDER.get(required, 0)


def require_role_value(actual: str | None, required: str) -> None:
    if not actual or not role_allows(actual, required):
        raise HTTPException(status_code=403, detail=f"project role required: {required}")


class ProjectRegistry(Protocol):
    async def get_project(self, project_id: str) -> ProjectRecord | None: ...

    async def get_project_by_owner_name(
        self,
        owner_user_id: str,
        name: str,
    ) -> ProjectRecord | None: ...

    async def create_project(
        self,
        *,
        owner_user_id: str,
        owner_username: str,
        name: str,
        home_node_id: str | None = None,
        output_dir: str | None = None,
        state_dir: str | None = None,
        runtime_dir: str | None = None,
    ) -> ProjectRecord: ...

    async def list_accessible_projects(
        self,
        principals: list[tuple[str, str]],
    ) -> list[ProjectRecord]: ...

    async def update_project_status(
        self,
        project_id: str,
        status: str,
    ) -> ProjectRecord | None: ...

    async def mark_project_purged(self, project_id: str) -> ProjectRecord | None: ...

    async def delete_uncommitted_project(self, project_id: str) -> None: ...

    async def delete_project_home(self, project_id: str) -> None: ...

    async def resolve_username_by_user_id(self, user_id: str) -> str | None: ...

    async def resolve_user_id_by_username(self, username: str) -> str | None: ...


class ProjectAccess(Protocol):
    """Authoritative project authorization.

    Providers may additionally expose ``effective_media_read_role(project,
    principals)`` returning only viewer/None. Media handlers opt into that
    optional cached capability; older providers fall back to the methods here.
    It must never be used to authorize mutations or background work.
    """

    async def resolve_requester_principals(self, user_id: str) -> list[Principal]: ...

    async def effective_project_role(
        self,
        project: ProjectRecord,
        principals: list[Principal],
    ) -> str | None: ...

    async def effective_project_role_by_id(
        self,
        project_id: str,
        principals: list[Principal],
    ) -> str | None: ...

    async def count_project_task_eligible_users(
        self,
        *,
        project_id: str,
        owner_type: str,
        owner_id: str,
    ) -> int: ...
