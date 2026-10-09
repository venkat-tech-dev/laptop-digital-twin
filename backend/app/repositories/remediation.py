"""Storage of remediations (Phase 8) and their append-only, hash-chained audit trail.

The audit chain is global per installation: every row stores the previous row's hash, and its own hash
covers its content plus that previous hash, so editing or removing a row breaks verification. In
PostgreSQL a trigger additionally rejects UPDATE / DELETE, and writers serialise on an advisory lock.
Audit rows are never purged by retention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.domain.remediation.models import AuditEntry, Mode, Remediation, Status, chain_hash
from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import RemediationAuditRow, RemediationRow

GENESIS = "0" * 64
AUDIT_LOCK = 0x4C445452  # pg_advisory_xact_lock key ("LDTR")


@dataclass(frozen=True, slots=True)
class RemediationFilter:
    device_id: str | None = None
    devices: frozenset[str] | None = None  # visibility restriction (employees)
    statuses: tuple[str, ...] = ()
    action_type: str | None = None
    risk: str | None = None
    requested_by: str | None = None
    diagnosis_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None


class RemediationRepository(Protocol):
    async def save(self, r: Remediation) -> None: ...

    async def get(self, remediation_id: str) -> Remediation | None: ...

    async def search(self, f: RemediationFilter, limit: int, offset: int = 0) -> list[Remediation]: ...

    async def recent(self, since: datetime) -> list[Remediation]: ...

    async def verify_audit(self, limit: int = 100_000) -> dict[str, Any]: ...


def _body(r: Remediation) -> dict[str, Any]:
    d = r.to_dict(full=True)
    d.pop("audit", None)
    return d


def _from(body: dict[str, Any], audit: list[AuditEntry]) -> Remediation:
    def dt(k: str) -> datetime | None:
        v = body.get(k)
        return datetime.fromisoformat(v) if v else None

    return Remediation(
        remediation_id=body["remediation_id"],
        tenant_id=body["tenant_id"],
        device_id=body["device_id"],
        action_type=body["action_type"],
        action_version=int(body.get("action_version") or 1),
        action_name=body["action_name"],
        description=body["description"],
        risk_level=body["risk_level"],
        requires_approval=bool(body["requires_approval"]),
        approval_policy=body["approval_policy"],
        parameters=body.get("parameters") or {},
        preconditions=body.get("preconditions") or [],
        verification_rules=body.get("verification_rules") or {},
        rollback_strategy=body["rollback_strategy"],
        status=Status(body["status"]),
        requested_by=body["requested_by"],
        created_at=datetime.fromisoformat(body["created_at"]),
        updated_at=datetime.fromisoformat(body["updated_at"]),
        correlation_id=body["correlation_id"],
        execution_id=body["execution_id"],
        alert_id=body.get("alert_id"),
        diagnosis_id=body.get("diagnosis_id"),
        prediction_id=body.get("prediction_id"),
        reason=body.get("reason") or "",
        evidence=body.get("evidence") or [],
        diagnosis_confidence=body.get("diagnosis_confidence"),
        action_confidence=body.get("action_confidence"),
        expected_success_probability=body.get("expected_success_probability"),
        recommendation_source=body.get("recommendation_source") or "rules",
        mode=Mode(body.get("mode") or "IMMEDIATE"),
        scheduled_at=dt("scheduled_at"),
        approval_expires_at=dt("approval_expires_at"),
        approved_by=body.get("approved_by"),
        approval_at=dt("approval_at"),
        rejected_by=body.get("rejected_by"),
        policy_version=int(body.get("policy_version") or 0),
        started_at=dt("started_at"),
        completed_at=dt("completed_at"),
        failed_at=dt("failed_at"),
        result=body.get("result"),
        failure_reason=body.get("failure_reason"),
        dry_run=bool(body.get("dry_run")),
        execution=body.get("execution") or {},
        verification=body.get("verification") or {},
        audit=audit,
    )


def _matches(r: Remediation, f: RemediationFilter) -> bool:
    return (
        (f.device_id is None or r.device_id == f.device_id)
        and (f.devices is None or r.device_id in f.devices)
        and (not f.statuses or r.status.value in f.statuses)
        and (f.action_type is None or r.action_type == f.action_type)
        and (f.risk is None or r.risk_level == f.risk)
        and (f.requested_by is None or r.requested_by == f.requested_by)
        and (f.diagnosis_id is None or r.diagnosis_id == f.diagnosis_id)
        and (f.since is None or r.created_at >= f.since)
        and (f.until is None or r.created_at <= f.until)
    )


class MemoryRemediationRepository:
    def __init__(self) -> None:
        self.items: dict[str, Remediation] = {}
        self.audit_rows: list[dict[str, Any]] = []
        self._persisted: dict[str, int] = {}  # remediation -> audit entries written

    async def save(self, r: Remediation) -> None:
        self.items[r.remediation_id] = r
        done = self._persisted.get(r.remediation_id, 0)
        for e in r.audit[done:]:
            prev = self.audit_rows[-1]["hash"] if self.audit_rows else GENESIS
            self.audit_rows.append(
                {
                    "remediation_id": r.remediation_id,
                    "entry": e,
                    "prev_hash": prev,
                    "hash": chain_hash(prev, r.remediation_id, e),
                }
            )
        self._persisted[r.remediation_id] = len(r.audit)

    async def get(self, remediation_id: str) -> Remediation | None:
        return self.items.get(remediation_id)

    async def search(self, f: RemediationFilter, limit: int, offset: int = 0) -> list[Remediation]:
        rows = sorted(
            (r for r in self.items.values() if _matches(r, f)), key=lambda r: r.created_at, reverse=True
        )
        return rows[offset : offset + limit]

    async def recent(self, since: datetime) -> list[Remediation]:
        return [r for r in self.items.values() if r.is_open or r.updated_at >= since]

    async def verify_audit(self, limit: int = 100_000) -> dict[str, Any]:
        prev = GENESIS
        for i, row in enumerate(self.audit_rows[:limit]):
            if (
                row["prev_hash"] != prev
                or chain_hash(prev, row["remediation_id"], row["entry"]) != row["hash"]
            ):
                return {"ok": False, "rows": i, "first_bad": i}
            prev = row["hash"]
        return {"ok": True, "rows": min(limit, len(self.audit_rows)), "first_bad": None, "head": prev}


class SqlRemediationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._persisted: dict[str, int] = {}

    async def save(self, r: Remediation) -> None:
        values = {
            "id": r.remediation_id,
            "tenant_id": r.tenant_id,
            "device_id": r.device_id,
            "action_type": r.action_type,
            "risk_level": r.risk_level,
            "status": r.status.value,
            "requested_by": r.requested_by,
            "approved_by": r.approved_by,
            "alert_id": r.alert_id,
            "diagnosis_id": r.diagnosis_id,
            "prediction_id": r.prediction_id,
            "correlation_id": r.correlation_id,
            "execution_id": r.execution_id,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "body": _body(r),
        }
        stmt = pg_insert(RemediationRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[RemediationRow.id], set_={k: stmt.excluded[k] for k in values if k != "id"}
        )
        async with self._db.sessions.begin() as session:
            await session.execute(stmt)
            # records read from the database are hydrated with their persisted count; unknown = new
            done = self._persisted.get(r.remediation_id, 0)
            new = r.audit[done:]
            if new:
                await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": AUDIT_LOCK})
                prev = (
                    await session.scalar(
                        select(RemediationAuditRow.hash).order_by(RemediationAuditRow.id.desc()).limit(1)
                    )
                    or GENESIS
                )
                for e in new:
                    h = chain_hash(prev, r.remediation_id, e)
                    session.add(
                        RemediationAuditRow(
                            remediation_id=r.remediation_id,
                            device_id=r.device_id,
                            at=e.at,
                            actor=e.actor[:128],
                            action=e.action[:48],
                            from_status=e.from_status,
                            to_status=e.to_status,
                            detail=e.detail,
                            prev_hash=prev,
                            hash=h,
                        )
                    )
                    prev = h
        self._persisted[r.remediation_id] = len(r.audit)

    async def _audit(self, ids: list[str]) -> dict[str, list[AuditEntry]]:
        out: dict[str, list[AuditEntry]] = {i: [] for i in ids}
        if not ids:
            return out
        q = (
            select(RemediationAuditRow)
            .where(RemediationAuditRow.remediation_id.in_(ids))
            .order_by(RemediationAuditRow.id)
        )
        async with self._db.sessions() as session:
            for a in (await session.scalars(q)).all():
                out[a.remediation_id].append(
                    AuditEntry(a.at, a.actor, a.action, a.from_status, a.to_status, a.detail or {})
                )
        return out

    async def _hydrate(self, rows: list[RemediationRow]) -> list[Remediation]:
        audits = await self._audit([r.id for r in rows])
        out = []
        for row in rows:
            r = _from(row.body, audits[row.id])
            self._persisted[r.remediation_id] = len(r.audit)
            out.append(r)
        return out

    async def get(self, remediation_id: str) -> Remediation | None:
        async with self._db.sessions() as session:
            row = await session.get(RemediationRow, remediation_id)
        return (await self._hydrate([row]))[0] if row is not None else None

    async def search(self, f: RemediationFilter, limit: int, offset: int = 0) -> list[Remediation]:
        q = select(RemediationRow)
        if f.device_id is not None:
            q = q.where(RemediationRow.device_id == f.device_id)
        if f.devices is not None:
            q = q.where(RemediationRow.device_id.in_(sorted(f.devices) or [""]))
        if f.statuses:
            q = q.where(RemediationRow.status.in_(f.statuses))
        if f.action_type:
            q = q.where(RemediationRow.action_type == f.action_type)
        if f.risk:
            q = q.where(RemediationRow.risk_level == f.risk)
        if f.requested_by:
            q = q.where(RemediationRow.requested_by == f.requested_by)
        if f.diagnosis_id:
            q = q.where(RemediationRow.diagnosis_id == f.diagnosis_id)
        if f.since is not None:
            q = q.where(RemediationRow.created_at >= f.since)
        if f.until is not None:
            q = q.where(RemediationRow.created_at <= f.until)
        q = q.order_by(RemediationRow.created_at.desc()).limit(limit).offset(offset)
        async with self._db.sessions() as session:
            rows = list((await session.scalars(q)).all())
        return await self._hydrate(rows)

    async def recent(self, since: datetime) -> list[Remediation]:
        open_ = [
            s.value
            for s in Status
            if s
            not in (
                Status.REJECTED,
                Status.SUCCEEDED,
                Status.PARTIALLY_SUCCEEDED,
                Status.FAILED,
                Status.CANCELLED,
                Status.EXPIRED,
                Status.ROLLED_BACK,
                Status.ROLLBACK_FAILED,
            )
        ]
        q = select(RemediationRow).where(
            (RemediationRow.status.in_(open_)) | (RemediationRow.updated_at >= since)
        )
        async with self._db.sessions() as session:
            rows = list((await session.scalars(q)).all())
        return await self._hydrate(rows)

    async def verify_audit(self, limit: int = 100_000) -> dict[str, Any]:
        q = select(RemediationAuditRow).order_by(RemediationAuditRow.id).limit(limit)
        prev = GENESIS
        n = 0
        async with self._db.sessions() as session:
            for a in (await session.scalars(q)).all():
                e = AuditEntry(a.at, a.actor, a.action, a.from_status, a.to_status, a.detail or {})
                if a.prev_hash != prev or chain_hash(prev, a.remediation_id, e) != a.hash:
                    return {"ok": False, "rows": n, "first_bad": a.id}
                prev = a.hash
                n += 1
        return {"ok": True, "rows": n, "first_bad": None, "head": prev}
