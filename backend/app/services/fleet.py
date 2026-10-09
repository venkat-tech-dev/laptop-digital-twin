"""Fleet intelligence service (Phase 10): gathers facts for ONE organization's visible devices and runs
the pure domain functions in ``app.domain.fleet.intelligence``.

Tenant isolation: every entry point takes the caller's visible device set (Phase 9 ``visible_devices``)
and the organization id. Nothing is computed across organizations. Platform-only figures (storage,
notification backlog) are added only when the caller has platform scope.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text

from app.domain.fleet import intelligence as fi
from app.domain.governance.policies import version_tuple
from app.repositories.alerting import AlertFilter
from app.services.device_rows import device_row, load_credentials

log = structlog.get_logger("fleet")

SNAPSHOT_EVERY_S = 3600
HISTORY_DAYS = 30


class FleetService:
    def __init__(self, container: Any) -> None:
        self.c = container
        self._history: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=HISTORY_DAYS * 24))

    # ------------------------------------------------------------------ facts
    def _anomaly_counts(self, device_id: str) -> dict[str, int]:
        twin = self.c.twin.get(device_id)
        if twin is None:
            return {}
        counts: Counter[str] = Counter()
        for a in [*twin.anomalies.active.values(), *twin.behavior_active]:
            counts[a.severity.value] += 1
        return dict(counts)

    def _predictions_24h(self, device_id: str, now: datetime) -> list[Any]:
        f = getattr(self.c, "forecasts", None)
        if f is None:
            return []
        soon = now + timedelta(hours=24)
        return [
            p
            for p in f.tracker.active(device_id)
            if p.active() and p.crossing_at is not None and p.crossing_at <= soon
        ]

    def _open_alerts(self, devices: set[str]) -> dict[str, Counter[str]]:
        out: dict[str, Counter[str]] = defaultdict(Counter)
        if self.c.alerts is None:
            return out
        for a in self.c.alerts.engine.open_alerts():
            if a.device_id in devices:
                out[a.device_id][a.severity] += 1
        return out

    async def facts(
        self, org_id: str, devices: set[str], now: datetime
    ) -> tuple[list[fi.DeviceHealthFacts], list[dict[str, Any]]]:
        creds = await load_credentials(self.c)
        rows = [device_row(self.c, d, creds) for d in sorted(devices)]
        recommended = self.c.policies.effective("agent", org_id)[0].get("recommended_version", "0")
        alerts = self._open_alerts(devices)
        out = []
        for r in rows:
            d = r["device_id"]
            p = self.c.presence.get(d)
            last = p.last_batch_at if p else None
            health = r["health"] if r["health"] in ("HEALTHY", "WARNING", "CRITICAL") else None
            out.append(
                fi.DeviceHealthFacts(
                    device_id=d,
                    health=health,
                    telemetry_age_s=(now - last).total_seconds() if last else None,
                    anomalies=self._anomaly_counts(d),
                    predictions_24h=len(self._predictions_24h(d, now)),
                    alerts=dict(alerts.get(d, {})),
                    compliance=r["compliance"],
                    agent_outdated=bool(r["agent_version"])
                    and version_tuple(r["agent_version"]) < version_tuple(recommended),
                    group_names=r["group_names"],
                )
            )
        return out, rows

    # ------------------------------------------------------------------ health
    async def health(
        self, org_id: str, devices: set[str], weights: dict[str, float] | None = None
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        facts, _ = await self.facts(org_id, devices, now)
        h = fi.fleet_health(facts, weights)
        h["generated_at"] = now.isoformat()
        h["history"] = await self.history(org_id)
        return h

    async def history(self, org_id: str) -> list[dict[str, Any]]:
        db = self.c.db
        if db is not None:
            try:
                async with db.sessions() as s:
                    rows = (
                        await s.execute(
                            text(
                                "SELECT at, score, coverage, devices, version FROM fleet_health_snapshots "
                                "WHERE org_id = :o AND at >= :since ORDER BY at"
                            ),
                            {"o": org_id, "since": datetime.now(UTC) - timedelta(days=HISTORY_DAYS)},
                        )
                    ).all()
                return [
                    {
                        "at": r[0].isoformat(),
                        "score": r[1],
                        "coverage": r[2],
                        "devices": r[3],
                        "version": r[4],
                    }
                    for r in rows
                ]
            except Exception as exc:
                log.warning("fleet_history_unavailable", error=str(exc)[:200])
        return list(self._history[org_id])

    async def snapshot_all(self) -> int:
        """Hourly: one health snapshot per organization (its own devices only)."""
        n = 0
        now = datetime.now(UTC)
        for org_id in list(self.c.tenancy.orgs):
            devices = set(self.c.tenancy.org_devices(org_id))
            if not devices:
                continue
            facts, _ = await self.facts(org_id, devices, now)
            h = fi.fleet_health(facts)
            if h["score"] is None:
                continue
            snap = {
                "at": now.isoformat(),
                "score": h["score"],
                "coverage": h["coverage"],
                "devices": h["devices"],
                "version": h["version"],
            }
            self._history[org_id].append(snap)
            if self.c.db is not None:
                try:
                    async with self.c.db.sessions.begin() as s:
                        await s.execute(
                            text(
                                "INSERT INTO fleet_health_snapshots "
                                "(org_id, at, score, coverage, devices, version, data) "
                                "VALUES (:o, :at, :score, :cov, :n, :v, CAST(:d AS JSONB))"
                            ),
                            {
                                "o": org_id,
                                "at": now,
                                "score": h["score"],
                                "cov": h["coverage"],
                                "n": h["devices"],
                                "v": h["version"],
                                "d": json.dumps(
                                    {
                                        "contributors": h["contributors"],
                                        "critical": len(h["critical_conditions"]),
                                    }
                                ),
                            },
                        )
                        await s.execute(
                            text("DELETE FROM fleet_health_snapshots WHERE at < :cut"),
                            {"cut": now - timedelta(days=HISTORY_DAYS * 12)},  # one year
                        )
                except Exception as exc:
                    log.warning("fleet_snapshot_write_failed", error=str(exc)[:200])
            n += 1
        return n

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.snapshot_all()
            except Exception:
                log.exception("fleet_snapshot_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), SNAPSHOT_EVERY_S)

    # ------------------------------------------------------------------ insights
    async def insights(
        self, org_id: str, devices: set[str], window_minutes: int = 30, since_hours: int = 24
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        _, rows = await self.facts(org_id, devices, now)
        attrs: dict[str, dict[str, str]] = {}
        for r in rows:
            twin = self.c.twin.get(r["device_id"])
            if twin is None:
                continue  # no inventory: no attributes to compare
            attrs[r["device_id"]] = {
                "model": twin.device.model or "",
                "os": r["os"] or "",
                "agent_version": r["agent_version"] or "",
                "groups": ", ".join(r["group_names"]),
            }
        anomalies = await self.c.event_repo.fleet_anomalies(
            sorted(devices), now - timedelta(hours=since_hours), 5000
        )
        events = [
            fi.AnomalyFact(
                a.device_id,
                f"{a.anomaly_type.value}:{a.category or a.metric_key}",
                a.started_at,
                a.severity.value,
            )
            for a in anomalies
        ]
        corr = fi.correlate(events, attrs, window=timedelta(minutes=window_minutes), org_id=org_id)
        recurring: list[dict[str, Any]] = []
        if self.c.alerts is not None:
            alerts = await self.c.alerts.repo.search_alerts(
                AlertFilter(devices=frozenset(devices), since=now - timedelta(days=30)), 5000
            )
            recurring = fi.recurring_issues(
                [fi.AlertFact(a.device_id, a.alert_type, a.severity, a.created_at) for a in alerts], now
            )
        rem = self._remediation_outcomes(devices)
        return {
            "generated_at": now.isoformat(),
            "scope": {
                "organization": org_id,
                "devices": len(devices),
                "anomalies_considered": len(events),
                "since_hours": since_hours,
            },
            "correlation": corr,
            "recurring_issues": recurring,
            "remediation_outcomes": rem,
            "legend": {
                "OBSERVED_FACT": "counted directly from records",
                "STATISTICAL_ASSOCIATION": "over-representation with an exact test; not a cause",
                "POSSIBLE_EXPLANATION": "a hypothesis to investigate",
                "PREDICTION": "a projection with stated assumptions",
            },
        }

    def _remediation_outcomes(self, devices: set[str]) -> list[dict[str, Any]]:
        rem = getattr(self.c, "remediation", None)
        if rem is None:
            return []
        by: dict[str, Counter[str]] = defaultdict(Counter)
        for r in rem.items.values():
            if r.device_id in devices and r.status.value in (
                "SUCCEEDED",
                "PARTIALLY_SUCCEEDED",
                "FAILED",
                "ROLLED_BACK",
            ):
                by[r.action_type][r.status.value] += 1
        out = []
        for action, c in by.items():
            done = sum(c.values())
            out.append(
                {
                    "action_type": action,
                    "completed": done,
                    "by_status": dict(c),
                    "success_rate": round(c.get("SUCCEEDED", 0) / done, 3) if done >= 5 else None,
                    "note": None if done >= 5 else "fewer than 5 completed actions: no rate",
                }
            )
        return sorted(out, key=lambda x: -x["completed"])

    # ------------------------------------------------------------------ capacity
    async def capacity(self, org_id: str, devices: set[str], platform: bool) -> dict[str, Any]:
        now = datetime.now(UTC)
        t = self.c.tenancy
        enrolled = sorted(
            r.enrolled_at for d in devices if (r := t.registry.get(d)) is not None and r.enrolled_at
        )
        series: list[tuple[datetime, float]] = []
        if enrolled:
            day = datetime(enrolled[0].year, enrolled[0].month, enrolled[0].day, tzinfo=UTC)
            while day <= now:
                series.append((day, float(sum(1 for e in enrolled if e < day + timedelta(days=1)))))
                day += timedelta(days=1)
        limit, _mode = t.orgs[org_id].quota("max_devices")
        out: dict[str, Any] = {
            "generated_at": now.isoformat(),
            "devices": {
                "metric": "enrolled devices (cumulative, by enrollment date)",
                **fi.project(series, limit=float(limit)),
            },
        }
        if self.c.alerts is not None:
            alerts = await self.c.alerts.repo.search_alerts(
                AlertFilter(devices=frozenset(devices), since=now - timedelta(days=60)), 20000
            )
            out["alert_volume"] = {
                "metric": "alerts per day",
                **fi.project(fi.daily([(a.created_at, 1.0) for a in alerts]), limit=None),
            }
        if platform:
            out["storage"] = await self._storage(now)
        return out

    async def _storage(self, now: datetime) -> dict[str, Any]:
        """Platform-wide: telemetry chunk sizes per day (TimescaleDB) and total database size."""
        db = self.c.db
        if db is None or not db.timescale:
            return {"status": "NOT_MEASURED", "reason": "no TimescaleDB"}
        try:
            async with db.sessions() as s:
                total = (await s.execute(text("SELECT pg_database_size(current_database())"))).scalar()
                rows = (
                    await s.execute(
                        text(
                            "SELECT c.range_start, "
                            "pg_total_relation_size(format('%I.%I', c.chunk_schema, c.chunk_name)::regclass) "
                            "FROM timescaledb_information.chunks c "
                            "WHERE c.hypertable_name = 'telemetry_samples' "
                            "AND NOT c.is_compressed ORDER BY c.range_start"
                        )
                    )
                ).all()
        except Exception as exc:
            return {"status": "NOT_MEASURED", "reason": str(exc)[:200]}
        # completed days only: today's chunk is still filling
        days = [(r[0], float(r[1])) for r in rows if r[0] + timedelta(days=1) <= now]
        per_day = fi.project(days, limit=None, min_points=7)
        return {
            "database_bytes": int(total or 0),
            "uncompressed_chunk_bytes_per_day": [
                {"day": d.date().isoformat(), "bytes": int(b)} for d, b in days
            ],
            "daily_volume_trend": per_day,
            "note": "chunks are compressed after 2 days (~18x measured); raw samples are kept 30 days",
        }

    # ------------------------------------------------------------------ operations summary
    async def operations(self, org_id: str, devices: set[str], platform: bool) -> dict[str, Any]:
        now = datetime.now(UTC)
        facts, rows = await self.facts(org_id, devices, now)
        presence = Counter(r["presence"] for r in rows)
        health = Counter(f.health or "UNKNOWN" for f in facts)
        upcoming = []
        for d in sorted(devices):
            for p in self._predictions_24h(d, now):
                upcoming.append(
                    {
                        "device_id": d,
                        "target": p.target_id,
                        "crossing_at": p.crossing_at.isoformat() if p.crossing_at else None,
                        "confidence": p.confidence_band,
                        "statement": p.statement,
                    }
                )
        recommended = self.c.policies.effective("agent", org_id)[0].get("recommended_version")
        out: dict[str, Any] = {
            "generated_at": now.isoformat(),
            "devices": len(devices),
            "presence": dict(presence),
            "health": dict(health),
            "critical_devices": sorted(f.device_id for f in facts if f.health == "CRITICAL"),
            "warning_devices": sorted(f.device_id for f in facts if f.health == "WARNING"),
            "active_anomalies": sum(sum(f.anomalies.values()) for f in facts),
            "upcoming_crossings_24h": sorted(upcoming, key=lambda u: u["crossing_at"] or ""),
            "agents": {
                "recommended_version": recommended,
                "by_version": dict(Counter(r["agent_version"] or "unknown" for r in rows)),
                "outdated": sorted(f.device_id for f in facts if f.agent_outdated),
                "legacy_enrolled": sorted(r["device_id"] for r in rows if r["enrollment"] == "legacy key"),
            },
            "compliance": dict(Counter(r["compliance"] for r in rows)),
            "remediation_outcomes": self._remediation_outcomes(devices),
        }
        if platform:
            ing = self.c.ingest
            out["pipeline"] = {
                "persist_queue_depth": self.c.persister.depth,
                "persist_oldest_age_s": self.c.persister.oldest_age_s(),
                "ingest_inflight": ing.inflight,
                "background": self.c.supervisor.status(),
            }
            if self.c.alerts is not None:
                count, oldest = await self.c.alerts.repo.backlog(now)
                out["notification_backlog"] = {"due": count, "oldest_due_s": oldest}
        return out


# ---------------------------------------------------------------------------------- model governance
async def model_governance(
    c: Any, org_id: str, devices: set[str], platform: bool, days: int = 30
) -> dict[str, Any]:
    """Quality of the detection / prediction / diagnosis models over the caller's devices.

    Labels come from operators (anomaly feedback) and from outcomes (predictions confirmed / expired).
    Rates are given only with their sample sizes; without labels they are reported as NO_LABELS.
    """
    from app.repositories.predictions import CLOSED_STATUSES, PredictionFilter, calibration

    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    anomalies = await c.event_repo.fleet_anomalies(sorted(devices), since, 20_000)
    by_detector: dict[str, Counter[str]] = defaultdict(Counter)
    for a in anomalies:
        key = f"{a.detector.value}:{a.anomaly_type.value}"
        by_detector[key]["detected"] += 1
        verdict = (a.feedback or {}).get("verdict")
        if verdict:
            by_detector[key][verdict] += 1
    detectors = []
    for key, n in sorted(by_detector.items()):
        labelled = n["true_positive"] + n["false_positive"]
        detectors.append(
            {
                "detector": key,
                "detected": n["detected"],
                "labelled": labelled,
                "label_coverage": round(labelled / n["detected"], 3) if n["detected"] else None,
                "false_positive_rate": round(n["false_positive"] / labelled, 3) if labelled >= 5 else None,
                "status": "OK" if labelled >= 5 else "NO_LABELS" if labelled == 0 else "FEW_LABELS",
            }
        )
    intel = getattr(c, "intelligence", None)
    out: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "window_days": days,
        "anomaly_detection": {
            "detectors": detectors,
            "config_version": (intel.config_meta.get("version") if intel is not None else None),
            "detection_delay": "NOT_MEASURED (requires labelled onset times)",
            "note": "false-positive rate needs at least 5 operator verdicts per detector",
        },
    }
    f = getattr(c, "forecasts", None)
    if f is not None:
        items = await f.repo.search(None, PredictionFilter(statuses=CLOSED_STATUSES, since=since), 5000)
        mine = [p for p in items if p.device_id in devices]
        versions = Counter(f"{p.model_type}@{p.model_version}" for p in mine)
        out["prediction"] = {**calibration(mine), "models_used": dict(versions)}
    if platform and getattr(c, "diagnosis", None) is not None:
        st = await c.diagnosis.status()
        out["diagnosis"] = {
            "provider": st["provider"],
            "prompt_version": st["prompt_version"],
            "stats": st["stats"],
            "feedback": st["feedback"],
            "queue_depth": st["queue_depth"],
        }
    return out
