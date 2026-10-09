"""PostgreSQL / TimescaleDB repositories (SQLAlchemy 2.x async)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, bindparam, delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.metrics import DB_ERRORS, DB_LATENCY
from app.domain.anomalies.models import (
    LEGACY_LEVEL,
    Anomaly,
    AnomalyType,
    Detector,
    Level,
    Lifecycle,
    Severity,
)
from app.domain.components.models import Component
from app.domain.devices.models import Device
from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import (
    AnomalyRow,
    DeviceRow,
    HardwareComponentRow,
    HealthEventRow,
    SystemEventRow,
    TelemetryMetricRow,
    TelemetrySampleRow,
)
from app.repositories.base import (
    AnomalyFilter,
    HealthEventRecord,
    HistoryPoint,
    MetricDef,
    SampleRow,
    SystemEventRecord,
)


class _Timed:
    def __init__(self, operation: str) -> None:
        self.operation = operation

    def __enter__(self) -> None:
        self._t = time.perf_counter()

    def __exit__(self, exc_type: object, *_: object) -> None:
        DB_LATENCY.labels(self.operation).observe(time.perf_counter() - self._t)
        if exc_type is not None:
            DB_ERRORS.labels(self.operation).inc()


class SqlDeviceRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, device: Device) -> None:
        values = {
            "id": device.device_id,
            "manufacturer": device.manufacturer,
            "model": device.model,
            "model_number": device.model_number,
            "os_name": device.os_name,
            "agent_version": device.agent_version,
            "inventory": device.inventory,
            "first_seen": device.first_seen,
            "last_seen": device.last_seen,
            "last_inventory_at": device.last_inventory_at,
        }
        stmt = pg_insert(DeviceRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[DeviceRow.id],
            set_={k: stmt.excluded[k] for k in values if k not in ("id", "first_seen")},
        )
        with _Timed("device_upsert"):
            async with self._db.sessions.begin() as session:
                await session.execute(stmt)

    async def touch(self, device_id: str, last_seen: datetime) -> None:
        with _Timed("device_touch"):
            async with self._db.sessions.begin() as session:
                row = await session.get(DeviceRow, device_id)
                if row is not None:
                    row.last_seen = last_seen

    async def get(self, device_id: str) -> Device | None:
        async with self._db.sessions() as session:
            row = await session.get(DeviceRow, device_id)
        return _device(row) if row else None

    async def list_all(self) -> list[Device]:
        async with self._db.sessions() as session:
            rows = (
                (await session.execute(select(DeviceRow).order_by(DeviceRow.last_seen.desc())))
                .scalars()
                .all()
            )
        return [_device(r) for r in rows]

    async def upsert_components(self, device_id: str, components: list[Component]) -> None:
        now = datetime.now(UTC)
        rows = [
            {
                "device_id": device_id,
                "component_id": c.component_id,
                "component_type": c.component_type.value,
                "name": c.name[:256],
                "parent_component_id": c.parent_id,
                "manufacturer": c.manufacturer,
                "model": c.model,
                "properties": _jsonable(c.properties),
                "updated_at": now,
            }
            for c in components
        ]
        if not rows:
            return
        stmt = pg_insert(HardwareComponentRow).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_component_device",
            set_={
                k: stmt.excluded[k]
                for k in (
                    "component_type",
                    "name",
                    "parent_component_id",
                    "manufacturer",
                    "model",
                    "properties",
                    "updated_at",
                )
            },
        )
        with _Timed("components_upsert"):
            async with self._db.sessions.begin() as session:
                await session.execute(stmt)


def _device(row: DeviceRow) -> Device:
    return Device(
        device_id=row.id,
        manufacturer=row.manufacturer,
        model=row.model,
        model_number=row.model_number,
        os_name=row.os_name,
        inventory=row.inventory,
        agent_version=row.agent_version,
        first_seen=row.first_seen,
        last_inventory_at=row.last_inventory_at,
        last_seen=row.last_seen,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class SqlTelemetryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._ids: dict[tuple[str, str], int] = {}

    async def _ensure_metrics(self, device_id: str, metrics: dict[str, MetricDef]) -> None:
        missing = [m for k, m in metrics.items() if (device_id, k) not in self._ids]
        if not missing:
            return
        rows = [
            {
                "device_id": device_id,
                "metric_key": m.key,
                "metric": m.metric,
                "component_id": m.component_id,
                "unit": m.unit,
                "source": m.source[:200],
                "kind": m.kind,
                "labels": m.labels,
            }
            for m in missing
        ]
        insert = pg_insert(TelemetryMetricRow).values(rows)
        upsert = insert.on_conflict_do_update(
            constraint="uq_metric_device_key",
            set_={
                "unit": insert.excluded.unit,
                "source": insert.excluded.source,
                "component_id": insert.excluded.component_id,
            },
        ).returning(TelemetryMetricRow.id, TelemetryMetricRow.metric_key)
        async with self._db.sessions.begin() as session:
            for metric_id, key in (await session.execute(upsert)).all():
                self._ids[(device_id, key)] = metric_id

    async def write_samples(
        self, device_id: str, metrics: dict[str, MetricDef], samples: list[SampleRow]
    ) -> int:
        if not samples:
            return 0
        with _Timed("samples_insert"):
            await self._ensure_metrics(device_id, metrics)
            records = [
                (s.time, self._ids[(device_id, s.key)], s.value, s.quality)
                for s in samples
                if (device_id, s.key) in self._ids
            ]
            await self._copy_samples(records)
        return len(records)

    async def write_samples_bulk(
        self, batches: list[tuple[str, dict[str, MetricDef], list[SampleRow]]]
    ) -> int:
        """All devices of one persister flush in a single COPY (one round trip, one transaction)."""
        records: list[tuple[datetime, int, float, int]] = []
        with _Timed("samples_insert"):
            for device_id, metrics, samples in batches:
                await self._ensure_metrics(device_id, metrics)
                records.extend(
                    (s.time, self._ids[(device_id, s.key)], s.value, s.quality)
                    for s in samples
                    if (device_id, s.key) in self._ids
                )
            await self._copy_samples(records)
        return len(records)

    async def _copy_samples(self, records: list[tuple[datetime, int, float, int]]) -> None:
        """Binary COPY into a per-connection staging table, then one set-based insert.

        COPY is encoded by asyncpg in C, so thousands of rows cost a few ms of event-loop time
        (a SQLAlchemy multi-VALUES insert cost ~0.9 s per flush at 100 devices). ``ON CONFLICT DO
        NOTHING`` keeps replays idempotent, which plain COPY into the hypertable could not.
        """
        if not records:
            return
        async with self._db.engine.connect() as conn:
            raw = await conn.get_raw_connection()
            apg: Any = raw.driver_connection
            async with apg.transaction():
                await apg.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS _samples_stage "
                    "(time timestamptz, metric_id integer, value double precision, quality smallint) "
                    "ON COMMIT DELETE ROWS"
                )
                await apg.copy_records_to_table(
                    "_samples_stage", records=records, columns=["time", "metric_id", "value", "quality"]
                )
                await apg.execute(
                    "INSERT INTO telemetry_samples (time, metric_id, value, quality) "
                    "SELECT time, metric_id, value, quality FROM _samples_stage ON CONFLICT DO NOTHING"
                )

    async def history(
        self, device_id: str, keys: list[str], start: datetime, end: datetime, bucket_s: int
    ) -> dict[str, list[HistoryPoint]]:
        if self._db.aggregate_5m and bucket_s >= 300 and bucket_s % 300 == 0:
            return await self._history_from_aggregate(device_id, keys, start, end, bucket_s)
        sql = text(
            """
            SELECT m.metric_key AS key,
                   date_bin(make_interval(secs => :bucket), s.time, TIMESTAMPTZ '2000-01-01') AS bucket,
                   avg(s.value) AS avg, min(s.value) AS min, max(s.value) AS max, count(*) AS n
            FROM telemetry_samples s
            JOIN telemetry_metrics m ON m.id = s.metric_id
            WHERE m.device_id = :device AND m.metric_key IN :keys AND s.time >= :start AND s.time < :end
            GROUP BY 1, 2
            ORDER BY 2
            """
        ).bindparams(bindparam("keys", expanding=True))
        out: dict[str, list[HistoryPoint]] = {k: [] for k in keys}
        with _Timed("history_query"):
            async with self._db.sessions() as session:
                result = await session.execute(
                    sql, {"bucket": bucket_s, "device": device_id, "keys": keys, "start": start, "end": end}
                )
                for row in result.mappings():
                    out[row["key"]].append(
                        HistoryPoint(
                            row["bucket"],
                            float(row["avg"]),
                            float(row["min"]),
                            float(row["max"]),
                            int(row["n"]),
                        )
                    )
        return out

    async def _history_from_aggregate(
        self, device_id: str, keys: list[str], start: datetime, end: datetime, bucket_s: int
    ) -> dict[str, list[HistoryPoint]]:
        """Long ranges read the 5-minute continuous aggregate (kept longer than raw samples)."""
        sql = text(
            """
            SELECT m.metric_key AS key,
                   date_bin(make_interval(secs => :bucket), a.bucket, TIMESTAMPTZ '2000-01-01') AS b,
                   sum(a.avg_value * a.samples) / nullif(sum(a.samples), 0) AS avg,
                   min(a.min_value) AS min, max(a.max_value) AS max, sum(a.samples) AS n
            FROM telemetry_samples_5m a
            JOIN telemetry_metrics m ON m.id = a.metric_id
            WHERE m.device_id = :device AND m.metric_key IN :keys AND a.bucket >= :start AND a.bucket < :end
            GROUP BY 1, 2
            ORDER BY 2
            """
        ).bindparams(bindparam("keys", expanding=True))
        out: dict[str, list[HistoryPoint]] = {k: [] for k in keys}
        with _Timed("history_aggregate_query"):
            async with self._db.sessions() as session:
                result = await session.execute(
                    sql, {"bucket": bucket_s, "device": device_id, "keys": keys, "start": start, "end": end}
                )
                for row in result.mappings():
                    if row["avg"] is None:
                        continue
                    out[row["key"]].append(
                        HistoryPoint(
                            row["b"], float(row["avg"]), float(row["min"]), float(row["max"]), int(row["n"])
                        )
                    )
        return out

    async def raw_values(
        self, device_id: str, key: str, start: datetime, end: datetime, limit: int = 20_000
    ) -> list[tuple[datetime, float]]:
        stmt = (
            select(TelemetrySampleRow.time, TelemetrySampleRow.value)
            .join(TelemetryMetricRow, TelemetryMetricRow.id == TelemetrySampleRow.metric_id)
            .where(
                TelemetryMetricRow.device_id == device_id,
                TelemetryMetricRow.metric_key == key,
                TelemetrySampleRow.time >= start,
                TelemetrySampleRow.time < end,
            )
            .order_by(TelemetrySampleRow.time.desc())
            .limit(limit)
        )
        with _Timed("raw_query"):
            async with self._db.sessions() as session:
                rows = (await session.execute(stmt)).all()
        return [(t, v) for t, v in reversed(rows)]

    async def list_metrics(self, device_id: str) -> list[MetricDef]:
        async with self._db.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(TelemetryMetricRow)
                        .where(TelemetryMetricRow.device_id == device_id)
                        .order_by(TelemetryMetricRow.metric_key)
                    )
                )
                .scalars()
                .all()
            )
        return [
            MetricDef(r.metric_key, r.metric, r.component_id, r.unit, r.source, r.kind, r.labels)
            for r in rows
        ]

    async def purge_older_than(self, cutoff: datetime) -> int:
        with _Timed("retention_purge"):
            async with self._db.sessions.begin() as session:
                result = await session.execute(
                    delete(TelemetrySampleRow).where(TelemetrySampleRow.time < cutoff)
                )
        return int(getattr(result, "rowcount", 0) or 0)


class SqlEventRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add_health_event(self, record: HealthEventRecord) -> None:
        with _Timed("health_event_insert"):
            async with self._db.sessions.begin() as session:
                session.add(
                    HealthEventRow(
                        device_id=record.device_id,
                        component_id=record.component_id,
                        time=record.time,
                        previous_score=record.previous_score,
                        score=record.score,
                        previous_status=record.previous_status,
                        status=record.status,
                        reasons=record.reasons,
                    )
                )

    async def list_health_events(self, device_id: str, limit: int) -> list[HealthEventRecord]:
        async with self._db.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(HealthEventRow)
                        .where(HealthEventRow.device_id == device_id)
                        .order_by(HealthEventRow.time.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [
            HealthEventRecord(
                r.device_id,
                r.component_id,
                r.time,
                r.previous_score,
                r.score,
                r.previous_status,
                r.status,
                r.reasons,
            )
            for r in rows
        ]

    async def upsert_anomaly(self, anomaly: Anomaly) -> None:
        values = {
            "id": anomaly.anomaly_id,
            "device_id": anomaly.device_id,
            "detector": anomaly.detector.value,
            "rule_id": anomaly.rule_id,
            "component_id": anomaly.component_id,
            "metric_key": anomaly.metric_key,
            "severity": anomaly.severity.value,
            "title": anomaly.title,
            "message": anomaly.message,
            "value": None if anomaly.value is None else str(anomaly.value)[:64],
            "threshold": None if anomaly.threshold is None else str(anomaly.threshold)[:64],
            "started_at": anomaly.started_at,
            "last_seen_at": anomaly.last_seen_at,
            "resolved_at": anomaly.resolved_at,
            "context": _jsonable(anomaly.context),
            "anomaly_type": anomaly.anomaly_type.value,
            "category": anomaly.category,
            "level": anomaly.effective_level.value,
            "confidence": anomaly.confidence,
            "lifecycle": anomaly.lifecycle.value,
            "signal_id": anomaly.signal_id,
            "model_version": anomaly.model_version,
            "baseline_version": anomaly.baseline_version,
            "expected_value": anomaly.expected_value,
            "expected_min": anomaly.expected_min,
            "expected_max": anomaly.expected_max,
            "deviation_score": anomaly.deviation_score,
            "evidence": _jsonable(anomaly.evidence),
            "related": _jsonable(anomaly.related),
            "correlation_key": anomaly.correlation_key,
            "occurrences": anomaly.occurrences,
            "updated_at": anomaly.updated_at,
        }
        stmt = pg_insert(AnomalyRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[AnomalyRow.id],
            set_={k: stmt.excluded[k] for k in _ANOMALY_MUTABLE},
        )
        with _Timed("anomaly_upsert"):
            async with self._db.sessions.begin() as session:
                await session.execute(stmt)

    async def list_anomalies(
        self, device_id: str, status: str | None, severity: str | None, since: datetime | None, limit: int
    ) -> list[Anomaly]:
        stmt = select(AnomalyRow).where(AnomalyRow.device_id == device_id)
        if status == "active":
            stmt = stmt.where(AnomalyRow.resolved_at.is_(None))
        elif status == "resolved":
            stmt = stmt.where(AnomalyRow.resolved_at.is_not(None))
        if severity:
            stmt = stmt.where(AnomalyRow.severity == severity)
        if since:
            stmt = stmt.where(AnomalyRow.started_at >= since)
        stmt = stmt.order_by(AnomalyRow.started_at.desc()).limit(limit)
        async with self._db.sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_anomaly(r) for r in rows]

    async def fleet_anomalies(self, device_ids: list[str], since: datetime, limit: int) -> list[Anomaly]:
        if not device_ids:
            return []
        stmt = (
            select(AnomalyRow)
            .where(AnomalyRow.device_id.in_(device_ids), AnomalyRow.started_at >= since)
            .order_by(AnomalyRow.started_at.desc())
            .limit(limit)
        )
        async with self._db.sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_anomaly(r) for r in rows]

    async def search_anomalies(
        self, device_id: str, filters: AnomalyFilter, limit: int, offset: int = 0
    ) -> list[Anomaly]:
        f = filters
        stmt = select(AnomalyRow).where(AnomalyRow.device_id == device_id)
        if f.status == "active":
            stmt = stmt.where(AnomalyRow.resolved_at.is_(None))
        elif f.status == "resolved":
            stmt = stmt.where(AnomalyRow.resolved_at.is_not(None))
        if f.severity:
            stmt = stmt.where(AnomalyRow.severity == f.severity)
        if f.levels:
            legacy = [s.value for s, lv in LEGACY_LEVEL.items() if lv.value in f.levels]
            stmt = stmt.where(
                or_(
                    AnomalyRow.level.in_(f.levels),
                    and_(AnomalyRow.level.is_(None), AnomalyRow.severity.in_(legacy or ["-"])),
                )
            )
        if f.types:
            cond: Any = AnomalyRow.anomaly_type.in_(f.types)
            if AnomalyType.THRESHOLD.value in f.types:
                cond = or_(cond, AnomalyRow.anomaly_type.is_(None))  # legacy rows are rule/threshold
            stmt = stmt.where(cond)
        if f.since:
            stmt = stmt.where(AnomalyRow.started_at >= f.since)
        if f.until:
            stmt = stmt.where(AnomalyRow.started_at <= f.until)
        if f.min_confidence is not None:
            stmt = stmt.where(AnomalyRow.confidence >= f.min_confidence)
        if f.signal_id:
            stmt = stmt.where(AnomalyRow.signal_id == f.signal_id)
        stmt = stmt.order_by(AnomalyRow.started_at.desc()).offset(offset).limit(limit)
        with _Timed("anomaly_search"):
            async with self._db.sessions() as session:
                rows = (await session.execute(stmt)).scalars().all()
        return [_anomaly(r) for r in rows]

    async def get_anomaly(self, anomaly_id: str) -> Anomaly | None:
        async with self._db.sessions() as session:
            row = await session.get(AnomalyRow, anomaly_id)
        return _anomaly(row) if row is not None else None

    async def set_anomaly_feedback(self, anomaly_id: str, feedback: dict[str, Any]) -> bool:
        async with self._db.sessions.begin() as session:
            result = await session.execute(
                update(AnomalyRow).where(AnomalyRow.id == anomaly_id).values(feedback=_jsonable(feedback))
            )
        return bool(getattr(result, "rowcount", 0))

    async def add_system_event(self, record: SystemEventRecord) -> None:
        stmt = (
            pg_insert(SystemEventRow)
            .values(
                device_id=record.device_id,
                time=record.time,
                event_type=record.event_type,
                severity=record.severity,
                message=record.message,
                data=_jsonable(record.data),
                event_uid=record.event_uid,
                priority=record.priority,
                category=record.category,
            )
            .on_conflict_do_nothing()  # the same agent event replayed after a restart is stored once
        )
        with _Timed("system_event_insert"):
            async with self._db.sessions.begin() as session:
                await session.execute(stmt)

    async def purge_system_events_older_than(self, cutoff: datetime) -> int:
        with _Timed("event_retention_purge"):
            async with self._db.sessions.begin() as session:
                result = await session.execute(delete(SystemEventRow).where(SystemEventRow.time < cutoff))
        return int(getattr(result, "rowcount", 0) or 0)

    async def list_system_events(self, device_id: str, limit: int) -> list[SystemEventRecord]:
        async with self._db.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(SystemEventRow)
                        .where(SystemEventRow.device_id == device_id)
                        .order_by(SystemEventRow.time.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [
            SystemEventRecord(
                r.device_id,
                r.time,
                r.event_type,
                r.severity,
                r.message,
                r.data,
                r.event_uid,
                r.priority,
                r.category,
            )
            for r in rows
        ]


def _num(value: str | None) -> float | str | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return value


_ANOMALY_MUTABLE = (
    "last_seen_at",
    "resolved_at",
    "value",
    "message",
    "context",
    "severity",
    "level",
    "confidence",
    "lifecycle",
    "expected_value",
    "expected_min",
    "expected_max",
    "deviation_score",
    "evidence",
    "related",
    "correlation_key",
    "occurrences",
    "updated_at",
    "model_version",
    "baseline_version",
)


def _anomaly(r: AnomalyRow) -> Anomaly:
    a = Anomaly(
        r.id,
        r.device_id,
        Detector(r.detector),
        r.rule_id,
        r.component_id,
        r.metric_key,
        Severity(r.severity),
        r.title,
        r.message,
        _num(r.value),
        _num(r.threshold),
        r.started_at,
        r.last_seen_at,
        r.resolved_at,
        r.context or {},
    )
    if r.anomaly_type:
        a.anomaly_type = AnomalyType(r.anomaly_type)
    a.category = r.category
    a.level = Level(r.level) if r.level else None
    a.confidence = r.confidence
    if r.lifecycle:
        a.lifecycle = Lifecycle(r.lifecycle)
    elif r.resolved_at is not None:
        a.lifecycle = Lifecycle.RESOLVED
    a.signal_id = r.signal_id
    a.model_version = r.model_version
    a.baseline_version = r.baseline_version
    a.expected_value, a.expected_min, a.expected_max = r.expected_value, r.expected_min, r.expected_max
    a.deviation_score = r.deviation_score
    a.evidence = r.evidence or {}
    a.related = r.related or []
    a.correlation_key = r.correlation_key
    a.occurrences = r.occurrences or 1
    a.updated_at = r.updated_at
    a.feedback = r.feedback
    return a
