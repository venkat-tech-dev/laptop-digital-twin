"""TenancyService (Phase 9): organisations, structure, memberships, device ownership / lifecycle, enrollment,
tenant context resolution, device visibility and quotas.

The in-memory model is loaded at start and updated write-through; every read used on the request path
(device -> organisation, membership, visibility) is a dictionary lookup.

Visibility = devices whose registry organisation is the context organisation, narrowed by the member's
device-group scope (if any) and, for employees, by device assignment. It is always an explicit set: there is
no "all devices" answer, so a route that forgets a filter fails closed rather than open.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.domain.tenancy.models import (
    DEFAULT_ORG,
    INGEST_REFUSED,
    LIFECYCLE_TRANSITIONS,
    NAME_MAX,
    SLUG,
    UNIT_PARENT,
    DeviceGroup,
    DeviceRecord,
    EnrollmentToken,
    Lifecycle,
    Membership,
    Organization,
    OrgStatus,
    OrgUnit,
    QuotaMode,
    UnitKind,
    hash_secret,
    new_enrollment_secret,
)
from app.domain.tenancy.permissions import LEGACY_RANK, ROLES, can_grant, permissions_for

log = structlog.get_logger("tenancy")


class TenancyError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class TenantContext:
    """Who is acting, in which organisation, with which permissions and scope (server-resolved)."""

    org_id: str
    username: str
    org_role: str | None
    permissions: frozenset[str]
    group_scope: frozenset[str] = frozenset()  # empty = whole organisation
    platform_admin: bool = False
    actor_type: str = "user"

    def can(self, permission: str) -> bool:
        return permission in self.permissions


@dataclass
class RateWindow:
    """Fixed per-minute (or per-hour) counter windows per key: cheap and good enough for quotas."""

    span_s: float
    hits: dict[str, tuple[float, int]] = field(default_factory=dict)

    def hit(self, key: str, limit: int, now: float) -> tuple[bool, float]:
        start, n = self.hits.get(key, (now, 0))
        if now - start >= self.span_s:
            start, n = now, 0
        if n >= limit:
            return False, self.span_s - (now - start)
        self.hits[key] = (start, n + 1)
        return True, 0.0

    def used(self, key: str, now: float) -> int:
        start, n = self.hits.get(key, (now, 0))
        return 0 if now - start >= self.span_s else n


class TenancyService:
    def __init__(self, repo: Any, assignments: Any = None, audit: Any = None) -> None:
        self.repo = repo
        self._assignments = assignments
        self.audit = audit  # AuditService (set by the container)
        self.orgs: dict[str, Organization] = {}
        self.units: dict[str, OrgUnit] = {}
        self.groups: dict[str, DeviceGroup] = {}
        self.device_groups: dict[str, set[str]] = {}  # device -> group ids
        self.members: dict[str, dict[str, Membership]] = {}  # username -> org -> membership
        self.registry: dict[str, DeviceRecord] = {}
        self.platform_admins: set[str] = set()
        self.on_lifecycle: list[Callable[[DeviceRecord], Any]] = []
        self._minute = RateWindow(60.0)
        self._hour = RateWindow(3600.0)
        self._day = RateWindow(86400.0)
        self.counters: dict[str, dict[str, int]] = {}  # org -> usage counters
        self.cross_tenant_attempts: deque[float] = deque(maxlen=10_000)

    # ------------------------------------------------------------------ load
    async def load(self) -> None:
        data = await self.repo.load_all()
        self.orgs = {o.org_id: o for o in data["orgs"]}
        self.units = {u.unit_id: u for u in data["units"]}
        self.groups = {g.group_id: g for g in data["groups"]}
        self.device_groups = {}
        for gid, device, _org in data["group_members"]:
            self.device_groups.setdefault(device, set()).add(gid)
        self.members = {}
        for m in data["members"]:
            self.members.setdefault(m.username, {})[m.org_id] = m
        self.registry = {r.device_id: r for r in data["registry"]}
        for dev, gids in self.device_groups.items():
            if dev in self.registry:
                self.registry[dev].groups = sorted(gids)
        self.platform_admins = set(data["platform_admins"])
        if (
            DEFAULT_ORG not in self.orgs
        ):  # memory deployments and fresh installs without the migration backfill
            await self.repo.save_org(
                o := Organization(DEFAULT_ORG, "Default organization", OrgStatus.ACTIVE, datetime.now(UTC))
            )
            self.orgs[DEFAULT_ORG] = o

    # ------------------------------------------------------------------ context
    def memberships(self, username: str) -> list[Membership]:
        return [
            m
            for m in self.members.get(username, {}).values()
            if m.status == "ACTIVE"
            and self.orgs.get(m.org_id) is not None
            and self.orgs[m.org_id].status == OrgStatus.ACTIVE
        ]

    def context(
        self,
        username: str,
        requested_org: str | None,
        legacy_role: str | None = None,
        actor_type: str = "user",
    ) -> TenantContext:
        """Resolve the organisation for a request. A requested organisation must be one of the user's active
        memberships (platform administrators may enter any active organisation). Raises TenancyError."""
        platform = username in self.platform_admins
        mine = {m.org_id: m for m in self.memberships(username)}
        org = requested_org or (DEFAULT_ORG if DEFAULT_ORG in mine else next(iter(sorted(mine)), None))
        if org is None and platform:
            org = DEFAULT_ORG
        if org is None:
            raise TenancyError(
                "NO_ORGANIZATION", "Your account is not a member of any active organization", 403
            )
        if org not in self.orgs or self.orgs[org].status != OrgStatus.ACTIVE:
            raise TenancyError("ORGANIZATION_ACCESS_DENIED", "Unknown or inactive organization", 403)
        m = mine.get(org)
        if m is None and not platform:
            self.note_cross_tenant(username, "organization", org)
            raise TenancyError("ORGANIZATION_ACCESS_DENIED", "You are not a member of this organization", 403)
        role = m.role if m is not None else ("org_owner" if platform else None)
        return TenantContext(
            org,
            username,
            role,
            permissions_for(role, platform),
            frozenset(m.group_scope) if m is not None else frozenset(),
            platform,
            actor_type,
        )

    def service_context(self, org_id: str = DEFAULT_ORG, actor: str = "local") -> TenantContext:
        """Anonymous (AUTH_MODE=none) and API-key principals: administrators of one organisation."""
        return TenantContext(
            org_id, actor, "org_admin", permissions_for("org_admin"), frozenset(), False, "api_key"
        )

    @staticmethod
    def legacy_role(ctx: TenantContext) -> str:
        if ctx.platform_admin:
            return "admin"
        return LEGACY_RANK.get(ctx.org_role or "", "employee")

    # ------------------------------------------------------------------ devices
    def org_of(self, device_id: str) -> str | None:
        r = self.registry.get(device_id)
        return r.org_id if r is not None else None

    def lifecycle(self, device_id: str) -> Lifecycle | None:
        r = self.registry.get(device_id)
        return r.lifecycle if r is not None else None

    def org_devices(self, org_id: str) -> set[str]:
        return {d for d, r in self.registry.items() if r.org_id == org_id}

    def visible(self, ctx: TenantContext) -> set[str]:
        devices = self.org_devices(ctx.org_id)
        if ctx.group_scope:
            devices = {d for d in devices if self.device_groups.get(d, set()) & ctx.group_scope}
        if ctx.org_role == "employee" and not ctx.platform_admin:
            mine = self._assignments.devices_for(ctx.username) if self._assignments is not None else set()
            devices &= set(mine)
        return devices

    def note_cross_tenant(
        self, actor: str, resource_type: str, resource_id: str, org: str | None = None
    ) -> None:
        self.cross_tenant_attempts.append(time.time())
        from app.core.metrics import CROSS_TENANT_ATTEMPTS

        CROSS_TENANT_ATTEMPTS.labels(resource_type).inc()
        if self.audit is not None:
            self.audit.record(
                None,  # platform security audit: never the prober's organisation (no inference channel)
                actor,
                "user",
                "security.cross_tenant_attempt",
                "security",
                resource_type=resource_type,
                resource_id=resource_id,
                result="DENIED",
                severity="HIGH",
                reason="resource belongs to another organization or does not exist",
            )

    async def ensure_device(
        self, device_id: str, org_id: str, enrollment_id: str, by: str, group_id: str | None = None
    ) -> DeviceRecord:
        """Register (or re-activate on re-enrollment) a device in ``org_id``. A device owned by another
        organisation is never moved implicitly: it must be retired there first."""
        now = datetime.now(UTC)
        r = self.registry.get(device_id)
        if (
            r is not None
            and r.org_id != org_id
            and r.lifecycle not in (Lifecycle.RETIRED, Lifecycle.DECOMMISSIONED)
        ):
            raise TenancyError(
                "DEVICE_OWNED_ELSEWHERE", "This device is enrolled in another organization", 409
            )
        if (
            r is not None
            and r.org_id == org_id
            and r.lifecycle in (Lifecycle.ACTIVE, Lifecycle.QUARANTINED, Lifecycle.DISABLED)
        ):
            if enrollment_id != "legacy":
                r.enrollment_id, r.updated_at, r.updated_by = enrollment_id, now, by
                await self.repo.save_registry(r)
            return r
        r = DeviceRecord(
            device_id,
            org_id,
            Lifecycle.ACTIVE,
            now,
            enrollment_id,
            [],
            now,
            by,
            "enrolled" if r is None else f"re-enrolled from {r.lifecycle.value}",
        )
        self.registry[device_id] = r
        await self.repo.save_registry(r)
        if group_id and group_id in self.groups and self.groups[group_id].org_id == org_id:
            await self.set_group_members(group_id, add=[device_id], remove=[], by=by)
        for cb in self.on_lifecycle:
            cb(r)
        return r

    def ingest_allowed(self, device_id: str) -> str | None:
        """None = allowed; else a code. Unknown devices are allowed only through registration paths."""
        r = self.registry.get(device_id)
        if r is None:
            return None
        if r.lifecycle in INGEST_REFUSED:
            return f"DEVICE_{r.lifecycle.value}"
        org = self.orgs.get(r.org_id)
        if org is None or org.status != OrgStatus.ACTIVE:
            return "ORGANIZATION_INACTIVE"
        return None

    async def transition(
        self, ctx: TenantContext, device_id: str, to: Lifecycle, reason: str | None
    ) -> DeviceRecord:
        r = self.registry.get(device_id)
        if r is None or r.org_id != ctx.org_id:
            raise TenancyError("DEVICE_NOT_FOUND", "Unknown device", 404)
        if to not in LIFECYCLE_TRANSITIONS[r.lifecycle]:
            raise TenancyError("INVALID_TRANSITION", f"{r.lifecycle.value} -> {to.value} is not allowed", 409)
        if to == Lifecycle.ACTIVE and r.lifecycle == Lifecycle.REVOKED:
            raise TenancyError(
                "REENROLL_REQUIRED", "A revoked device must re-enroll with a new enrollment token", 409
            )
        old = r.lifecycle
        r.lifecycle, r.updated_at, r.updated_by, r.reason = (
            to,
            datetime.now(UTC),
            ctx.username,
            (reason or "")[:300] or None,
        )
        await self.repo.save_registry(r)
        for cb in self.on_lifecycle:
            cb(r)
        self._audit(
            ctx,
            f"device.{to.value.lower()}",
            "device",
            device_id,
            metadata={"from": old.value, "to": to.value, "reason": reason},
        )
        return r

    # ------------------------------------------------------------------ enrollment
    async def create_token(
        self,
        ctx: TenantContext,
        ttl_hours: float,
        max_uses: int,
        group_id: str | None,
        label: str,
        max_ttl_hours: int,
        allow_multi_use: bool,
    ) -> tuple[str, EnrollmentToken]:
        if not 0 < ttl_hours <= max_ttl_hours:
            raise TenancyError(
                "INVALID_TTL", f"ttl_hours must be between 0 and {max_ttl_hours} (enrollment policy)", 422
            )
        if max_uses < 1 or (max_uses > 1 and not allow_multi_use) or max_uses > 1000:
            raise TenancyError(
                "INVALID_USES",
                "multi-use tokens are not allowed by the enrollment policy"
                if max_uses > 1
                else "max_uses must be at least 1",
                422,
            )
        if group_id is not None and (
            group_id not in self.groups or self.groups[group_id].org_id != ctx.org_id
        ):
            raise TenancyError("GROUP_NOT_FOUND", "Unknown device group", 404)
        secret, digest = new_enrollment_secret()
        now = datetime.now(UTC)
        t = EnrollmentToken(
            uuid.uuid4().hex,
            ctx.org_id,
            digest,
            ctx.username,
            now,
            now + timedelta(hours=ttl_hours),
            max_uses,
            0,
            group_id,
            None,
            label[:120],
        )
        await self.repo.save_token(t)
        self._audit(
            ctx,
            "device.enrollment_token_created",
            "enrollment_token",
            t.token_id,
            metadata={"expires_at": t.expires_at.isoformat(), "max_uses": max_uses, "group_id": group_id},
        )
        return secret, t

    async def revoke_token(self, ctx: TenantContext, token_id: str) -> EnrollmentToken:
        t: EnrollmentToken | None = next(
            (x for x in await self.repo.tokens_for(ctx.org_id) if x.token_id == token_id), None
        )
        if t is None:
            raise TenancyError("TOKEN_NOT_FOUND", "Unknown enrollment token", 404)
        t.revoked_at = t.revoked_at or datetime.now(UTC)
        await self.repo.save_token(t)
        self._audit(ctx, "device.enrollment_token_revoked", "enrollment_token", token_id)
        return t

    async def enroll(
        self, secret: str, device_id: str, ip: str | None
    ) -> tuple[DeviceRecord, EnrollmentToken]:
        """Agent enrollment with an organisation token (single-use by default, expiring, revocable)."""
        now = datetime.now(UTC)
        t = await self.repo.token_by_hash(hash_secret(secret)) if secret.startswith("ldt_enr_") else None
        why = "invalid enrollment token" if t is None else t.usable(now)
        org = self.orgs.get(t.org_id) if t is not None else None
        if why is None and (org is None or org.status != OrgStatus.ACTIVE):
            why = "organization inactive"
        if why is None and t is not None:
            limit, mode = org.quota("max_devices")  # type: ignore[union-attr]
            if (
                mode == QuotaMode.REJECT
                and len(self.org_devices(t.org_id)) >= limit
                and device_id not in self.registry
            ):
                why = "device quota of the organization reached"
        if why is None and t is not None and not await self.repo.consume_token(t.token_id):
            why = "enrollment token already used"
        if why is not None or t is None:
            from app.core.metrics import ENROLLMENT_FAILURES

            ENROLLMENT_FAILURES.labels(why.split()[0] if why else "invalid").inc()
            if self.audit is not None:
                self.audit.record(
                    t.org_id if t else None,
                    f"agent:{device_id[:64]}",
                    "agent",
                    "device.enrollment_failed",
                    "device",
                    resource_type="device",
                    resource_id=device_id[:64],
                    result="FAILURE",
                    severity="WARNING",
                    reason=why,
                    ip=ip,
                    source="agent",
                )
            raise TenancyError("ENROLLMENT_FAILED", why or "invalid enrollment token", 401)
        r = await self.ensure_device(
            device_id, t.org_id, t.token_id, f"enrollment:{t.token_id[:8]}", t.group_id
        )
        if self.audit is not None:
            self.audit.record(
                t.org_id,
                f"agent:{device_id}",
                "agent",
                "device.enrolled",
                "device",
                resource_type="device",
                resource_id=device_id,
                ip=ip,
                source="agent",
                metadata={"token_id": t.token_id, "group_id": t.group_id},
            )
        return r, t

    # ------------------------------------------------------------------ organisations / structure / groups
    async def create_org(self, ctx: TenantContext, org_id: str, name: str) -> Organization:
        if not ctx.platform_admin:
            raise TenancyError("FORBIDDEN", "Only platform administrators create organizations", 403)
        if not SLUG.match(org_id) or org_id in self.orgs:
            raise TenancyError(
                "INVALID_ORG", "org_id must be a new lower-case slug (letters, digits, '-')", 422
            )
        o = Organization(org_id, _name(name), OrgStatus.ACTIVE, datetime.now(UTC))
        await self.repo.save_org(o)
        self.orgs[org_id] = o
        self._audit(ctx, "organization.created", "organization", org_id, org_override=org_id)
        return o

    async def update_org(
        self,
        ctx: TenantContext,
        org_id: str,
        name: str | None,
        status: str | None,
        quotas: dict[str, Any] | None,
    ) -> Organization:
        o = self.orgs.get(org_id)
        if o is None or (org_id != ctx.org_id and not ctx.platform_admin):
            raise TenancyError("ORG_NOT_FOUND", "Unknown organization", 404)
        if (status is not None or quotas is not None) and not ctx.platform_admin:
            raise TenancyError("FORBIDDEN", "Status and quotas are managed by platform administrators", 403)
        from app.domain.tenancy.models import QUOTA_DEFAULTS

        if quotas is not None:
            clean: dict[str, Any] = {}
            for k, v in quotas.items():
                if k not in QUOTA_DEFAULTS or not isinstance(v, dict):
                    raise TenancyError("INVALID_QUOTA", f"unknown quota {k}", 422)
                limit, mode = (
                    int(v.get("limit", QUOTA_DEFAULTS[k][0])),
                    str(v.get("mode", QUOTA_DEFAULTS[k][1])),
                )
                if limit < 0 or limit > 10_000_000 or mode not in QuotaMode.__members__:
                    raise TenancyError("INVALID_QUOTA", f"invalid value for {k}", 422)
                clean[k] = {"limit": limit, "mode": mode}
            o.quotas = clean
        if name is not None:
            o.name = _name(name)
        if status is not None:
            if status not in OrgStatus.__members__ or (org_id == DEFAULT_ORG and status != "ACTIVE"):
                raise TenancyError("INVALID_STATUS", "invalid organization status", 422)
            o.status = OrgStatus(status)
        await self.repo.save_org(o)
        self._audit(
            ctx,
            "organization.updated",
            "organization",
            org_id,
            org_override=org_id,
            metadata={"name": name, "status": status, "quotas": sorted((quotas or {}).keys())},
        )
        return o

    async def create_unit(self, ctx: TenantContext, kind: str, name: str, parent_id: str | None) -> OrgUnit:
        try:
            k = UnitKind(kind)
        except ValueError as exc:
            raise TenancyError("INVALID_UNIT", "kind must be business_unit, department or team", 422) from exc
        parent = self.units.get(parent_id) if parent_id else None
        if parent_id and (parent is None or parent.org_id != ctx.org_id):
            raise TenancyError("UNIT_NOT_FOUND", "Unknown parent unit", 404)
        if (parent.kind if parent else None) not in UNIT_PARENT[k]:
            raise TenancyError("INVALID_PARENT", f"a {k.value} cannot be placed under that parent", 422)
        u = OrgUnit(uuid.uuid4().hex, ctx.org_id, k, _name(name), parent_id)
        await self.repo.save_unit(u)
        self.units[u.unit_id] = u
        self._audit(
            ctx,
            "organization.unit_created",
            "org_unit",
            u.unit_id,
            metadata={"kind": k.value, "name": u.name},
        )
        return u

    async def archive_unit(self, ctx: TenantContext, unit_id: str) -> OrgUnit:
        u = self.units.get(unit_id)
        if u is None or u.org_id != ctx.org_id:
            raise TenancyError("UNIT_NOT_FOUND", "Unknown unit", 404)
        u.status = "ARCHIVED"
        await self.repo.save_unit(u)
        self._audit(ctx, "organization.unit_archived", "org_unit", unit_id)
        return u

    async def save_group(
        self,
        ctx: TenantContext,
        group_id: str | None,
        name: str,
        kind: str,
        unit_id: str | None,
        priority: int,
        tags: list[str],
        status: str = "ACTIVE",
    ) -> DeviceGroup:
        if kind not in (
            "department",
            "team",
            "location",
            "business_unit",
            "os",
            "environment",
            "role",
            "custom",
        ):
            raise TenancyError("INVALID_GROUP", "unknown group kind", 422)
        if unit_id is not None and (unit_id not in self.units or self.units[unit_id].org_id != ctx.org_id):
            raise TenancyError("UNIT_NOT_FOUND", "Unknown unit", 404)
        if not 0 <= priority <= 10_000 or status not in ("ACTIVE", "ARCHIVED"):
            raise TenancyError("INVALID_GROUP", "invalid priority or status", 422)
        clean = _name(name)
        if any(
            g.org_id == ctx.org_id and g.name == clean and g.group_id != group_id
            for g in self.groups.values()
        ):
            raise TenancyError("DUPLICATE_GROUP", "A group with this name exists", 409)
        if group_id is not None:
            g = self.groups.get(group_id)
            if g is None or g.org_id != ctx.org_id:
                raise TenancyError("GROUP_NOT_FOUND", "Unknown device group", 404)
            g.name, g.kind, g.unit_id, g.priority, g.status = clean, kind, unit_id, priority, status
            g.tags = [str(t)[:40] for t in tags][:20]
        else:
            g = DeviceGroup(
                uuid.uuid4().hex, ctx.org_id, clean, kind, unit_id, priority, [str(t)[:40] for t in tags][:20]
            )
        await self.repo.save_group(g)
        self.groups[g.group_id] = g
        self._audit(
            ctx, "group.saved", "device_group", g.group_id, metadata={"name": g.name, "status": g.status}
        )
        return g

    async def set_group_members(
        self, group_id: str, add: list[str], remove: list[str], by: str, ctx: TenantContext | None = None
    ) -> DeviceGroup:
        g = self.groups.get(group_id)
        if g is None or (ctx is not None and g.org_id != ctx.org_id):
            raise TenancyError("GROUP_NOT_FOUND", "Unknown device group", 404)
        allowed = self.visible(ctx) if ctx is not None else None
        for d in add:
            if self.org_of(d) != g.org_id or (allowed is not None and d not in allowed):
                raise TenancyError("DEVICE_NOT_FOUND", "Unknown device", 404)
        for d in add:
            self.device_groups.setdefault(d, set()).add(group_id)
            await self.repo.set_group_member(group_id, d, g.org_id, True)
        for d in remove:
            if group_id in self.device_groups.get(d, set()):
                self.device_groups[d].discard(group_id)
                await self.repo.set_group_member(group_id, d, g.org_id, False)
        for d in {*add, *remove}:
            if d in self.registry:
                self.registry[d].groups = sorted(self.device_groups.get(d, set()))
        if ctx is not None:
            self._audit(
                ctx,
                "group.membership_changed",
                "device_group",
                group_id,
                metadata={"added": add, "removed": remove},
            )
        return g

    # ------------------------------------------------------------------ members
    def member(self, org_id: str, username: str) -> Membership | None:
        return self.members.get(username, {}).get(org_id)

    def org_members(self, org_id: str) -> list[Membership]:
        return [ms[org_id] for ms in self.members.values() if org_id in ms]

    async def set_member(
        self,
        ctx: TenantContext | None,
        org_id: str,
        username: str,
        role: str,
        group_scope: list[str] | None = None,
        status: str = "ACTIVE",
        source: str = "local",
    ) -> Membership:
        if role not in ROLES:
            raise TenancyError("INVALID_ROLE", "unknown role", 422)
        if ctx is not None:
            if org_id != ctx.org_id and not ctx.platform_admin:
                raise TenancyError("FORBIDDEN", "Unknown organization", 404)
            prev = self.member(org_id, username)
            if not can_grant(ctx.org_role, ctx.platform_admin, role) or (
                prev is not None and not can_grant(ctx.org_role, ctx.platform_admin, prev.role)
            ):
                raise TenancyError("ROLE_ESCALATION", "You cannot grant or change a role above your own", 403)
            if username == ctx.username and not ctx.platform_admin and prev is not None and role != prev.role:
                raise TenancyError("SELF_ROLE_CHANGE", "You cannot change your own role", 403)
        if status not in ("ACTIVE", "DISABLED"):
            raise TenancyError("INVALID_STATUS", "invalid membership status", 422)
        scope = [g for g in (group_scope or []) if g in self.groups and self.groups[g].org_id == org_id]
        if len(scope) != len(group_scope or []):
            raise TenancyError("GROUP_NOT_FOUND", "Unknown device group in scope", 404)
        prev = self.member(org_id, username)
        if prev is None and ctx is not None:
            limit, mode = self.orgs[org_id].quota("max_users")
            if (
                mode == QuotaMode.REJECT
                and len([m for m in self.org_members(org_id) if m.status == "ACTIVE"]) >= limit
            ):
                raise TenancyError("QUOTA_EXCEEDED", "User quota of the organization reached", 409)
        if prev is not None and prev.role == "org_owner" and (role != "org_owner" or status != "ACTIVE"):
            owners = [m for m in self.org_members(org_id) if m.role == "org_owner" and m.status == "ACTIVE"]
            if len(owners) <= 1:
                raise TenancyError("LAST_OWNER", "An organization must keep at least one active owner", 409)
        m = Membership(
            org_id,
            username,
            role,
            status,
            scope,
            source if prev is None else prev.source,
            prev.created_at if prev else datetime.now(UTC),
        )
        await self.repo.save_member(m)
        self.members.setdefault(username, {})[org_id] = m
        if ctx is not None:
            self._audit(
                ctx,
                "user.role_changed" if prev else "user.member_added",
                "user",
                username,
                org_override=org_id,
                metadata={
                    "role": role,
                    "previous_role": prev.role if prev else None,
                    "status": status,
                    "group_scope": scope,
                },
            )
        return m

    async def set_platform_admin(self, username: str, value: bool) -> None:
        await self.repo.set_platform_admin(username, value)
        (self.platform_admins.add if value else self.platform_admins.discard)(username)

    def recipients(self, device_id: str) -> list[tuple[str, str]]:
        """(username, legacy role) of active members who may see this device (never another tenant)."""
        org = self.org_of(device_id)
        if org is None:
            return []
        out = []
        for m in self.org_members(org):
            if m.status != "ACTIVE":
                continue
            ctx = TenantContext(org, m.username, m.role, permissions_for(m.role), frozenset(m.group_scope))
            if "alert.view" in ctx.permissions and device_id in self.visible(ctx):
                out.append((m.username, LEGACY_RANK.get(m.role, "employee")))
        return out

    # ------------------------------------------------------------------ quotas
    def check_rate(self, org_id: str, quota: str, key_suffix: str = "", cost: int = 1) -> tuple[bool, float]:
        org = self.orgs.get(org_id)
        if org is None:
            return False, 60.0
        limit, mode = org.quota(quota)
        window = {"per_min": self._minute, "per_hour": self._hour, "per_day": self._day}[
            "per_hour"
            if quota.endswith("per_hour")
            else "per_day"
            if quota.endswith("per_day")
            else "per_min"
        ]
        usage = self.counters.setdefault(org_id, {})
        usage[quota] = usage.get(quota, 0) + cost
        if mode == QuotaMode.ALLOW:
            return True, 0.0
        ok, retry = window.hit(f"{org_id}:{quota}:{key_suffix}", limit, time.monotonic())
        if not ok:
            from app.core.metrics import QUOTA_REJECTIONS

            QUOTA_REJECTIONS.labels(quota).inc()
            usage[f"{quota}_rejected"] = usage.get(f"{quota}_rejected", 0) + 1
        return ok, retry

    def quota_usage(self, org_id: str) -> dict[str, Any]:
        org = self.orgs[org_id]
        now = time.monotonic()
        out: dict[str, Any] = {}
        for name in ("max_devices", "max_users"):
            limit, mode = org.quota(name)
            used = (
                len(self.org_devices(org_id))
                if name == "max_devices"
                else len([m for m in self.org_members(org_id) if m.status == "ACTIVE"])
            )
            out[name] = {"used": used, "limit": limit, "mode": mode.value}
        for name, window in (
            ("telemetry_batches_per_min", self._minute),
            ("api_requests_per_min", self._minute),
            ("diagnosis_jobs_per_hour", self._hour),
            ("remediation_requests_per_day", self._day),
            ("exports_per_hour", self._hour),
        ):
            limit, mode = org.quota(name)
            out[name] = {
                "used": window.used(f"{org_id}:{name}:", now),
                "limit": limit,
                "mode": mode.value,
                "rejected_total": self.counters.get(org_id, {}).get(f"{name}_rejected", 0),
            }
        return out

    # ------------------------------------------------------------------ audit helper
    def _audit(
        self,
        ctx: TenantContext,
        action: str,
        resource_type: str,
        resource_id: str,
        org_override: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self.audit is not None:
            category = action.split(".")[0]
            self.audit.record(
                org_override or ctx.org_id,
                ctx.username,
                ctx.actor_type,
                action,
                category if category in ("device", "user", "organization", "policy") else "organization",
                resource_type=resource_type,
                resource_id=resource_id,
                metadata=metadata or {},
            )


def _name(name: str) -> str:
    clean = " ".join(str(name).split())[:NAME_MAX]
    if not clean:
        raise TenancyError("INVALID_NAME", "name is required", 422)
    return clean
