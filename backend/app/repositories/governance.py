"""Storage for Phase 9: organisations, structure, groups, memberships, device registry, enrollment tokens,
policies, enterprise audit, sessions, identity providers, MFA seeds and SCIM tokens (memory + PostgreSQL)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.domain.governance.audit import GENESIS, AuditEvent, chain
from app.domain.governance.policies import Policy, PolicyStatus
from app.domain.tenancy.models import (
    DeviceGroup,
    DeviceRecord,
    EnrollmentToken,
    Lifecycle,
    Membership,
    Organization,
    OrgStatus,
    OrgUnit,
    UnitKind,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import (
    AuditEventRow,
    DeviceGroupMemberRow,
    DeviceGroupRow,
    DeviceRegistryRow,
    EnrollmentTokenRow,
    IdentityProviderRow,
    OrganizationMemberRow,
    OrganizationRow,
    OrgUnitRow,
    PolicyRow,
    ScimTokenRow,
    UserMfaRow,
    UserRow,
    UserSessionRow,
)

AUDIT_LOCK = 0x4C445441  # "LDTA"


@dataclass
class Session:
    session_id: str
    username: str
    org_id: str
    created_at: datetime
    expires_at: datetime
    auth_method: str
    mfa: bool = False
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    ip: str | None = None
    user_agent: str | None = None

    def public(self) -> dict[str, Any]:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        return {
            "session_id": self.session_id,
            "username": self.username,
            "org_id": self.org_id,
            "created_at": iso(self.created_at),
            "expires_at": iso(self.expires_at),
            "last_seen_at": iso(self.last_seen_at),
            "revoked_at": iso(self.revoked_at),
            "revoked_reason": self.revoked_reason,
            "auth_method": self.auth_method,
            "mfa": self.mfa,
            "ip": self.ip,
            "user_agent": self.user_agent,
        }


@dataclass
class IdentityProvider:
    provider_id: str
    org_id: str
    kind: str  # local | oidc | saml
    name: str
    status: str  # ACTIVE | DISABLED
    config: dict[str, Any]
    created_by: str
    created_at: datetime
    updated_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "org_id": self.org_id,
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "config": self.config,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class MfaSeed:
    username: str
    secret_enc: str
    enabled: bool
    created_at: datetime
    last_step: int = 0


@dataclass
class ScimToken:
    token_id: str
    org_id: str
    token_hash: str
    label: str
    created_by: str
    created_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class AuditFilter:
    org_id: str | None  # None only for platform administrators
    actor_id: str | None = None
    action: str | None = None  # prefix match (e.g. "auth.")
    category: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    result: str | None = None
    severity: str | None = None
    since: datetime | None = None
    until: datetime | None = None


def _audit_matches(e: AuditEvent, f: AuditFilter) -> bool:
    return (
        (f.org_id is None or e.org_id == f.org_id)
        and (f.actor_id is None or e.actor_id == f.actor_id)
        and (f.action is None or e.action.startswith(f.action))
        and (f.category is None or e.category == f.category)
        and (f.resource_type is None or e.resource_type == f.resource_type)
        and (f.resource_id is None or e.resource_id == f.resource_id)
        and (f.result is None or e.result == f.result)
        and (f.severity is None or e.severity == f.severity)
        and (f.since is None or e.at >= f.since)
        and (f.until is None or e.at <= f.until)
    )


@dataclass
class MemoryGovernanceRepository:
    orgs: dict[str, Organization] = field(default_factory=dict)
    units: dict[str, OrgUnit] = field(default_factory=dict)
    groups: dict[str, DeviceGroup] = field(default_factory=dict)
    group_members: set[tuple[str, str, str]] = field(default_factory=set)  # (group, device, org)
    members: dict[tuple[str, str], Membership] = field(default_factory=dict)
    registry: dict[str, DeviceRecord] = field(default_factory=dict)
    tokens: dict[str, EnrollmentToken] = field(default_factory=dict)
    policies: list[Policy] = field(default_factory=list)
    audit: list[AuditEvent] = field(default_factory=list)
    sessions: dict[str, Session] = field(default_factory=dict)
    idps: dict[str, IdentityProvider] = field(default_factory=dict)
    mfa: dict[str, MfaSeed] = field(default_factory=dict)
    scim: dict[str, ScimToken] = field(default_factory=dict)
    platform_admin_set: set[str] = field(default_factory=set)

    async def load_all(self) -> dict[str, Any]:
        return {
            "orgs": list(self.orgs.values()),
            "units": list(self.units.values()),
            "groups": list(self.groups.values()),
            "group_members": sorted(self.group_members),
            "members": list(self.members.values()),
            "registry": list(self.registry.values()),
            "platform_admins": set(self.platform_admin_set),
        }

    async def save_org(self, o: Organization) -> None:
        self.orgs[o.org_id] = o

    async def save_unit(self, u: OrgUnit) -> None:
        self.units[u.unit_id] = u

    async def save_group(self, g: DeviceGroup) -> None:
        self.groups[g.group_id] = g

    async def set_group_member(self, group_id: str, device_id: str, org_id: str, member: bool) -> None:
        if member:
            self.group_members.add((group_id, device_id, org_id))
        else:
            self.group_members.discard((group_id, device_id, org_id))

    async def save_member(self, m: Membership) -> None:
        self.members[(m.org_id, m.username)] = m

    async def save_registry(self, r: DeviceRecord) -> None:
        self.registry[r.device_id] = r

    async def set_platform_admin(self, username: str, value: bool) -> None:
        (self.platform_admin_set.add if value else self.platform_admin_set.discard)(username)

    async def save_token(self, t: EnrollmentToken) -> None:
        self.tokens[t.token_id] = t

    async def tokens_for(self, org_id: str) -> list[EnrollmentToken]:
        return sorted(
            (t for t in self.tokens.values() if t.org_id == org_id), key=lambda t: t.created_at, reverse=True
        )

    async def token_by_hash(self, token_hash: str) -> EnrollmentToken | None:
        return next((t for t in self.tokens.values() if t.token_hash == token_hash), None)

    async def consume_token(self, token_id: str) -> bool:
        """Atomic use: False when exhausted / revoked meanwhile (race-free single use)."""
        t = self.tokens.get(token_id)
        if t is None or t.revoked_at or t.uses >= t.max_uses:
            return False
        t.uses += 1
        return True

    async def policies_for(self, org_id: str) -> list[Policy]:
        return [p for p in self.policies if p.org_id == org_id]

    async def save_policy(self, p: Policy) -> None:
        for i, q in enumerate(self.policies):
            if q.policy_id == p.policy_id and q.version == p.version:
                self.policies[i] = p
                return
        self.policies.append(p)

    async def append_audit(self, events: list[AuditEvent]) -> None:
        prev = self.audit[-1].hash if self.audit else GENESIS
        for e in events:
            e.prev_hash, e.hash = prev, chain(prev, e)
            prev = e.hash
            self.audit.append(e)

    async def search_audit(
        self, f: AuditFilter, limit: int, before_id: int | None = None
    ) -> list[tuple[int, AuditEvent]]:
        rows = [
            (i + 1, e)
            for i, e in enumerate(self.audit)
            if _audit_matches(e, f) and (before_id is None or i + 1 < before_id)
        ]
        return list(reversed(rows))[:limit]

    async def verify_audit(self, limit: int = 1_000_000) -> dict[str, Any]:
        prev = GENESIS
        for i, e in enumerate(self.audit[:limit]):
            if e.prev_hash != prev or chain(prev, e) != e.hash:
                return {"ok": False, "rows": i, "first_bad": i + 1}
            prev = e.hash
        return {"ok": True, "rows": min(limit, len(self.audit)), "first_bad": None, "head": prev}

    async def save_session(self, s: Session) -> None:
        self.sessions[s.session_id] = s

    async def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def sessions_of(self, username: str) -> list[Session]:
        return sorted(
            (s for s in self.sessions.values() if s.username == username),
            key=lambda s: s.created_at,
            reverse=True,
        )

    async def idps_for(self, org_id: str | None) -> list[IdentityProvider]:
        return [p for p in self.idps.values() if org_id is None or p.org_id == org_id]

    async def save_idp(self, p: IdentityProvider) -> None:
        self.idps[p.provider_id] = p

    async def get_mfa(self, username: str) -> MfaSeed | None:
        return self.mfa.get(username)

    async def save_mfa(self, m: MfaSeed) -> None:
        self.mfa[m.username] = m

    async def save_scim(self, t: ScimToken) -> None:
        self.scim[t.token_id] = t

    async def scim_by_hash(self, token_hash: str) -> ScimToken | None:
        return next((t for t in self.scim.values() if t.token_hash == token_hash), None)

    async def scim_for(self, org_id: str) -> list[ScimToken]:
        return [t for t in self.scim.values() if t.org_id == org_id]


# ------------------------------------------------------------------------------------------- SQL
def _org(r: OrganizationRow) -> Organization:
    return Organization(r.id, r.name, OrgStatus(r.status), r.created_at, r.quotas or {}, r.settings or {})


def _policy(r: PolicyRow) -> Policy:
    return Policy(
        r.policy_id,
        r.org_id,
        r.scope_type,
        r.scope_id,
        r.kind,
        r.version,
        PolicyStatus(r.status),
        r.body or {},
        list(r.locked or []),
        r.created_by,
        r.created_at,
        r.updated_by,
        r.updated_at,
        r.effective_from,
        r.effective_until,
        r.note or "",
    )


def _audit_row(e: AuditEvent) -> dict[str, Any]:
    return {
        "event_id": e.event_id,
        "at": e.at,
        "org_id": e.org_id,
        "actor_id": e.actor_id[:128],
        "actor_type": e.actor_type,
        "action": e.action[:64],
        "category": e.category,
        "resource_type": e.resource_type,
        "resource_id": (e.resource_id or None) and e.resource_id[:128],
        "result": e.result,
        "reason": (e.reason or None) and e.reason[:500],
        "severity": e.severity,
        "source": e.source,
        "request_id": e.request_id,
        "ip": e.ip,
        "metadata_": e.metadata,
        "prev_hash": e.prev_hash,
        "hash": e.hash,
    }


def _audit_from(r: AuditEventRow) -> AuditEvent:
    return AuditEvent(
        r.event_id,
        r.at,
        r.org_id,
        r.actor_id,
        r.actor_type,
        r.action,
        r.category,
        r.resource_type,
        r.resource_id,
        r.result,
        r.reason,
        r.severity,
        r.source,
        r.request_id,
        r.ip,
        r.metadata_ or {},
        r.prev_hash,
        r.hash,
    )


def _session(r: UserSessionRow) -> Session:
    return Session(
        r.id,
        r.username,
        r.org_id,
        r.created_at,
        r.expires_at,
        r.auth_method,
        r.mfa,
        r.last_seen_at,
        r.revoked_at,
        r.revoked_reason,
        r.ip,
        r.user_agent,
    )


class SqlGovernanceRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def _upsert(self, model: Any, values: dict[str, Any], keys: list[str]) -> None:
        stmt = pg_insert(model).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=keys, set_={k: stmt.excluded[k] for k in values if k not in keys}
        )
        async with self._db.sessions.begin() as s:
            await s.execute(stmt)

    async def load_all(self) -> dict[str, Any]:
        async with self._db.sessions() as s:
            orgs = [_org(r) for r in (await s.scalars(select(OrganizationRow))).all()]
            units = [
                OrgUnit(r.id, r.org_id, UnitKind(r.kind), r.name, r.parent_id, r.status)
                for r in (await s.scalars(select(OrgUnitRow))).all()
            ]
            groups = [
                DeviceGroup(
                    r.id, r.org_id, r.name, r.kind, r.unit_id, r.priority, list(r.tags or []), r.status
                )
                for r in (await s.scalars(select(DeviceGroupRow))).all()
            ]
            gm = [
                (r.group_id, r.device_id, r.org_id)
                for r in (await s.scalars(select(DeviceGroupMemberRow))).all()
            ]
            members = [
                Membership(
                    r.org_id, r.username, r.role, r.status, list(r.group_scope or []), r.source, r.created_at
                )
                for r in (await s.scalars(select(OrganizationMemberRow))).all()
            ]
            registry = [
                DeviceRecord(
                    r.device_id,
                    r.org_id,
                    Lifecycle(r.lifecycle),
                    r.enrolled_at,
                    r.enrollment_id,
                    [],
                    r.updated_at,
                    r.updated_by,
                    r.reason,
                )
                for r in (await s.scalars(select(DeviceRegistryRow))).all()
            ]
            admins = set(
                (await s.scalars(select(UserRow.username).where(UserRow.platform_admin.is_(True)))).all()
            )
        return {
            "orgs": orgs,
            "units": units,
            "groups": groups,
            "group_members": gm,
            "members": members,
            "registry": registry,
            "platform_admins": admins,
        }

    async def save_org(self, o: Organization) -> None:
        await self._upsert(
            OrganizationRow,
            {
                "id": o.org_id,
                "name": o.name,
                "status": o.status.value,
                "created_at": o.created_at,
                "quotas": o.quotas,
                "settings": o.settings,
            },
            ["id"],
        )

    async def save_unit(self, u: OrgUnit) -> None:
        await self._upsert(
            OrgUnitRow,
            {
                "id": u.unit_id,
                "org_id": u.org_id,
                "kind": u.kind.value,
                "name": u.name,
                "parent_id": u.parent_id,
                "status": u.status,
            },
            ["id"],
        )

    async def save_group(self, g: DeviceGroup) -> None:
        await self._upsert(
            DeviceGroupRow,
            {
                "id": g.group_id,
                "org_id": g.org_id,
                "name": g.name,
                "kind": g.kind,
                "unit_id": g.unit_id,
                "priority": g.priority,
                "tags": g.tags,
                "status": g.status,
            },
            ["id"],
        )

    async def set_group_member(self, group_id: str, device_id: str, org_id: str, member: bool) -> None:
        async with self._db.sessions.begin() as s:
            if member:
                await s.execute(
                    pg_insert(DeviceGroupMemberRow)
                    .values(group_id=group_id, device_id=device_id, org_id=org_id)
                    .on_conflict_do_nothing()
                )
            else:
                await s.execute(
                    delete(DeviceGroupMemberRow).where(
                        DeviceGroupMemberRow.group_id == group_id, DeviceGroupMemberRow.device_id == device_id
                    )
                )

    async def save_member(self, m: Membership) -> None:
        await self._upsert(
            OrganizationMemberRow,
            {
                "org_id": m.org_id,
                "username": m.username,
                "role": m.role,
                "status": m.status,
                "group_scope": m.group_scope,
                "source": m.source,
                "created_at": m.created_at or datetime.now().astimezone(),
            },
            ["org_id", "username"],
        )

    async def save_registry(self, r: DeviceRecord) -> None:
        await self._upsert(
            DeviceRegistryRow,
            {
                "device_id": r.device_id,
                "org_id": r.org_id,
                "lifecycle": r.lifecycle.value,
                "enrolled_at": r.enrolled_at,
                "enrollment_id": r.enrollment_id,
                "updated_at": r.updated_at,
                "updated_by": r.updated_by,
                "reason": r.reason,
            },
            ["device_id"],
        )

    async def set_platform_admin(self, username: str, value: bool) -> None:
        async with self._db.sessions.begin() as s:
            await s.execute(update(UserRow).where(UserRow.username == username).values(platform_admin=value))

    async def save_token(self, t: EnrollmentToken) -> None:
        await self._upsert(
            EnrollmentTokenRow,
            {
                "id": t.token_id,
                "org_id": t.org_id,
                "token_hash": t.token_hash,
                "created_by": t.created_by,
                "created_at": t.created_at,
                "expires_at": t.expires_at,
                "max_uses": t.max_uses,
                "uses": t.uses,
                "group_id": t.group_id,
                "revoked_at": t.revoked_at,
                "label": t.label,
            },
            ["id"],
        )

    @staticmethod
    def _token(r: EnrollmentTokenRow) -> EnrollmentToken:
        return EnrollmentToken(
            r.id,
            r.org_id,
            r.token_hash,
            r.created_by,
            r.created_at,
            r.expires_at,
            r.max_uses,
            r.uses,
            r.group_id,
            r.revoked_at,
            r.label,
        )

    async def tokens_for(self, org_id: str) -> list[EnrollmentToken]:
        q = (
            select(EnrollmentTokenRow)
            .where(EnrollmentTokenRow.org_id == org_id)
            .order_by(EnrollmentTokenRow.created_at.desc())
        )
        async with self._db.sessions() as s:
            return [self._token(r) for r in (await s.scalars(q.limit(500))).all()]

    async def token_by_hash(self, token_hash: str) -> EnrollmentToken | None:
        async with self._db.sessions() as s:
            r = (
                await s.scalars(select(EnrollmentTokenRow).where(EnrollmentTokenRow.token_hash == token_hash))
            ).first()
        return self._token(r) if r is not None else None

    async def consume_token(self, token_id: str) -> bool:
        async with self._db.sessions.begin() as s:
            res = await s.execute(
                update(EnrollmentTokenRow)
                .where(
                    EnrollmentTokenRow.id == token_id,
                    EnrollmentTokenRow.revoked_at.is_(None),
                    EnrollmentTokenRow.uses < EnrollmentTokenRow.max_uses,
                )
                .values(uses=EnrollmentTokenRow.uses + 1)
            )
            return bool(getattr(res, "rowcount", 0))

    async def policies_for(self, org_id: str) -> list[Policy]:
        q = (
            select(PolicyRow)
            .where(PolicyRow.org_id == org_id)
            .order_by(PolicyRow.policy_id, PolicyRow.version)
        )
        async with self._db.sessions() as s:
            return [_policy(r) for r in (await s.scalars(q)).all()]

    async def save_policy(self, p: Policy) -> None:
        values = {
            "policy_id": p.policy_id,
            "org_id": p.org_id,
            "scope_type": p.scope_type,
            "scope_id": p.scope_id,
            "kind": p.kind,
            "version": p.version,
            "status": p.status.value,
            "body": p.body,
            "locked": p.locked,
            "created_by": p.created_by,
            "created_at": p.created_at,
            "updated_by": p.updated_by,
            "updated_at": p.updated_at,
            "effective_from": p.effective_from,
            "effective_until": p.effective_until,
            "note": p.note,
        }
        stmt = pg_insert(PolicyRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_policies_version",
            set_={k: stmt.excluded[k] for k in ("status", "updated_by", "updated_at", "effective_until")},
        )
        async with self._db.sessions.begin() as s:
            await s.execute(stmt)

    async def append_audit(self, events: list[AuditEvent]) -> None:
        if not events:
            return
        async with self._db.sessions.begin() as s:
            await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": AUDIT_LOCK})
            prev = (
                await s.scalar(select(AuditEventRow.hash).order_by(AuditEventRow.id.desc()).limit(1))
                or GENESIS
            )
            for e in events:
                e.prev_hash, e.hash = prev, chain(prev, e)
                prev = e.hash
                s.add(AuditEventRow(**_audit_row(e)))

    async def search_audit(
        self, f: AuditFilter, limit: int, before_id: int | None = None
    ) -> list[tuple[int, AuditEvent]]:
        q = select(AuditEventRow)
        if f.org_id is not None:
            q = q.where(AuditEventRow.org_id == f.org_id)
        for col, val in (
            (AuditEventRow.actor_id, f.actor_id),
            (AuditEventRow.category, f.category),
            (AuditEventRow.resource_type, f.resource_type),
            (AuditEventRow.resource_id, f.resource_id),
            (AuditEventRow.result, f.result),
            (AuditEventRow.severity, f.severity),
        ):
            if val is not None:
                q = q.where(col == val)
        if f.action:
            q = q.where(AuditEventRow.action.startswith(f.action, autoescape=True))
        if f.since is not None:
            q = q.where(AuditEventRow.at >= f.since)
        if f.until is not None:
            q = q.where(AuditEventRow.at <= f.until)
        if before_id is not None:
            q = q.where(AuditEventRow.id < before_id)
        q = q.order_by(AuditEventRow.id.desc()).limit(limit)
        async with self._db.sessions() as s:
            return [(r.id, _audit_from(r)) for r in (await s.scalars(q)).all()]

    async def verify_audit(self, limit: int = 1_000_000) -> dict[str, Any]:
        prev, n = GENESIS, 0
        async with self._db.sessions() as s:
            for r in (await s.scalars(select(AuditEventRow).order_by(AuditEventRow.id).limit(limit))).all():
                e = _audit_from(r)
                if e.prev_hash != prev or chain(prev, e) != e.hash:
                    return {"ok": False, "rows": n, "first_bad": r.id}
                prev, n = e.hash, n + 1
        return {"ok": True, "rows": n, "first_bad": None, "head": prev}

    async def save_session(self, x: Session) -> None:
        await self._upsert(
            UserSessionRow,
            {
                "id": x.session_id,
                "username": x.username,
                "org_id": x.org_id,
                "created_at": x.created_at,
                "expires_at": x.expires_at,
                "last_seen_at": x.last_seen_at,
                "revoked_at": x.revoked_at,
                "revoked_reason": x.revoked_reason,
                "auth_method": x.auth_method,
                "mfa": x.mfa,
                "ip": x.ip,
                "user_agent": x.user_agent,
            },
            ["id"],
        )

    async def get_session(self, session_id: str) -> Session | None:
        async with self._db.sessions() as s:
            r = await s.get(UserSessionRow, session_id)
        return _session(r) if r is not None else None

    async def sessions_of(self, username: str) -> list[Session]:
        q = (
            select(UserSessionRow)
            .where(UserSessionRow.username == username)
            .order_by(UserSessionRow.created_at.desc())
        )
        async with self._db.sessions() as s:
            return [_session(r) for r in (await s.scalars(q.limit(200))).all()]

    async def idps_for(self, org_id: str | None) -> list[IdentityProvider]:
        q = select(IdentityProviderRow)
        if org_id is not None:
            q = q.where(IdentityProviderRow.org_id == org_id)
        async with self._db.sessions() as s:
            return [
                IdentityProvider(
                    r.id,
                    r.org_id,
                    r.kind,
                    r.name,
                    r.status,
                    r.config or {},
                    r.created_by,
                    r.created_at,
                    r.updated_at,
                )
                for r in (await s.scalars(q)).all()
            ]

    async def save_idp(self, p: IdentityProvider) -> None:
        await self._upsert(
            IdentityProviderRow,
            {
                "id": p.provider_id,
                "org_id": p.org_id,
                "kind": p.kind,
                "name": p.name,
                "status": p.status,
                "config": p.config,
                "created_by": p.created_by,
                "created_at": p.created_at,
                "updated_at": p.updated_at,
            },
            ["id"],
        )

    async def get_mfa(self, username: str) -> MfaSeed | None:
        async with self._db.sessions() as s:
            r = await s.get(UserMfaRow, username)
        return (
            MfaSeed(r.username, r.secret_enc, r.enabled, r.created_at, r.last_step) if r is not None else None
        )

    async def save_mfa(self, m: MfaSeed) -> None:
        await self._upsert(
            UserMfaRow,
            {
                "username": m.username,
                "secret_enc": m.secret_enc,
                "enabled": m.enabled,
                "created_at": m.created_at,
                "last_step": m.last_step,
            },
            ["username"],
        )

    async def save_scim(self, t: ScimToken) -> None:
        await self._upsert(
            ScimTokenRow,
            {
                "id": t.token_id,
                "org_id": t.org_id,
                "token_hash": t.token_hash,
                "label": t.label,
                "created_by": t.created_by,
                "created_at": t.created_at,
                "revoked_at": t.revoked_at,
            },
            ["id"],
        )

    async def scim_by_hash(self, token_hash: str) -> ScimToken | None:
        async with self._db.sessions() as s:
            r = (await s.scalars(select(ScimTokenRow).where(ScimTokenRow.token_hash == token_hash))).first()
        return (
            ScimToken(r.id, r.org_id, r.token_hash, r.label, r.created_by, r.created_at, r.revoked_at)
            if r
            else None
        )

    async def scim_for(self, org_id: str) -> list[ScimToken]:
        async with self._db.sessions() as s:
            rows = (await s.scalars(select(ScimTokenRow).where(ScimTokenRow.org_id == org_id))).all()
        return [
            ScimToken(r.id, r.org_id, r.token_hash, r.label, r.created_by, r.created_at, r.revoked_at)
            for r in rows
        ]


def new_id() -> str:
    return uuid.uuid4().hex
