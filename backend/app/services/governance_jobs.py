"""DataGovernanceService (Phase 9): controlled device-data deletion and per-organisation retention.

Deletion (explicit workflow: permission + recent sign-in + typed confirmation + retired device):
    deletes the device's raw samples (and refreshes the 5-minute aggregate over that range so derived
    points disappear too), metrics catalogue, health events, system events, anomalies, baselines, models,
    predictions, diagnoses (+ feedback), notifications and ingest receipts; then marks the device
    DECOMMISSIONED. Kept on purpose: the device row (identity), alerts, remediations and every audit trail
    (governance records), and the registry entry.

Retention (hourly): per organisation, raw telemetry, diagnoses, closed alerts and delivered notifications
older than the organisation's retention policy are deleted. Platform settings are the ceiling
(RETENTION_DAYS, NOTIFICATION_RETENTION_DAYS purge everything older platform-wide): an organisation can
shorten retention, never extend it. Open alerts and undelivered notifications are never removed. Audit
events are never deleted by retention. Everything is recorded in the audit trail with row counts.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text

from app.domain.tenancy.models import Lifecycle
from app.repositories.alerting import DUE, OPEN

#: maintenance transactions lift DB_STATEMENT_TIMEOUT_MS for themselves only (SET LOCAL)
NO_STATEMENT_TIMEOUT = "SET LOCAL statement_timeout = 0"
MAX_JOBS = 200  # deletion jobs kept in memory for status queries (the audit trail keeps all)

log = structlog.get_logger("governance")

DEVICE_TABLES = (  # (table, device column) deleted in this order; samples go first via the metric ids
    ("health_events", "device_id"),
    ("system_events", "device_id"),
    ("anomaly_acknowledgements", "device_id"),
    ("anomalies", "device_id"),
    ("device_baselines", "device_id"),
    ("anomaly_models", "device_id"),
    ("predictions", "device_id"),
    ("diagnoses", "device_id"),
    ("notifications", "device_id"),
    ("ingest_receipts", "device_id"),
    ("hardware_components", "device_id"),
    ("telemetry_metrics", "device_id"),
)


class DataGovernanceService:
    def __init__(
        self, db: Any, tenancy: Any, policies: Any, audit: Any, settings: Any, telemetry_repo: Any
    ) -> None:
        self._db = db
        self.tenancy = tenancy
        self.policies = policies
        self.audit = audit
        self._s = settings
        self._telemetry = telemetry_repo
        self.jobs: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    async def delete_device_data(self, ctx: Any, device_id: str, reason: str) -> dict[str, Any]:
        job = {
            "job_id": uuid.uuid4().hex,
            "device_id": device_id,
            "status": "QUEUED",
            "requested_by": ctx.username,
            "requested_at": datetime.now(UTC).isoformat(),
            "deleted": {},
        }
        self.jobs[job["job_id"]] = job
        finished = [k for k, j in self.jobs.items() if j["status"] in ("DONE", "FAILED")]
        for k in finished[: max(0, len(self.jobs) - MAX_JOBS)]:  # dicts keep insertion order: oldest first
            del self.jobs[k]
        self.audit.record(
            ctx.org_id,
            ctx.username,
            ctx.actor_type,
            "data.deletion_requested",
            "data",
            resource_type="device",
            resource_id=device_id,
            severity="WARNING",
            metadata={"reason": reason},
        )
        task = asyncio.create_task(self._run_deletion(ctx, job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run_deletion(self, ctx: Any, job: dict[str, Any]) -> None:
        device_id = job["device_id"]
        job["status"] = "RUNNING"
        try:
            job["deleted"] = await self._delete(device_id)
            await self.tenancy.transition(ctx, device_id, Lifecycle.DECOMMISSIONED, "device data deleted")
            job["status"] = "DONE"
            self.audit.record(
                ctx.org_id,
                ctx.username,
                ctx.actor_type,
                "data.deleted",
                "data",
                resource_type="device",
                resource_id=device_id,
                severity="WARNING",
                metadata={"rows": job["deleted"]},
            )
        except Exception as exc:
            job["status"], job["error"] = "FAILED", type(exc).__name__
            log.warning("device_data_deletion_failed", device_id=device_id, error=str(exc)[:200])
            self.audit.record(
                ctx.org_id,
                ctx.username,
                ctx.actor_type,
                "data.deletion_failed",
                "data",
                resource_type="device",
                resource_id=device_id,
                result="FAILURE",
                severity="HIGH",
                reason=type(exc).__name__,
            )
        job["finished_at"] = datetime.now(UTC).isoformat()

    async def _delete(self, device_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        if self._db is None:  # memory deployments (tests): drop the in-memory samples
            store = getattr(self._telemetry, "samples", {})
            keys = [k for k in store if k[0] == device_id]
            counts["telemetry_samples"] = sum(len(store.pop(k)) for k in keys)
            return counts
        async with self._db.sessions.begin() as s:
            await s.execute(text(NO_STATEMENT_TIMEOUT))  # deliberate bulk delete: may exceed the default
            span = (
                await s.execute(
                    text(
                        "SELECT min(time), max(time) FROM telemetry_samples WHERE metric_id IN "
                        "(SELECT id FROM telemetry_metrics WHERE device_id = :d)"
                    ),
                    {"d": device_id},
                )
            ).first()
            res = await s.execute(
                text(
                    "DELETE FROM telemetry_samples WHERE metric_id IN "
                    "(SELECT id FROM telemetry_metrics WHERE device_id = :d)"
                ),
                {"d": device_id},
            )
            counts["telemetry_samples"] = int(getattr(res, "rowcount", 0) or 0)
            for table, col in DEVICE_TABLES:
                res = await s.execute(text(f"DELETE FROM {table} WHERE {col} = :d"), {"d": device_id})  # noqa: S608 - fixed names
                counts[table] = int(getattr(res, "rowcount", 0) or 0)
        if span and span[0] is not None and getattr(self._db, "aggregate_5m", False):
            with contextlib.suppress(Exception):  # derived 5-minute points of the deleted range
                async with self._db.engine.connect() as conn:
                    await conn.execution_options(isolation_level="AUTOCOMMIT")
                    await conn.execute(
                        text("CALL refresh_continuous_aggregate('telemetry_samples_5m', :a, :b)"),
                        {"a": span[0], "b": span[1] + timedelta(minutes=5)},
                    )
        return counts

    # ------------------------------------------------------------------ retention
    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 3600)
            if stop.is_set():
                break
            try:
                await self.apply_retention(datetime.now(UTC))
            except Exception as exc:
                log.warning("tenant_retention_failed", error=str(exc)[:200])

    async def apply_retention(self, now: datetime) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        if self._db is None:
            return out
        platform_raw = int(getattr(self._s, "retention_days", 30))
        for org_id in list(self.tenancy.orgs):
            pol = self.policies.effective("retention", org_id)[0]
            devices = sorted(self.tenancy.org_devices(org_id))
            if not devices:
                continue
            counts: dict[str, int] = {}
            async with self._db.sessions.begin() as s:
                await s.execute(text(NO_STATEMENT_TIMEOUT))
                if pol["raw_telemetry_days"] < platform_raw:
                    res = await s.execute(
                        text(
                            "DELETE FROM telemetry_samples WHERE time < :c AND metric_id IN "
                            "(SELECT id FROM telemetry_metrics WHERE device_id = ANY(:d))"
                        ),
                        {"c": now - timedelta(days=pol["raw_telemetry_days"]), "d": devices},
                    )
                    counts["telemetry_samples"] = int(getattr(res, "rowcount", 0) or 0)
                res = await s.execute(
                    text("DELETE FROM diagnoses WHERE created_at < :c AND device_id = ANY(:d)"),
                    {"c": now - timedelta(days=pol["diagnoses_days"]), "d": devices},
                )
                counts["diagnoses"] = int(getattr(res, "rowcount", 0) or 0)
                platform_alerts = int(getattr(self._s, "notification_retention_days", 90))
                if pol["notifications_days"] < platform_alerts:
                    res = await s.execute(
                        text(
                            "DELETE FROM notifications WHERE created_at < :c AND device_id = ANY(:d) "
                            "AND status <> ALL(:due)"
                        ),
                        {
                            "c": now - timedelta(days=pol["notifications_days"]),
                            "d": devices,
                            "due": list(DUE),
                        },
                    )
                    counts["notifications"] = int(getattr(res, "rowcount", 0) or 0)
                if pol["alerts_days"] < platform_alerts:
                    res = await s.execute(
                        text(
                            "DELETE FROM alerts WHERE created_at < :c AND device_id = ANY(:d) "
                            "AND status <> ALL(:open)"
                        ),
                        {"c": now - timedelta(days=pol["alerts_days"]), "d": devices, "open": list(OPEN)},
                    )
                    counts["alerts"] = int(getattr(res, "rowcount", 0) or 0)
            if any(counts.values()):
                self.audit.record(
                    org_id,
                    "system",
                    "system",
                    "data.retention_applied",
                    "data",
                    source="worker",
                    metadata={
                        "rows": counts,
                        "policy": {
                            k: pol[k]
                            for k in (
                                "raw_telemetry_days",
                                "diagnoses_days",
                                "notifications_days",
                                "alerts_days",
                            )
                        },
                    },
                )
            out[org_id] = counts
        return out
