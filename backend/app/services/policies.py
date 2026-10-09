"""PolicyService (Phase 9): draft -> validate (+ preview) -> publish -> rollback / archive, and deterministic
evaluation of the effective policy for a device (cached; invalidated on every publication)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.core.metrics import POLICY_EVALUATIONS
from app.domain.governance import policies as pol
from app.domain.governance.policies import Policy, PolicyStatus
from app.domain.tenancy.models import UnitKind
from app.services.tenancy import TenancyError, TenancyService, TenantContext

UNIT_ORDER = {UnitKind.BUSINESS_UNIT: 1, UnitKind.DEPARTMENT: 2, UnitKind.TEAM: 3}


class PolicyService:
    def __init__(self, repo: Any, tenancy: TenancyService, audit: Any = None) -> None:
        self.repo = repo
        self.tenancy = tenancy
        self.audit = audit
        self._by_org: dict[str, list[Policy]] = {}
        self._cache: dict[tuple[str, str, str | None], tuple[dict[str, Any], dict[str, str]]] = {}
        self.agent_version_of: Callable[[str], str | None] = lambda _d: None  # set by the container

    async def load(self) -> None:
        for org in list(self.tenancy.orgs):
            self._by_org[org] = await self.repo.policies_for(org)
        self._cache.clear()

    def _policies(self, org_id: str) -> list[Policy]:
        return self._by_org.setdefault(org_id, [])

    def list_policies(self, org_id: str, kind: str | None = None) -> list[Policy]:
        return [p for p in self._policies(org_id) if kind is None or p.kind == kind]

    def published(self, org_id: str, kind: str, now: datetime | None = None) -> list[Policy]:
        now = now or datetime.now(UTC)
        return [p for p in self._policies(org_id) if p.kind == kind and p.active(now)]

    def _check_scope(self, ctx: TenantContext, scope_type: str, scope_id: str) -> None:
        t = self.tenancy
        ok = {
            "organization": scope_id == ctx.org_id,
            "business_unit": scope_id in t.units
            and t.units[scope_id].org_id == ctx.org_id
            and t.units[scope_id].kind == UnitKind.BUSINESS_UNIT,
            "department": scope_id in t.units
            and t.units[scope_id].org_id == ctx.org_id
            and t.units[scope_id].kind == UnitKind.DEPARTMENT,
            "team": scope_id in t.units
            and t.units[scope_id].org_id == ctx.org_id
            and t.units[scope_id].kind == UnitKind.TEAM,
            "device_group": scope_id in t.groups and t.groups[scope_id].org_id == ctx.org_id,
            "device": t.org_of(scope_id) == ctx.org_id,
        }.get(scope_type, False)
        if not ok:
            raise TenancyError("SCOPE_NOT_FOUND", "Unknown policy scope", 404)

    def _get(self, ctx: TenantContext, policy_id: str, version: int | None = None) -> Policy:
        versions = [p for p in self._policies(ctx.org_id) if p.policy_id == policy_id]
        if not versions:
            raise TenancyError("POLICY_NOT_FOUND", "Unknown policy", 404)
        if version is None:
            return max(versions, key=lambda p: p.version)
        p = next((p for p in versions if p.version == version), None)
        if p is None:
            raise TenancyError("POLICY_NOT_FOUND", "Unknown policy version", 404)
        return p

    async def save_draft(
        self,
        ctx: TenantContext,
        kind: str,
        scope_type: str,
        scope_id: str,
        body: dict[str, Any],
        locked: list[str],
        note: str = "",
        effective_from: datetime | None = None,
        effective_until: datetime | None = None,
    ) -> Policy:
        if kind not in pol.SCHEMAS:
            raise TenancyError("INVALID_POLICY", f"unknown policy kind {kind}", 422)
        self._check_scope(ctx, scope_type, scope_id)
        existing = [
            p
            for p in self._policies(ctx.org_id)
            if p.kind == kind and p.scope_type == scope_type and p.scope_id == scope_id
        ]
        now = datetime.now(UTC)
        draft = next((p for p in existing if p.status == PolicyStatus.DRAFT), None)
        if draft is not None:  # one open draft per policy: editing replaces its content (same version)
            draft.body, draft.locked, draft.note = dict(body), list(locked), note[:300]
            draft.updated_by, draft.updated_at = ctx.username, now
            draft.effective_from, draft.effective_until = effective_from, effective_until
            p = draft
        else:
            policy_id = existing[0].policy_id if existing else uuid.uuid4().hex
            version = max((q.version for q in existing), default=0) + 1
            p = Policy(
                policy_id,
                ctx.org_id,
                scope_type,
                scope_id,
                kind,
                version,
                PolicyStatus.DRAFT,
                dict(body),
                list(locked),
                ctx.username,
                now,
                ctx.username,
                now,
                effective_from,
                effective_until,
                note[:300],
            )
            self._policies(ctx.org_id).append(p)
        await self.repo.save_policy(p)
        self._audit(ctx, "policy.draft_saved", p)
        return p

    def validate(self, ctx: TenantContext, policy_id: str, version: int | None = None) -> dict[str, Any]:
        p = self._get(ctx, policy_id, version)
        errors, warnings = pol.validate(p, self.published(ctx.org_id, p.kind))
        return {"policy": p.public(), "errors": errors, "warnings": warnings, "preview": self.preview(ctx, p)}

    def preview(self, ctx: TenantContext, p: Policy) -> dict[str, Any]:
        """Effective values in the policy's scope with and without this version (affected devices counted)."""
        devices = sorted(self._scope_devices(ctx.org_id, p))
        sample = devices[0] if devices else None
        before = self.effective(p.kind, ctx.org_id, sample)[0]
        after = self.effective(p.kind, ctx.org_id, sample, extra=p)[0]
        changed = {
            k: {"from": before.get(k), "to": after.get(k)} for k in after if after.get(k) != before.get(k)
        }
        out: dict[str, Any] = {"affected_devices": len(devices), "sample_device": sample, "changes": changed}
        if p.kind == "agent" and p.body.get("blocked_versions"):
            blocked = set(p.body["blocked_versions"])
            out["devices_on_blocked_versions"] = sum(
                1 for d in devices if self.agent_version_of(d) in blocked
            )
        return out

    def _scope_devices(self, org_id: str, p: Policy) -> set[str]:
        t = self.tenancy
        org = t.org_devices(org_id)
        if p.scope_type == "organization":
            return org
        if p.scope_type == "device":
            return {p.scope_id} & org
        if p.scope_type == "device_group":
            return {d for d in org if p.scope_id in t.device_groups.get(d, set())}
        return {d for d in org if p.scope_id in self._device_units(d)}

    def _device_units(self, device_id: str) -> set[str]:
        t, out = self.tenancy, set()
        for gid in t.device_groups.get(device_id, set()):
            group = t.groups.get(gid)
            unit = t.units.get(group.unit_id) if group is not None and group.unit_id else None
            while unit is not None and unit.unit_id not in out:
                out.add(unit.unit_id)
                unit = t.units.get(unit.parent_id) if unit.parent_id else None
        return out

    async def publish(self, ctx: TenantContext, policy_id: str, version: int) -> Policy:
        p = self._get(ctx, policy_id, version)
        if p.status != PolicyStatus.DRAFT:
            raise TenancyError("NOT_DRAFT", "Only a draft can be published", 409)
        errors, warnings = pol.validate(p, self.published(ctx.org_id, p.kind))
        if errors:
            raise TenancyError("POLICY_INVALID", "; ".join(errors), 422)
        now = datetime.now(UTC)
        for q in self._policies(ctx.org_id):
            if q.policy_id == p.policy_id and q.status == PolicyStatus.PUBLISHED:
                q.status, q.updated_by, q.updated_at = PolicyStatus.ARCHIVED, ctx.username, now
                await self.repo.save_policy(q)
        p.status, p.updated_by, p.updated_at = PolicyStatus.PUBLISHED, ctx.username, now
        await self.repo.save_policy(p)
        self._cache.clear()
        self._audit(ctx, "policy.published", p, {"warnings": warnings})
        return p

    async def rollback(self, ctx: TenantContext, policy_id: str, to_version: int) -> Policy:
        src = self._get(ctx, policy_id, to_version)
        if src.status == PolicyStatus.DRAFT:
            raise TenancyError("NOT_PUBLISHED", "Roll back to a previously published version", 409)
        if any(
            q.policy_id == policy_id and q.status == PolicyStatus.DRAFT for q in self._policies(ctx.org_id)
        ):
            raise TenancyError("DRAFT_OPEN", "Discard or publish the open draft first", 409)
        draft = await self.save_draft(
            ctx, src.kind, src.scope_type, src.scope_id, src.body, src.locked, f"rollback to v{to_version}"
        )
        p = await self.publish(ctx, policy_id, draft.version)
        self._audit(ctx, "policy.rolled_back", p, {"to_version": to_version})
        return p

    async def archive(self, ctx: TenantContext, policy_id: str) -> Policy:
        p = self._get(ctx, policy_id)
        now = datetime.now(UTC)
        changed = None
        for q in self._policies(ctx.org_id):
            if q.policy_id == policy_id and q.status in (PolicyStatus.PUBLISHED, PolicyStatus.DRAFT):
                q.status, q.updated_by, q.updated_at = PolicyStatus.ARCHIVED, ctx.username, now
                await self.repo.save_policy(q)
                changed = q
        self._cache.clear()
        self._audit(ctx, "policy.archived", changed or p)
        return changed or p

    # ------------------------------------------------------------------ evaluation
    def effective(
        self, kind: str, org_id: str | None, device_id: str | None = None, extra: Policy | None = None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if org_id is None:
            return pol.platform_default(kind), {k: "platform" for k in pol.SCHEMAS[kind]}
        key = (org_id, kind, device_id)
        if extra is None and key in self._cache:
            return self._cache[key]
        POLICY_EVALUATIONS.inc()
        now = datetime.now(UTC)
        pub = self.published(org_id, kind, now)
        if extra is not None:
            pub = [q for q in pub if q.policy_id != extra.policy_id] + [extra]
        chain = [q for q in pub if q.scope_type == "organization" and q.scope_id == org_id]
        if device_id is not None:
            t = self.tenancy
            units = self._device_units(device_id)
            chain += sorted(
                (
                    q
                    for q in pub
                    if q.scope_type in ("business_unit", "department", "team") and q.scope_id in units
                ),
                key=lambda q: pol.SCOPE_RANK[q.scope_type],
            )
            groups = t.device_groups.get(device_id, set())
            gq = [q for q in pub if q.scope_type == "device_group" and q.scope_id in groups]
            # lower priority number wins -> applied last; ties broken by group id (deterministic)
            chain += sorted(
                gq,
                key=lambda q: (-t.groups[q.scope_id].priority if q.scope_id in t.groups else 0, q.scope_id),
            )
            chain += [q for q in pub if q.scope_type == "device" and q.scope_id == device_id]
        result = pol.effective(kind, chain)
        if extra is None:
            self._cache[key] = result
        return result

    def value(self, kind: str, name: str, device_id: str | None = None, org_id: str | None = None) -> Any:
        org = org_id or (self.tenancy.org_of(device_id) if device_id else None)
        return self.effective(kind, org, device_id)[0][name]

    def _audit(self, ctx: TenantContext, action: str, p: Policy, extra: dict[str, Any] | None = None) -> None:
        if self.audit is not None:
            self.audit.record(
                ctx.org_id,
                ctx.username,
                ctx.actor_type,
                action,
                "policy",
                resource_type="policy",
                resource_id=f"{p.policy_id}:v{p.version}",
                metadata={
                    "kind": p.kind,
                    "scope": f"{p.scope_type}:{p.scope_id}",
                    "fields": sorted(p.body),
                    "locked": p.locked,
                    **(extra or {}),
                },
            )
