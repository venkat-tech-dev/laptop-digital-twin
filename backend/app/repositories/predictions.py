"""Storage of predictions (Phase 5): upsert by id, queries for active / history / calibration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.domain.prediction.engine import Prediction
from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import PredictionRow

ACTIVE_STATUSES = ("ACTIVE", "UPDATED", "LOW_CONFIDENCE")
CLOSED_STATUSES = ("CONFIRMED", "INVALIDATED", "EXPIRED", "CANCELLED")


@dataclass(frozen=True, slots=True)
class PredictionFilter:
    active: bool | None = None  # True: open lifecycle states; False: closed
    statuses: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    since: datetime | None = None
    until: datetime | None = None


class PredictionRepository(Protocol):
    async def upsert(self, p: Prediction) -> None: ...

    async def get(self, prediction_id: str) -> Prediction | None: ...

    async def search(
        self, device_id: str | None, f: PredictionFilter, limit: int, offset: int = 0
    ) -> list[Prediction]: ...


def _matches(p: Prediction, device_id: str | None, f: PredictionFilter) -> bool:
    return (
        (device_id is None or p.device_id == device_id)
        and (f.active is None or (p.status in ACTIVE_STATUSES) == f.active)
        and (not f.statuses or p.status in f.statuses)
        and (not f.targets or p.target_id in f.targets)
        and (f.since is None or p.created_at >= f.since)
        and (f.until is None or p.created_at <= f.until)
    )


class MemoryPredictionRepository:
    def __init__(self, max_items: int = 5000) -> None:
        self.items: dict[str, Prediction] = {}
        self._max = max_items

    async def upsert(self, p: Prediction) -> None:
        self.items[p.prediction_id] = p
        if len(self.items) > self._max:
            oldest = min(self.items.values(), key=lambda x: x.updated_at)
            self.items.pop(oldest.prediction_id, None)

    async def get(self, prediction_id: str) -> Prediction | None:
        return self.items.get(prediction_id)

    async def search(
        self, device_id: str | None, f: PredictionFilter, limit: int, offset: int = 0
    ) -> list[Prediction]:
        rows = [p for p in self.items.values() if _matches(p, device_id, f)]
        rows.sort(key=lambda p: p.created_at, reverse=True)
        return rows[offset : offset + limit]


_COLUMNS = (
    "device_id",
    "correlation_key",
    "target_id",
    "prediction_type",
    "unit",
    "direction",
    "status",
    "severity",
    "current_value",
    "threshold",
    "forecast_value",
    "forecast_at",
    "time_to_threshold_s",
    "crossing_at",
    "crossing_earliest",
    "crossing_latest",
    "lower_bound",
    "upper_bound",
    "confidence",
    "confidence_band",
    "model_type",
    "model_version",
    "feature_version",
    "baseline_version",
    "history_start",
    "history_end",
    "statement",
    "evidence",
    "reason",
    "revisions",
    "first_crossing_at",
    "actual_crossing_at",
    "timing_error_s",
    "first_timing_error_s",
    "lead_time_s",
    "created_at",
    "updated_at",
    "expires_at",
    "closed_at",
)


def _values(p: Prediction) -> dict[str, Any]:
    v = {c: getattr(p, c) for c in _COLUMNS}
    v["id"] = p.prediction_id
    v["metric"] = p.metric_field
    return v


def _from_row(r: PredictionRow) -> Prediction:
    kw = {c: getattr(r, c) for c in _COLUMNS}
    kw["evidence"] = r.evidence or {}
    return Prediction(prediction_id=r.id, metric_field=r.metric, **kw)


class SqlPredictionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, p: Prediction) -> None:
        values = _values(p)
        stmt = pg_insert(PredictionRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[PredictionRow.id], set_={k: stmt.excluded[k] for k in values if k != "id"}
        )
        async with self._db.sessions.begin() as session:
            await session.execute(stmt)

    async def get(self, prediction_id: str) -> Prediction | None:
        async with self._db.sessions() as session:
            row = await session.get(PredictionRow, prediction_id)
        return _from_row(row) if row is not None else None

    async def search(
        self, device_id: str | None, f: PredictionFilter, limit: int, offset: int = 0
    ) -> list[Prediction]:
        stmt = select(PredictionRow)
        if device_id is not None:
            stmt = stmt.where(PredictionRow.device_id == device_id)
        if f.active is not None:
            cond = PredictionRow.status.in_(ACTIVE_STATUSES)
            stmt = stmt.where(cond if f.active else ~cond)
        if f.statuses:
            stmt = stmt.where(PredictionRow.status.in_(f.statuses))
        if f.targets:
            stmt = stmt.where(PredictionRow.target_id.in_(f.targets))
        if f.since:
            stmt = stmt.where(PredictionRow.created_at >= f.since)
        if f.until:
            stmt = stmt.where(PredictionRow.created_at <= f.until)
        stmt = stmt.order_by(PredictionRow.created_at.desc()).offset(offset).limit(limit)
        async with self._db.sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_from_row(r) for r in rows]


def calibration(items: list[Prediction]) -> dict[str, Any]:
    """How accurate have closed predictions been? (per target and per confidence band)"""

    def summarize(group: list[Prediction]) -> dict[str, Any]:
        closed = [p for p in group if p.status in CLOSED_STATUSES and p.status != "CANCELLED"]
        confirmed = [p for p in closed if p.status == "CONFIRMED"]
        errs = sorted(abs(p.timing_error_s) for p in confirmed if p.timing_error_s is not None)
        first = sorted(abs(p.first_timing_error_s) for p in confirmed if p.first_timing_error_s is not None)
        leads = [p.lead_time_s for p in confirmed if p.lead_time_s is not None]
        in_range = [
            p
            for p in confirmed
            if p.actual_crossing_at
            and p.crossing_earliest
            and p.crossing_earliest <= p.actual_crossing_at <= (p.crossing_latest or p.actual_crossing_at)
        ]
        return {
            "closed": len(closed),
            "confirmed": len(confirmed),
            "invalidated": sum(1 for p in closed if p.status == "INVALIDATED"),
            "expired": sum(1 for p in closed if p.status == "EXPIRED"),
            "hit_rate": round(len(confirmed) / len(closed), 3) if closed else None,
            "false_prediction_rate": round(sum(1 for p in closed if p.status == "EXPIRED") / len(closed), 3)
            if closed
            else None,
            "median_abs_timing_error_s": errs[len(errs) // 2] if errs else None,
            "median_abs_first_timing_error_s": first[len(first) // 2] if first else None,
            "mean_lead_time_s": round(sum(leads) / len(leads)) if leads else None,
            "actual_within_predicted_range": round(len(in_range) / len(confirmed), 3) if confirmed else None,
        }

    by_target: dict[str, list[Prediction]] = {}
    by_band: dict[str, list[Prediction]] = {}
    for p in items:
        by_target.setdefault(p.target_id, []).append(p)
        by_band.setdefault(p.confidence_band, []).append(p)
    return {
        "overall": summarize(items),
        "by_target": {k: summarize(v) for k, v in sorted(by_target.items())},
        "by_confidence_band": {k: summarize(v) for k, v in sorted(by_band.items())},
        "note": "An INVALIDATED prediction is not counted as wrong: conditions changed (e.g. cleanup, "
        "charger connected). EXPIRED = the likely crossing time passed without a crossing.",
    }
