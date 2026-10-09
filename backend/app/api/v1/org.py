"""Phase-9 organisation administration APIs (tenant-scoped, permission-checked, audited).

The organisation is always the caller's current organisation (resolved from the session and membership);
no route accepts an organisation id from the client except platform-administration routes, which require
``platform.manage``. Resources of another organisation answer 404 like unknown ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.access import context_of, visible_devices
from app.api.deps import ContainerDep, Reader, require_permission, require_recent_auth
from app.core.security import Principal
from app.domain.governance import compliance as comp
from app.domain.governance.policies import KINDS, SCHEMAS, SCOPES
from app.domain.tenancy.models import QUOTA_DEFAULTS, Lifecycle
from app.domain.tenancy.permissions import PERMISSIONS, ROLE_LABELS, ROLES
from app.repositories.governance import AuditFilter, IdentityProvider
from app.services.device_rows import device_facts as _facts
from app.services.device_rows import device_row as _device_row
from app.services.device_rows import load_credentials as _creds
from app.services.identity import IdentityError
from app.services.tenancy import TenancyError

router = APIRouter(prefix="/org", tags=["organization"])
platform_router = APIRouter(prefix="/platform", tags=["platform administration"])


def _err(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


def _t(exc: TenancyError) -> HTTPException:
    return _err(exc.status, exc.code, str(exc))


def _ctx(p: Principal) -> Any:
    return context_of(p)


# ============================================================================ organisation
@router.get("", summary="Current organization, my role, permissions and quotas")
async def current(principal: Reader, container: ContainerDep) -> dict[str, Any]:
    org = container.tenancy.orgs[principal.org_id]
    return {
        "organization": org.public(),
        "role": principal.org_role,
        "platform_admin": principal.platform_admin,
        "permissions": sorted(principal.permissions),
        "roles": [{"role": r, "label": ROLE_LABELS[r], "permissions": sorted(p)} for r, p in ROLES.items()],
    }


class OrgPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=120)


@router.patch("", summary="Rename the organization")
async def rename(
    body: OrgPatch, container: ContainerDep, principal: Principal = require_permission("organization.manage")
) -> dict[str, Any]:
    try:
        o = await container.tenancy.update_org(_ctx(principal), principal.org_id, body.name, None, None)
    except TenancyError as exc:
        raise _t(exc) from exc
    return o.public()


# ----------------------------------------------------------------------------- structure
class UnitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["business_unit", "department", "team"]
    name: str = Field(min_length=1, max_length=120)
    parent_id: str | None = Field(default=None, max_length=36)


@router.get("/units", summary="Business units, departments and teams")
async def units(principal: Reader, container: ContainerDep) -> dict[str, Any]:
    if not principal.can("group.view"):
        raise _err(403, "PERMISSION_DENIED", "Permission group.view required")
    return {"items": [u.public() for u in container.tenancy.units.values() if u.org_id == principal.org_id]}


@router.post("/units", status_code=201, summary="Create a business unit, department or team")
async def create_unit(
    body: UnitIn, container: ContainerDep, principal: Principal = require_permission("group.manage")
) -> dict[str, Any]:
    try:
        return (
            await container.tenancy.create_unit(_ctx(principal), body.kind, body.name, body.parent_id)
        ).public()
    except TenancyError as exc:
        raise _t(exc) from exc


@router.post("/units/{unit_id}/archive", summary="Archive a unit (history kept)")
async def archive_unit(
    unit_id: str, container: ContainerDep, principal: Principal = require_permission("group.manage")
) -> dict[str, Any]:
    try:
        return (await container.tenancy.archive_unit(_ctx(principal), unit_id[:36])).public()
    except TenancyError as exc:
        raise _t(exc) from exc


# ------------------------------------------------------------------------------- groups
class GroupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    kind: Literal[
        "department", "team", "location", "business_unit", "os", "environment", "role", "custom"
    ] = "custom"
    unit_id: str | None = Field(default=None, max_length=36)
    priority: int = Field(default=100, ge=0, le=10_000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    status: Literal["ACTIVE", "ARCHIVED"] = "ACTIVE"


class MembersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add: list[str] = Field(default_factory=list, max_length=500)
    remove: list[str] = Field(default_factory=list, max_length=500)


@router.get("/groups", summary="Device groups with member counts")
async def groups(principal: Reader, container: ContainerDep) -> dict[str, Any]:
    if not principal.can("group.view"):
        raise _err(403, "PERMISSION_DENIED", "Permission group.view required")
    t = container.tenancy
    visible = visible_devices(principal, container)
    out = []
    for g in t.groups.values():
        if g.org_id != principal.org_id:
            continue
        members = sorted(d for d in visible if g.group_id in t.device_groups.get(d, set()))
        out.append(dict(g.public(), devices=members, device_count=len(members)))
    return {"items": sorted(out, key=lambda g: g["name"])}


@router.post("/groups", status_code=201, summary="Create a device group")
async def create_group(
    body: GroupIn, container: ContainerDep, principal: Principal = require_permission("group.manage")
) -> dict[str, Any]:
    try:
        g = await container.tenancy.save_group(
            _ctx(principal), None, body.name, body.kind, body.unit_id, body.priority, body.tags, body.status
        )
    except TenancyError as exc:
        raise _t(exc) from exc
    return g.public()


@router.patch("/groups/{group_id}", summary="Update or archive a device group")
async def update_group(
    group_id: str,
    body: GroupIn,
    container: ContainerDep,
    principal: Principal = require_permission("group.manage"),
) -> dict[str, Any]:
    try:
        g = await container.tenancy.save_group(
            _ctx(principal),
            group_id[:36],
            body.name,
            body.kind,
            body.unit_id,
            body.priority,
            body.tags,
            body.status,
        )
    except TenancyError as exc:
        raise _t(exc) from exc
    container.policies._cache.clear()  # group priority influences policy precedence
    return g.public()


@router.post("/groups/{group_id}/members", summary="Add or remove devices")
async def group_members(
    group_id: str,
    body: MembersIn,
    container: ContainerDep,
    principal: Principal = require_permission("group.manage"),
) -> dict[str, Any]:
    try:
        g = await container.tenancy.set_group_members(
            group_id[:36], body.add, body.remove, principal.subject, _ctx(principal)
        )
    except TenancyError as exc:
        raise _t(exc) from exc
    container.policies._cache.clear()
    return g.public()


# ------------------------------------------------------------------------------- members
class MemberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(max_length=24)
    group_scope: list[str] = Field(default_factory=list, max_length=50)
    status: Literal["ACTIVE", "DISABLED"] = "ACTIVE"


class NewMemberIn(MemberIn):
    username: str = Field(min_length=3, max_length=64)
    password: str | None = Field(
        default=None, max_length=256
    )  # create a local account when it does not exist


@router.get("/members", summary="Members of the organization")
async def members(
    container: ContainerDep, principal: Principal = require_permission("user.view")
) -> dict[str, Any]:
    t = container.tenancy
    out = []
    for m in t.org_members(principal.org_id):
        u = await container.admin.get_user(m.username)
        out.append(
            dict(
                m.public(),
                role_label=ROLE_LABELS.get(m.role, m.role),
                last_login_at=u.last_login_at.isoformat() if u and u.last_login_at else None,
                mfa_enrolled=await container.identity.mfa_enabled(m.username),
            )
        )
    return {"items": sorted(out, key=lambda m: m["username"])}


@router.post("/members", status_code=201, summary="Add a member (existing account, or a new local account)")
async def add_member(
    body: NewMemberIn,
    container: ContainerDep,
    request: Request,
    principal: Principal = require_permission("user.manage"),
) -> dict[str, Any]:
    require_recent_auth(principal, container)
    from app.domain.admin.models import Role
    from app.services.admin import ConflictError

    if await container.admin.get_user(body.username) is None:
        if not body.password:
            raise _err(404, "USER_NOT_FOUND", "No such account; provide a password to create a local account")
        try:
            await container.admin.create_user(body.username, body.password, Role.VIEWER)
        except (ConflictError, ValueError) as exc:
            raise _err(422, "INVALID_USER", str(exc)) from exc
    elif container.tenancy.member(principal.org_id, body.username) is not None:
        raise _err(409, "ALREADY_MEMBER", "Already a member")
    try:
        m = await container.tenancy.set_member(
            _ctx(principal), principal.org_id, body.username, body.role, body.group_scope, body.status
        )
    except TenancyError as exc:
        raise _t(exc) from exc
    return m.public()


@router.put("/members/{username}", summary="Change a member's role, scope or status")
async def update_member(
    username: str, body: MemberIn, container: ContainerDep, principal: Reader
) -> dict[str, Any]:
    t = container.tenancy
    prev = t.member(principal.org_id, username[:64])
    if prev is None:
        raise _err(404, "MEMBER_NOT_FOUND", "Unknown member")
    only_status = prev.role == body.role and prev.group_scope == body.group_scope
    needed = "user.disable" if only_status else "user.manage"
    if not principal.can(needed):
        raise _err(403, "PERMISSION_DENIED", f"Permission {needed} required")
    require_recent_auth(principal, container)
    try:
        m = await t.set_member(
            _ctx(principal), principal.org_id, prev.username, body.role, body.group_scope, body.status
        )
    except TenancyError as exc:
        raise _t(exc) from exc
    if m.status == "DISABLED" or m.role != prev.role or m.group_scope != prev.group_scope:
        await container.identity.revoke_user(m.username, "membership changed", org_id=principal.org_id)
    return m.public()


@router.post(
    "/members/{username}/revoke-sessions", summary="End all sessions of a member in this organization"
)
async def revoke_member_sessions(
    username: str, container: ContainerDep, principal: Principal = require_permission("user.disable")
) -> dict[str, Any]:
    if container.tenancy.member(principal.org_id, username[:64]) is None:
        raise _err(404, "MEMBER_NOT_FOUND", "Unknown member")
    n = await container.identity.revoke_user(
        username[:64], "revoked by administrator", org_id=principal.org_id
    )
    container.audit.record(
        principal.org_id,
        principal.subject,
        "user",
        "auth.sessions_revoked",
        "authentication",
        resource_type="user",
        resource_id=username[:64],
        metadata={"sessions": n},
    )
    return {"revoked": n}


# ============================================================================== devices
@router.get("/devices", summary="Fleet: devices with lifecycle, compliance, groups and agent versions")
async def fleet(
    principal: Reader,
    container: ContainerDep,
    group_id: str | None = Query(default=None, max_length=36),
    unit_id: str | None = Query(default=None, max_length=36),
    lifecycle: str | None = Query(default=None, max_length=24),
    compliance: str | None = Query(default=None, max_length=24),
    health: str | None = Query(default=None, max_length=16),
    agent_version: str | None = Query(default=None, max_length=32),
    os: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    if not principal.can("device.view"):
        raise _err(403, "PERMISSION_DENIED", "Permission device.view required")
    creds = await _creds(container)
    rows = [_device_row(container, d, creds) for d in sorted(visible_devices(principal, container))]
    if unit_id:
        units = {unit_id} | {u.unit_id for u in container.tenancy.units.values() if u.parent_id == unit_id}
        gids = {g.group_id for g in container.tenancy.groups.values() if g.unit_id in units}
        rows = [r for r in rows if gids & set(r["groups"])]
    for key, val in (
        ("lifecycle_display", lifecycle),
        ("compliance", compliance),
        ("health", health),
        ("agent_version", agent_version),
    ):
        if val:
            rows = [r for r in rows if r[key] == val]
    if group_id:
        rows = [r for r in rows if group_id in r["groups"]]
    if os:
        rows = [r for r in rows if os.lower() in (r["os"] or "").lower()]
    return {"items": rows, "count": len(rows)}


@router.get("/devices/{device_id}/compliance", summary="Compliance checks of one device")
async def device_compliance(device_id: str, principal: Reader, container: ContainerDep) -> dict[str, Any]:
    from app.api.access import check_device

    check_device(principal, container, device_id[:64])
    org = principal.org_id
    facts = _facts(container, device_id, await _creds(container))
    return comp.evaluate(
        facts,
        container.policies.effective("agent", org, device_id)[0],
        container.policies.effective("compliance", org, device_id)[0],
    )


class LifecycleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: Literal["ACTIVE", "DISABLED", "QUARANTINED", "REVOKED", "RETIRED"]
    reason: str | None = Field(default=None, max_length=300)


@router.post(
    "/devices/{device_id}/lifecycle", summary="Disable, quarantine, revoke, retire or re-activate a device"
)
async def device_lifecycle(
    device_id: str, body: LifecycleIn, principal: Reader, container: ContainerDep
) -> dict[str, Any]:
    from app.api.access import check_device

    check_device(principal, container, device_id[:64])
    needed = "device.remove" if body.to in ("REVOKED", "RETIRED") else "device.manage"
    if not principal.can(needed):
        raise _err(403, "PERMISSION_DENIED", f"Permission {needed} required")
    if body.to in ("REVOKED", "RETIRED"):
        require_recent_auth(principal, container)
    try:
        rec = await container.tenancy.transition(_ctx(principal), device_id, Lifecycle(body.to), body.reason)
    except TenancyError as exc:
        raise _t(exc) from exc
    if rec.lifecycle in (Lifecycle.REVOKED, Lifecycle.RETIRED, Lifecycle.DECOMMISSIONED):
        await container.device_auth.revoke(device_id)  # the old credential stops working immediately
    return rec.public()


# ---------------------------------------------------------------------- enrollment tokens
class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ttl_hours: float = Field(default=24, gt=0, le=168)
    max_uses: int = Field(default=1, ge=1, le=1000)
    group_id: str | None = Field(default=None, max_length=36)
    label: str = Field(default="", max_length=120)


@router.get("/enrollment-tokens", summary="Enrollment tokens (secrets are never shown again)")
async def tokens(
    container: ContainerDep, principal: Principal = require_permission("device.enroll")
) -> dict[str, Any]:
    return {"items": [t.public() for t in await container.tenancy.repo.tokens_for(principal.org_id)]}


@router.post("/enrollment-tokens", status_code=201, summary="Create an enrollment token (shown once)")
async def create_token(
    body: TokenIn, container: ContainerDep, principal: Principal = require_permission("device.enroll")
) -> dict[str, Any]:
    pol = container.policies.effective("enrollment", principal.org_id)[0]
    try:
        secret, t = await container.tenancy.create_token(
            _ctx(principal),
            body.ttl_hours,
            body.max_uses,
            body.group_id,
            body.label,
            pol["token_max_ttl_hours"],
            pol["allow_multi_use"],
        )
    except TenancyError as exc:
        raise _t(exc) from exc
    return {"token": secret, **t.public(), "note": "Copy the token now; it is stored only as a hash."}


@router.post("/enrollment-tokens/{token_id}/revoke", summary="Revoke an enrollment token")
async def revoke_token(
    token_id: str, container: ContainerDep, principal: Principal = require_permission("device.enroll")
) -> dict[str, Any]:
    try:
        return (await container.tenancy.revoke_token(_ctx(principal), token_id[:36])).public()
    except TenancyError as exc:
        raise _t(exc) from exc


# ============================================================================== policies
class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(max_length=24)
    scope_type: str = Field(max_length=16)
    scope_id: str | None = Field(default=None, max_length=64)
    body: dict[str, Any] = Field(default_factory=dict)
    locked: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=300)
    effective_from: datetime | None = None
    effective_until: datetime | None = None


@router.get("/policies/schema", summary="Policy kinds, fields, defaults and scopes")
async def policy_schema(principal: Reader) -> dict[str, Any]:
    return {
        "kinds": {
            k: {
                n: {
                    "type": f.type,
                    "default": f.default,
                    "min": f.lo,
                    "max": f.hi,
                    "choices": list(f.choices),
                    "doc": f.doc,
                }
                for n, f in SCHEMAS[k].items()
            }
            for k in KINDS
        },
        "scopes": list(SCOPES),
    }


@router.get("/policies", summary="Policies and versions of the organization")
async def list_policies(
    container: ContainerDep,
    kind: str | None = Query(default=None, max_length=24),
    principal: Principal = require_permission("policy.view"),
) -> dict[str, Any]:
    return {"items": [p.public() for p in container.policies.list_policies(principal.org_id, kind)]}


@router.post("/policies", status_code=201, summary="Save a draft (new policy or new version)")
async def save_policy(
    body: PolicyIn, container: ContainerDep, principal: Principal = require_permission("policy.manage")
) -> dict[str, Any]:
    if body.kind == "remediation" and not principal.can("remediation.manage_policy"):
        raise _err(403, "PERMISSION_DENIED", "Permission remediation.manage_policy required")
    if body.kind in ("security", "retention"):
        needed = "security.manage" if body.kind == "security" else "retention.manage"
        if not principal.can(needed):
            raise _err(403, "PERMISSION_DENIED", f"Permission {needed} required")
    try:
        p = await container.policies.save_draft(
            _ctx(principal),
            body.kind,
            body.scope_type,
            body.scope_id or principal.org_id,
            body.body,
            body.locked,
            body.note,
            body.effective_from,
            body.effective_until,
        )
    except TenancyError as exc:
        raise _t(exc) from exc
    return p.public()


@router.post("/policies/{policy_id}/validate", summary="Validate and preview a policy version")
async def validate_policy(
    policy_id: str,
    container: ContainerDep,
    version: int | None = None,
    principal: Principal = require_permission("policy.view"),
) -> dict[str, Any]:
    try:
        return container.policies.validate(_ctx(principal), policy_id[:36], version)
    except TenancyError as exc:
        raise _t(exc) from exc


class VersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


@router.post("/policies/{policy_id}/publish", summary="Publish a validated draft")
async def publish_policy(
    policy_id: str,
    body: VersionIn,
    container: ContainerDep,
    principal: Principal = require_permission("policy.manage"),
) -> dict[str, Any]:
    p0 = next(
        (p for p in container.policies.list_policies(principal.org_id) if p.policy_id == policy_id), None
    )
    if p0 is not None and p0.kind in ("security", "retention", "remediation"):
        require_recent_auth(principal, container)
        needed = {
            "security": "security.manage",
            "retention": "retention.manage",
            "remediation": "remediation.manage_policy",
        }[p0.kind]
        if not principal.can(needed):
            raise _err(403, "PERMISSION_DENIED", f"Permission {needed} required")
    try:
        return (await container.policies.publish(_ctx(principal), policy_id[:36], body.version)).public()
    except TenancyError as exc:
        raise _t(exc) from exc


class RollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_version: int = Field(ge=1)


@router.post("/policies/{policy_id}/rollback", summary="Publish an earlier version again (as a new version)")
async def rollback_policy(
    policy_id: str,
    body: RollbackIn,
    container: ContainerDep,
    principal: Principal = require_permission("policy.manage"),
) -> dict[str, Any]:
    require_recent_auth(principal, container)
    try:
        return (await container.policies.rollback(_ctx(principal), policy_id[:36], body.to_version)).public()
    except TenancyError as exc:
        raise _t(exc) from exc


@router.post(
    "/policies/{policy_id}/archive", summary="Archive a policy (its scope falls back to inheritance)"
)
async def archive_policy(
    policy_id: str, container: ContainerDep, principal: Principal = require_permission("policy.manage")
) -> dict[str, Any]:
    try:
        return (await container.policies.archive(_ctx(principal), policy_id[:36])).public()
    except TenancyError as exc:
        raise _t(exc) from exc


@router.get(
    "/policies/effective", summary="Effective policy (with provenance) for the organization or a device"
)
async def effective_policy(
    principal: Reader,
    container: ContainerDep,
    kind: str = Query(max_length=24),
    device_id: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    if kind not in SCHEMAS:
        raise _err(422, "INVALID_POLICY", "unknown policy kind")
    if not principal.can("policy.view") and not (device_id and principal.can("device.view")):
        raise _err(403, "PERMISSION_DENIED", "Permission policy.view required")
    if device_id:
        from app.api.access import check_device

        check_device(principal, container, device_id)
    values, source = container.policies.effective(kind, principal.org_id, device_id)
    return {"kind": kind, "device_id": device_id, "values": values, "source": source}


# ================================================================================= audit
def _audit_filter(
    principal: Principal,
    actor: str | None,
    action: str | None,
    category: str | None,
    resource_type: str | None,
    resource_id: str | None,
    result: str | None,
    severity: str | None,
    since: datetime | None,
    until: datetime | None,
) -> AuditFilter:
    return AuditFilter(
        principal.org_id, actor, action, category, resource_type, resource_id, result, severity, since, until
    )


@router.get("/audit", summary="Search the organization's audit events (newest first)")
async def audit(
    container: ContainerDep,
    actor: str | None = Query(default=None, max_length=128),
    action: str | None = Query(default=None, max_length=64),
    category: str | None = Query(default=None, max_length=24),
    resource_type: str | None = Query(default=None, max_length=32),
    resource_id: str | None = Query(default=None, max_length=128),
    result: str | None = Query(default=None, max_length=16),
    severity: str | None = Query(default=None, max_length=10),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    before_id: int | None = Query(default=None, ge=1),
    principal: Principal = require_permission("audit.view"),
) -> dict[str, Any]:
    f = _audit_filter(
        principal, actor, action, category, resource_type, resource_id, result, severity, since, until
    )
    return await container.audit.search(f, limit, before_id)


@router.get("/audit/export", summary="Export audit events (CSV or JSON, max 50,000 rows; audited)")
async def audit_export(
    container: ContainerDep,
    request: Request,
    fmt: Literal["csv", "json"] = "csv",
    action: str | None = Query(default=None, max_length=64),
    category: str | None = Query(default=None, max_length=24),
    since: datetime | None = None,
    until: datetime | None = None,
    principal: Principal = require_permission("audit.export"),
) -> Response:
    ok, retry = container.tenancy.check_rate(principal.org_id, "exports_per_hour")
    if not ok:
        raise HTTPException(
            429,
            {"code": "RATE_LIMITED", "message": "Export quota reached"},
            headers={"Retry-After": str(max(1, int(retry)))},
        )
    f = _audit_filter(principal, None, action, category, None, None, None, None, since, until)
    body, rows = await container.audit.export(f, fmt)
    container.audit.record(
        principal.org_id,
        principal.subject,
        "user",
        "data.exported",
        "data",
        resource_type="audit",
        metadata={"rows": rows, "format": fmt, "action": action, "since": since, "until": until},
    )
    name = f"audit-{principal.org_id}-{datetime.now(UTC):%Y%m%d%H%M}.{fmt}"
    return Response(
        body,
        media_type="text/csv" if fmt == "csv" else "application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/audit/verify", summary="Verify the audit hash chain")
async def audit_verify(
    container: ContainerDep, principal: Principal = require_permission("audit.view")
) -> dict[str, Any]:
    await container.audit.flush()
    out: dict[str, Any] = await container.audit.repo.verify_audit()
    out.pop("head", None)
    return out


# ============================================================================== identity
class ProviderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["oidc", "saml"]
    name: str = Field(min_length=1, max_length=120)
    config: dict[str, Any]
    status: Literal["ACTIVE", "DISABLED"] = "ACTIVE"


@router.get("/identity-providers", summary="Identity providers of the organization")
async def idps(
    container: ContainerDep, principal: Principal = require_permission("identity.manage")
) -> dict[str, Any]:
    return {
        "items": [p.public() for p in container.identity.providers.values() if p.org_id == principal.org_id],
        "mfa_available": container.identity.mfa_available(),
        "redirect_uri_hint": (container.settings.public_base_url or "") + "/api/v1/auth/oidc/callback",
    }


@router.post("/identity-providers", status_code=201, summary="Add an OIDC or SAML identity provider")
async def add_idp(
    body: ProviderIn, container: ContainerDep, principal: Principal = require_permission("identity.manage")
) -> dict[str, Any]:
    require_recent_auth(principal, container)
    import uuid

    try:
        cfg = container.identity.check_provider_config(
            body.kind, body.config, container.settings.oidc_allow_http
        )
    except IdentityError as exc:
        raise _err(exc.status, exc.code, str(exc)) from exc
    for role in (cfg.get("role_mapping") or {}).values():
        from app.domain.tenancy.permissions import can_grant

        if not can_grant(principal.org_role, principal.platform_admin, role):
            raise _err(403, "ROLE_ESCALATION", "role_mapping cannot grant roles above your own")
    p = IdentityProvider(
        uuid.uuid4().hex,
        principal.org_id,
        body.kind,
        body.name,
        body.status,
        cfg,
        principal.subject,
        datetime.now(UTC),
    )
    await container.identity.repo.save_idp(p)
    container.identity.providers[p.provider_id] = p
    container.audit.record(
        principal.org_id,
        principal.subject,
        "user",
        "identity.provider_added",
        "identity",
        resource_type="identity_provider",
        resource_id=p.provider_id,
        metadata={"kind": body.kind, "issuer": cfg.get("issuer"), "status": body.status},
    )
    return p.public()


class ProviderStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ACTIVE", "DISABLED"]


@router.post("/identity-providers/{provider_id}/status", summary="Enable or disable an identity provider")
async def idp_status(
    provider_id: str,
    body: ProviderStatusIn,
    container: ContainerDep,
    principal: Principal = require_permission("identity.manage"),
) -> dict[str, Any]:
    require_recent_auth(principal, container)
    p = container.identity.providers.get(provider_id[:36])
    if p is None or p.org_id != principal.org_id:
        raise _err(404, "PROVIDER_NOT_FOUND", "Unknown identity provider")
    p.status, p.updated_at = body.status, datetime.now(UTC)
    await container.identity.repo.save_idp(p)
    container.audit.record(
        principal.org_id,
        principal.subject,
        "user",
        "identity.provider_status",
        "identity",
        resource_type="identity_provider",
        resource_id=p.provider_id,
        metadata={"status": body.status},
    )
    return p.public()


class ScimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="", max_length=120)


@router.post("/scim-tokens", status_code=201, summary="Create a SCIM provisioning token (shown once)")
async def scim_token(
    body: ScimIn, container: ContainerDep, principal: Principal = require_permission("identity.manage")
) -> dict[str, Any]:
    require_recent_auth(principal, container)
    secret, t = await container.identity.create_scim_token(principal.org_id, body.label, principal.subject)
    container.audit.record(
        principal.org_id,
        principal.subject,
        "user",
        "identity.scim_token_created",
        "identity",
        resource_type="scim_token",
        resource_id=t.token_id,
    )
    return {"token": secret, "token_id": t.token_id, "label": t.label, "note": "Copy the token now."}


@router.get("/scim-tokens", summary="SCIM tokens (secrets are never shown again)")
async def scim_tokens(
    container: ContainerDep, principal: Principal = require_permission("identity.manage")
) -> dict[str, Any]:
    items = await container.identity.repo.scim_for(principal.org_id)
    return {
        "items": [
            {
                "token_id": t.token_id,
                "label": t.label,
                "created_by": t.created_by,
                "created_at": t.created_at.isoformat(),
                "revoked_at": t.revoked_at.isoformat() if t.revoked_at else None,
            }
            for t in items
        ]
    }


@router.post("/scim-tokens/{token_id}/revoke", summary="Revoke a SCIM token")
async def revoke_scim(
    token_id: str, container: ContainerDep, principal: Principal = require_permission("identity.manage")
) -> dict[str, Any]:
    t = next(
        (x for x in await container.identity.repo.scim_for(principal.org_id) if x.token_id == token_id), None
    )
    if t is None:
        raise _err(404, "TOKEN_NOT_FOUND", "Unknown token")
    t.revoked_at = t.revoked_at or datetime.now(UTC)
    await container.identity.repo.save_scim(t)
    container.audit.record(
        principal.org_id,
        principal.subject,
        "user",
        "identity.scim_token_revoked",
        "identity",
        resource_type="scim_token",
        resource_id=token_id,
    )
    return {"ok": True}


# ============================================================================ dashboards
@router.get("/dashboard", summary="Organization overview from live data")
async def dashboard(principal: Reader, container: ContainerDep) -> dict[str, Any]:
    if not principal.can("device.view"):
        raise _err(403, "PERMISSION_DENIED", "Permission device.view required")
    creds = await _creds(container)
    devices = sorted(visible_devices(principal, container))
    rows = [_device_row(container, d, creds) for d in devices]

    def count(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rows:
            out[str(r[key])] = out.get(str(r[key]), 0) + 1
        return out

    alerts = (
        [a for a in container.alerts.engine.open_alerts() if a.device_id in set(devices)]
        if container.alerts
        else []
    )
    preds = sum(len(container.forecasts.tracker.active(d)) for d in devices) if container.forecasts else 0
    rem = (
        [r for r in container.remediation.items.values() if r.device_id in set(devices)]
        if container.remediation
        else []
    )
    rem_status: dict[str, int] = {}
    for r in rem:
        rem_status[r.status.value] = rem_status.get(r.status.value, 0) + 1
    return {
        "devices_total": len(rows),
        "by_presence": count("presence"),
        "by_health": count("health"),
        "by_lifecycle": count("lifecycle_display"),
        "by_compliance": count("compliance"),
        "by_agent_version": count("agent_version"),
        "by_security_posture": count("security_posture"),
        "active_alerts": len(alerts),
        "alerts_by_severity": {
            s: sum(1 for a in alerts if a.severity == s) for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        },
        "active_predictions": preds,
        "remediation_by_status": rem_status,
        "generated_at": datetime.now(UTC).isoformat(),
    }


@router.get("/security", summary="Security dashboard (audit-backed)")
async def security(
    container: ContainerDep, principal: Principal = require_permission("audit.view")
) -> dict[str, Any]:
    await container.audit.flush()
    since = datetime.now(UTC) - timedelta(hours=24)
    f = AuditFilter(principal.org_id, since=since)
    events = [e for _, e in await container.audit.repo.search_audit(f, 5000)]

    def n(prefix: str, result: str | None = None) -> int:
        return sum(
            1 for e in events if e.action.startswith(prefix) and (result is None or e.result == result)
        )

    t = container.tenancy
    members = [m for m in t.org_members(principal.org_id) if m.status == "ACTIVE"]
    mfa = 0
    inactive = 0
    for m in members:
        mfa += await container.identity.mfa_enabled(m.username) or m.source in ("oidc", "saml")
        u = await container.admin.get_user(m.username)
        if u is None or u.last_login_at is None or u.last_login_at < datetime.now(UTC) - timedelta(days=30):
            inactive += 1
    creds = await _creds(container)
    rows = [_device_row(container, d, creds) for d in sorted(visible_devices(principal, container))]
    agent_pol = container.policies.effective("agent", principal.org_id)[0]
    from app.domain.governance.policies import version_tuple

    return {
        "window_hours": 24,
        "authentication_failures": n("auth.login_failed") + n("auth.failed") + n("auth.oidc_failed"),
        "authorization_denials": n("security.authorization_denied"),
        "cross_tenant_attempts": n("security.cross_tenant_attempt"),
        "failed_enrollments": n("device.enrollment_failed"),
        "audit_events": len(events),
        "members": len(members),
        "mfa_adoption": {"with_mfa": int(mfa), "members": len(members)},
        "inactive_users_30d": inactive,
        "devices_legacy_enrolled": sum(1 for r in rows if r["enrollment"] == "legacy key"),
        "devices_outdated_agent": sum(
            1
            for r in rows
            if r["agent_version"]
            and version_tuple(r["agent_version"]) < version_tuple(agent_pol["recommended_version"])
        ),
        "devices_non_compliant": sum(1 for r in rows if r["compliance"] == "NON_COMPLIANT"),
        "devices_revoked": sum(1 for r in rows if r["lifecycle"] == "REVOKED"),
        "security_alerts_open": sum(
            1
            for a in (container.alerts.engine.open_alerts() if container.alerts else [])
            if a.category == "security" and a.device_id in {r["device_id"] for r in rows}
        ),
        "measured": "from this organization's audit trail, registry and live state",
    }


@router.get("/usage", summary="Usage and quota utilisation")
async def usage(
    container: ContainerDep, principal: Principal = require_permission("usage.view")
) -> dict[str, Any]:
    t = container.tenancy
    devices = visible_devices(principal, container)
    counters = t.counters.get(principal.org_id, {})
    ws = sum(1 for c in container.ws._clients if getattr(c, "org_id", None) == principal.org_id)
    diag = (
        sum(1 for d in getattr(container.diagnosis.repo, "items", {}).values() if d.device_id in devices)
        if container.diagnosis
        else None
    )
    return {
        "users": len([m for m in t.org_members(principal.org_id) if m.status == "ACTIVE"]),
        "devices": len(t.org_devices(principal.org_id)),
        "telemetry_batches_since_start": counters.get("telemetry_batches_per_min", 0),
        "api_requests_since_start": counters.get("api_requests_per_min", 0),
        "websocket_connections": ws,
        "diagnoses_in_memory": diag,
        "remediations_recent": sum(
            1
            for r in (container.remediation.items.values() if container.remediation else [])
            if r.device_id in devices
        ),
        "storage": "NOT_MEASURED",
        "quotas": t.quota_usage(principal.org_id),
        "since": "counters reset when the backend restarts",
    }


# ================================================================== data governance
class DeletionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_device_id: str = Field(max_length=64)
    reason: str = Field(min_length=3, max_length=300)


@router.post(
    "/devices/{device_id}/data-deletion",
    status_code=202,
    summary="Delete a retired device's telemetry and derived data (audit and governance records are kept)",
)
async def delete_device_data(
    device_id: str,
    body: DeletionIn,
    container: ContainerDep,
    principal: Principal = require_permission("device.remove"),
) -> dict[str, Any]:
    from app.api.access import check_device

    if not principal.can("retention.manage"):
        raise _err(403, "PERMISSION_DENIED", "Permission retention.manage required")
    check_device(principal, container, device_id[:64])
    require_recent_auth(principal, container)
    if body.confirm_device_id != device_id:
        raise _err(422, "CONFIRMATION_MISMATCH", "Type the device id to confirm")
    rec = container.tenancy.registry.get(device_id)
    if rec is None or rec.lifecycle != Lifecycle.RETIRED:
        raise _err(409, "NOT_RETIRED", "Retire the device before deleting its data")
    job = await container.governance_jobs.delete_device_data(_ctx(principal), device_id, body.reason)
    return job


# ======================================================================= platform admin
class OrgIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: str = Field(min_length=2, max_length=48)
    name: str = Field(min_length=1, max_length=120)
    owner: str | None = Field(default=None, max_length=64)


class OrgAdminPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=120)
    status: Literal["ACTIVE", "SUSPENDED", "ARCHIVED"] | None = None
    quotas: dict[str, dict[str, Any]] | None = None


@platform_router.get("/organizations", summary="All organizations (platform administrators)")
async def all_orgs(
    container: ContainerDep, principal: Principal = require_permission("platform.manage")
) -> dict[str, Any]:
    t = container.tenancy
    return {
        "items": [
            dict(o.public(), devices=len(t.org_devices(o.org_id)), members=len(t.org_members(o.org_id)))
            for o in t.orgs.values()
        ],
        "quota_names": list(QUOTA_DEFAULTS),
        "permissions": sorted(PERMISSIONS),
    }


@platform_router.post(
    "/organizations", status_code=201, summary="Create an organization (and its first owner)"
)
async def create_org(
    body: OrgIn, container: ContainerDep, principal: Principal = require_permission("platform.manage")
) -> dict[str, Any]:
    require_recent_auth(principal, container)
    ctx = _ctx(principal)
    try:
        o = await container.tenancy.create_org(ctx, body.org_id, body.name)
        if body.owner:
            if await container.admin.get_user(body.owner) is None:
                raise _err(404, "USER_NOT_FOUND", "Owner account does not exist")
            await container.tenancy.set_member(None, o.org_id, body.owner, "org_owner")
            container.audit.record(
                o.org_id,
                principal.subject,
                "user",
                "user.member_added",
                "user",
                resource_type="user",
                resource_id=body.owner,
                metadata={"role": "org_owner"},
            )
    except TenancyError as exc:
        raise _t(exc) from exc
    await container.policies.load()
    return o.public()


@platform_router.patch("/organizations/{org_id}", summary="Rename, suspend or set quotas")
async def patch_org(
    org_id: str,
    body: OrgAdminPatch,
    container: ContainerDep,
    principal: Principal = require_permission("platform.manage"),
) -> dict[str, Any]:
    require_recent_auth(principal, container)
    try:
        o = await container.tenancy.update_org(
            _ctx(principal), org_id[:48], body.name, body.status, body.quotas
        )
    except TenancyError as exc:
        raise _t(exc) from exc
    if o.status.value != "ACTIVE":
        for m in container.tenancy.org_members(o.org_id):
            await container.identity.revoke_user(
                m.username, f"organization {o.status.value.lower()}", org_id=o.org_id
            )
    return o.public()


@platform_router.get("/audit", summary="Platform security audit (platform administrators; all organizations)")
async def platform_audit(
    container: ContainerDep,
    org_id: str | None = Query(default=None, max_length=64),
    action: str | None = Query(default=None, max_length=64),
    category: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=100, ge=1, le=500),
    before_id: int | None = Query(default=None, ge=1),
    principal: Principal = require_permission("platform.manage"),
) -> dict[str, Any]:
    f = AuditFilter(
        org_id, action=action, category=category
    )  # org_id None: every organization + platform events
    return await container.audit.search(f, limit, before_id)
