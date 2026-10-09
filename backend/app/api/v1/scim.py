"""SCIM 2.0 user provisioning (RFC 7643/7644 subset): Users list / get / create / replace / patch / delete.

Authentication: a per-organisation SCIM bearer token (created by an identity administrator, stored hashed).
The token determines the organisation; nothing in the request can choose another one. Provisioned users get
a non-administrative role (roles[].value limited to provisionable roles); deactivation (active=false or
DELETE) disables the membership and ends the user's sessions in that organisation immediately. Users are
never hard-deleted (history and audit references remain). Groups are not implemented.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.api.deps import ContainerDep
from app.domain.tenancy.permissions import PROVISIONABLE_ROLES

router = APIRouter(prefix="/scim/v2", tags=["scim"])
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
USERNAME = re.compile(r"^[A-Za-z0-9._@+-]{3,64}$")


def _scim_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code, {"schemas": [ERROR_SCHEMA], "status": str(status_code), "detail": detail}
    )


async def scim_org(
    container: ContainerDep, request: Request, authorization: Annotated[str | None, Header()] = None
) -> str:
    if not container.settings.feature_scim:
        raise _scim_error(404, "SCIM is disabled")
    bearer = (
        authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
    )
    org = await container.identity.scim_org(bearer)
    if org is None:
        container.audit.record(
            None,
            "scim",
            "scim",
            "auth.scim_failed",
            "authentication",
            result="FAILURE",
            severity="WARNING",
            source="scim",
            ip=request.client.host if request.client else None,
        )
        raise _scim_error(401, "invalid SCIM token")
    return org


ScimOrg = Annotated[str, Depends(scim_org)]


def _user(container: Any, org: str, username: str) -> dict[str, Any]:
    m = container.tenancy.member(org, username)
    return {
        "schemas": [USER_SCHEMA],
        "id": username,
        "userName": username,
        "active": bool(m and m.status == "ACTIVE"),
        "roles": [{"value": m.role, "primary": True}] if m else [],
        "meta": {"resourceType": "User", "created": m.created_at.isoformat() if m and m.created_at else None},
    }


def _role(body: dict[str, Any], current: str | None) -> str:
    roles = body.get("roles") or []
    value = roles[0].get("value") if roles and isinstance(roles[0], dict) else None
    if value is None:
        return current or "read_only"
    if value not in PROVISIONABLE_ROLES:
        raise _scim_error(400, f"role {value} cannot be provisioned through SCIM")
    return str(value)


async def _apply(container: Any, org: str, username: str, active: bool, role: str) -> None:
    t = container.tenancy
    prev = t.member(org, username)
    if prev is not None and prev.role not in PROVISIONABLE_ROLES and role != prev.role:
        raise _scim_error(403, "administrative roles are not managed through SCIM")
    await t.set_member(
        None,
        org,
        username,
        role if prev is None or prev.role in PROVISIONABLE_ROLES else prev.role,
        prev.group_scope if prev else [],
        "ACTIVE" if active else "DISABLED",
        source="scim",
    )
    if not active:
        n = await container.identity.revoke_user(username, "deprovisioned (SCIM)", org_id=org)
        container.audit.record(
            org,
            "scim",
            "scim",
            "user.deprovisioned",
            "user",
            resource_type="user",
            resource_id=username,
            source="scim",
            metadata={"sessions_revoked": n},
        )
    else:
        container.audit.record(
            org,
            "scim",
            "scim",
            "user.provisioned",
            "user",
            resource_type="user",
            resource_id=username,
            source="scim",
            metadata={"role": role},
        )


@router.get("/ServiceProviderConfig")
async def config(_: ScimOrg) -> dict[str, Any]:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{"type": "oauthbearertoken", "name": "Bearer token", "primary": True}],
    }


@router.get("/Users")
async def list_users(
    org: ScimOrg,
    container: ContainerDep,
    filter: str | None = None,
    startIndex: int = 1,  # noqa: N803 - SCIM parameter name
    count: int = 100,
) -> dict[str, Any]:
    members = sorted(m.username for m in container.tenancy.org_members(org))
    if filter:
        m = re.fullmatch(r'\s*userName\s+eq\s+"([^"]{1,64})"\s*', filter)
        if m is None:
            raise _scim_error(400, "only 'userName eq \"...\"' filters are supported")
        members = [u for u in members if u.lower() == m.group(1).lower()]
    start, count = max(1, startIndex), max(0, min(count, 200))
    page = members[start - 1 : start - 1 + count]
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": len(members),
        "startIndex": start,
        "itemsPerPage": len(page),
        "Resources": [_user(container, org, u) for u in page],
    }


@router.get("/Users/{user_id}")
async def get_user(user_id: str, org: ScimOrg, container: ContainerDep) -> dict[str, Any]:
    if container.tenancy.member(org, user_id[:64]) is None:
        raise _scim_error(404, "user not found")
    return _user(container, org, user_id[:64])


@router.post("/Users", status_code=201)
async def create_user(body: dict[str, Any], org: ScimOrg, container: ContainerDep) -> dict[str, Any]:
    username = str(body.get("userName") or "").strip().lower()
    if not USERNAME.match(username):
        raise _scim_error(400, "invalid userName")
    if container.tenancy.member(org, username) is not None:
        raise _scim_error(409, "user already exists")
    from app.domain.tenancy.models import QuotaMode

    limit, mode = container.tenancy.orgs[org].quota("max_users")
    if (
        mode == QuotaMode.REJECT
        and len([m for m in container.tenancy.org_members(org) if m.status == "ACTIVE"]) >= limit
    ):
        raise _scim_error(409, "user quota reached")
    if await container.admin.get_user(username) is None:
        await container.admin.create_external_user(username)
    await _apply(container, org, username, bool(body.get("active", True)), _role(body, None))
    return _user(container, org, username)


@router.put("/Users/{user_id}")
async def replace_user(
    user_id: str, body: dict[str, Any], org: ScimOrg, container: ContainerDep
) -> dict[str, Any]:
    m = container.tenancy.member(org, user_id[:64])
    if m is None:
        raise _scim_error(404, "user not found")
    await _apply(container, org, m.username, bool(body.get("active", True)), _role(body, m.role))
    return _user(container, org, m.username)


@router.patch("/Users/{user_id}")
async def patch_user(
    user_id: str, body: dict[str, Any], org: ScimOrg, container: ContainerDep
) -> dict[str, Any]:
    m = container.tenancy.member(org, user_id[:64])
    if m is None:
        raise _scim_error(404, "user not found")
    active, role = m.status == "ACTIVE", m.role
    for op in body.get("Operations") or []:
        if str(op.get("op", "")).lower() not in ("replace", "add"):
            raise _scim_error(400, "only replace/add operations are supported")
        path, value = op.get("path"), op.get("value")
        if path == "active":
            active = value in (True, "true", "True")
        elif path is None and isinstance(value, dict):
            if "active" in value:
                active = value["active"] in (True, "true", "True")
            if "roles" in value:
                role = _role(value, role)
        elif path == "roles":
            role = _role({"roles": value if isinstance(value, list) else [value]}, role)
        else:
            raise _scim_error(400, f"unsupported path {path}")
    await _apply(container, org, m.username, active, role)
    return _user(container, org, m.username)


@router.delete("/Users/{user_id}", status_code=204)
async def delete_user(user_id: str, org: ScimOrg, container: ContainerDep) -> Response:
    m = container.tenancy.member(org, user_id[:64])
    if m is None:
        raise _scim_error(404, "user not found")
    await _apply(container, org, m.username, False, m.role)
    return Response(status_code=204)
