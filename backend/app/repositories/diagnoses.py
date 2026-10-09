"""Storage of diagnoses (Phase 7): immutable versions per series, feedback, expiry and retention."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.domain.diagnosis.models import SCALAR_KEYS, Diagnosis, diagnosis_body, diagnosis_from
from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import DiagnosisFeedbackRow, DiagnosisRow

CURRENT_STATUSES = ("GENERATING", "AVAILABLE", "LOW_CONFIDENCE", "INSUFFICIENT_EVIDENCE", "FAILED")
EXPIRABLE = ("AVAILABLE", "LOW_CONFIDENCE", "INSUFFICIENT_EVIDENCE")


@dataclass(frozen=True, slots=True)
class DiagnosisFilter:
    current_only: bool = False  # hide SUPERSEDED / EXPIRED
    statuses: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    alert_id: str | None = None
    anomaly_id: str | None = None
    prediction_id: str | None = None
    since: datetime | None = None


class DiagnosisRepository(Protocol):
    async def save(self, d: Diagnosis) -> None: ...

    async def get(self, diagnosis_id: str) -> Diagnosis | None: ...

    async def versions(self, series_id: str) -> list[Diagnosis]: ...

    async def search(
        self, device_id: str | None, f: DiagnosisFilter, limit: int, offset: int = 0
    ) -> list[Diagnosis]: ...

    async def by_fingerprint(self, device_id: str, fingerprint: str, since: datetime) -> Diagnosis | None: ...

    async def due_expiry(self, now: datetime, limit: int = 200) -> list[Diagnosis]: ...

    async def add_feedback(self, item: dict[str, Any]) -> None: ...

    async def feedback_for(self, diagnosis_ids: list[str]) -> list[dict[str, Any]]: ...

    async def feedback_stats(self) -> dict[str, dict[str, int]]: ...

    async def purge_older_than(self, cutoff: datetime) -> int: ...


def _matches(d: Diagnosis, device_id: str | None, f: DiagnosisFilter) -> bool:
    s = d.status.value
    return (
        (device_id is None or d.device_id == device_id)
        and (not f.current_only or s in CURRENT_STATUSES)
        and (not f.statuses or s in f.statuses)
        and (not f.types or d.diagnosis_type.value in f.types)
        and (f.alert_id is None or d.alert_id == f.alert_id)
        and (f.anomaly_id is None or d.anomaly_id == f.anomaly_id)
        and (f.prediction_id is None or d.prediction_id == f.prediction_id)
        and (f.since is None or d.created_at >= f.since)
    )


class MemoryDiagnosisRepository:
    def __init__(self, max_items: int = 5000) -> None:
        self.items: dict[str, Diagnosis] = {}
        self.feedback: list[dict[str, Any]] = []
        self._max = max_items

    async def save(self, d: Diagnosis) -> None:
        self.items[d.diagnosis_id] = d
        if len(self.items) > self._max:
            oldest = min(self.items.values(), key=lambda x: x.updated_at)
            self.items.pop(oldest.diagnosis_id, None)

    async def get(self, diagnosis_id: str) -> Diagnosis | None:
        return self.items.get(diagnosis_id)

    async def versions(self, series_id: str) -> list[Diagnosis]:
        return sorted((d for d in self.items.values() if d.series_id == series_id), key=lambda d: d.version)

    async def search(
        self, device_id: str | None, f: DiagnosisFilter, limit: int, offset: int = 0
    ) -> list[Diagnosis]:
        rows = [d for d in self.items.values() if _matches(d, device_id, f)]
        rows.sort(key=lambda d: d.created_at, reverse=True)
        return rows[offset : offset + limit]

    async def by_fingerprint(self, device_id: str, fingerprint: str, since: datetime) -> Diagnosis | None:
        rows = [
            d
            for d in self.items.values()
            if d.device_id == device_id
            and d.context_fingerprint == fingerprint
            and d.created_at >= since
            and d.status.value in EXPIRABLE
        ]
        return max(rows, key=lambda d: d.created_at, default=None)

    async def due_expiry(self, now: datetime, limit: int = 200) -> list[Diagnosis]:
        return [
            d
            for d in self.items.values()
            if d.status.value in EXPIRABLE and d.expires_at is not None and d.expires_at <= now
        ][:limit]

    async def add_feedback(self, item: dict[str, Any]) -> None:
        self.feedback.append(dict(item))

    async def feedback_for(self, diagnosis_ids: list[str]) -> list[dict[str, Any]]:
        ids = set(diagnosis_ids)
        return [f for f in self.feedback if f["diagnosis_id"] in ids]

    async def feedback_stats(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for f in self.feedback:
            row = out.setdefault(f["diagnosis_type"], {})
            row[f["verdict"]] = row.get(f["verdict"], 0) + 1
        return out

    async def purge_older_than(self, cutoff: datetime) -> int:
        old = {k for k, d in self.items.items() if d.created_at < cutoff}
        for k in old:
            self.items.pop(k, None)
        self.feedback = [f for f in self.feedback if f["diagnosis_id"] not in old]
        return len(old)


def _values(d: Diagnosis) -> dict[str, Any]:
    v: dict[str, Any] = {k: getattr(d, k) for k in SCALAR_KEYS}
    v["status"] = d.status.value
    v["diagnosis_type"] = d.diagnosis_type.value
    v["id"] = d.diagnosis_id
    v["body"] = diagnosis_body(d)
    return v


def _from_row(r: DiagnosisRow) -> Diagnosis:
    return diagnosis_from(r.id, {k: getattr(r, k) for k in SCALAR_KEYS}, r.body or {})


class SqlDiagnosisRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def save(self, d: Diagnosis) -> None:
        values = _values(d)
        stmt = pg_insert(DiagnosisRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[DiagnosisRow.id], set_={k: stmt.excluded[k] for k in values if k != "id"}
        )
        async with self._db.sessions.begin() as session:
            await session.execute(stmt)

    async def get(self, diagnosis_id: str) -> Diagnosis | None:
        async with self._db.sessions() as session:
            row = await session.get(DiagnosisRow, diagnosis_id)
            return _from_row(row) if row is not None else None

    async def versions(self, series_id: str) -> list[Diagnosis]:
        q = select(DiagnosisRow).where(DiagnosisRow.series_id == series_id).order_by(DiagnosisRow.version)
        async with self._db.sessions() as session:
            return [_from_row(r) for r in (await session.scalars(q)).all()]

    async def search(
        self, device_id: str | None, f: DiagnosisFilter, limit: int, offset: int = 0
    ) -> list[Diagnosis]:
        q = select(DiagnosisRow)
        if device_id is not None:
            q = q.where(DiagnosisRow.device_id == device_id)
        if f.current_only:
            q = q.where(DiagnosisRow.status.in_(CURRENT_STATUSES))
        if f.statuses:
            q = q.where(DiagnosisRow.status.in_(f.statuses))
        if f.types:
            q = q.where(DiagnosisRow.diagnosis_type.in_(f.types))
        if f.alert_id is not None:
            q = q.where(DiagnosisRow.alert_id == f.alert_id)
        if f.anomaly_id is not None:
            q = q.where(DiagnosisRow.anomaly_id == f.anomaly_id)
        if f.prediction_id is not None:
            q = q.where(DiagnosisRow.prediction_id == f.prediction_id)
        if f.since is not None:
            q = q.where(DiagnosisRow.created_at >= f.since)
        q = q.order_by(DiagnosisRow.created_at.desc()).limit(limit).offset(offset)
        async with self._db.sessions() as session:
            return [_from_row(r) for r in (await session.scalars(q)).all()]

    async def by_fingerprint(self, device_id: str, fingerprint: str, since: datetime) -> Diagnosis | None:
        q = (
            select(DiagnosisRow)
            .where(
                DiagnosisRow.device_id == device_id,
                DiagnosisRow.context_fingerprint == fingerprint,
                DiagnosisRow.created_at >= since,
                DiagnosisRow.status.in_(EXPIRABLE),
            )
            .order_by(DiagnosisRow.created_at.desc())
            .limit(1)
        )
        async with self._db.sessions() as session:
            row = (await session.scalars(q)).first()
            return _from_row(row) if row is not None else None

    async def due_expiry(self, now: datetime, limit: int = 200) -> list[Diagnosis]:
        q = (
            select(DiagnosisRow)
            .where(DiagnosisRow.status.in_(EXPIRABLE), DiagnosisRow.expires_at <= now)
            .limit(limit)
        )
        async with self._db.sessions() as session:
            return [_from_row(r) for r in (await session.scalars(q)).all()]

    async def add_feedback(self, item: dict[str, Any]) -> None:
        async with self._db.sessions.begin() as session:
            session.add(DiagnosisFeedbackRow(**item))

    async def feedback_for(self, diagnosis_ids: list[str]) -> list[dict[str, Any]]:
        if not diagnosis_ids:
            return []
        q = (
            select(DiagnosisFeedbackRow)
            .where(DiagnosisFeedbackRow.diagnosis_id.in_(diagnosis_ids))
            .order_by(DiagnosisFeedbackRow.created_at)
        )
        cols = [c.key for c in DiagnosisFeedbackRow.__table__.columns]
        async with self._db.sessions() as session:
            return [{c: getattr(r, c) for c in cols} for r in (await session.scalars(q)).all()]

    async def feedback_stats(self) -> dict[str, dict[str, int]]:
        q = select(DiagnosisFeedbackRow.diagnosis_type, DiagnosisFeedbackRow.verdict, func.count()).group_by(
            DiagnosisFeedbackRow.diagnosis_type, DiagnosisFeedbackRow.verdict
        )
        out: dict[str, dict[str, int]] = {}
        async with self._db.sessions() as session:
            for t, v, n in (await session.execute(q)).all():
                out.setdefault(t, {})[v] = int(n)
        return out

    async def purge_older_than(self, cutoff: datetime) -> int:
        async with self._db.sessions.begin() as session:
            res = await session.execute(delete(DiagnosisRow).where(DiagnosisRow.created_at < cutoff))
            return int(getattr(res, "rowcount", 0) or 0)
