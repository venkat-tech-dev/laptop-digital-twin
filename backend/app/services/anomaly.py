from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.anomalies.models import Anomaly
from app.repositories.base import AnomalyFilter, EventRepository, TelemetryRepository
from app.services.admin import AdminService
from app.services.digital_twin import DigitalTwinService
from app.services.insights import (
    CORRELATION_CANDIDATES,
    ProcessHistory,
    attribute_processes,
    build_summary,
    correlate,
    detection_confidence,
)


class AnomalyService:
    """Read side for anomalies: active ones from the live engine, history from the repository.

    Every anomaly is returned with its persisted acknowledgement (if any) and a detection confidence.
    """

    def __init__(
        self,
        twin: DigitalTwinService,
        repo: EventRepository,
        admin: AdminService,
        telemetry_repo: TelemetryRepository,
        processes: ProcessHistory,
    ) -> None:
        self._twin = twin
        self._repo = repo
        self._admin = admin
        self._telemetry = telemetry_repo
        self._processes = processes
        self.intelligence: Any = None  # IntelligenceService (wired by the container)

    # ------------------------------------------------------------------ Phase 4 read side
    def live(self, device_id: str) -> list[Anomaly]:
        """Active anomalies of one device: safety thresholds + behavioral/multivariate."""
        twin = self._twin.get(device_id) if self._twin.has_device(device_id) else None
        if twin is None:
            return []
        items = [*twin.anomalies.active.values(), *twin.behavior_active]
        return sorted(items, key=lambda a: a.started_at, reverse=True)

    async def _acks(self, device_id: str) -> dict[str, Any]:
        try:
            return await self._admin.acks(device_id)
        except Exception:
            return {}

    def _full(self, a: Anomaly, acks: dict[str, Any]) -> dict[str, Any]:
        out = a.to_dict()
        ack = acks.get(a.anomaly_id)
        out["acknowledgement"] = ack.public() if ack else None
        if ack and out["lifecycle"] in ("DETECTED", "ONGOING"):
            out["lifecycle"] = "ACKNOWLEDGED"
        out["confidence_band"] = (a.evidence or {}).get("confidence_band")
        return out

    async def active(self, device_id: str) -> list[dict[str, Any]]:
        acks = await self._acks(device_id)
        return [self._full(a, acks) for a in self.live(device_id)]

    async def history(
        self, device_id: str, filters: AnomalyFilter, limit: int, offset: int
    ) -> dict[str, Any]:
        acks = await self._acks(device_id)
        live = {a.anomaly_id: a for a in self.live(device_id)}
        source = "persisted"
        try:
            stored = await self._repo.search_anomalies(device_id, filters, limit + len(live), offset)
        except Exception:
            source = "memory (persistence unavailable)"
            twin = self._twin.get(device_id) if self._twin.has_device(device_id) else None
            stored = list(twin.anomalies_recent) if twin is not None else []
        from app.repositories.memory import matches

        merged: dict[str, Anomaly] = {}
        if filters.status in (None, "active") and offset == 0:
            merged.update({k: a for k, a in live.items() if matches(a, filters)})
        for a in stored:
            # the live object is fresher than the last persisted row
            merged.setdefault(a.anomaly_id, live.get(a.anomaly_id, a))
        items = sorted(merged.values(), key=lambda a: a.started_at, reverse=True)[:limit]
        return {
            "device_id": device_id,
            "items": [self._full(a, acks) for a in items],
            "limit": limit,
            "offset": offset,
            "source": source,
        }

    async def get(self, anomaly_id: str) -> dict[str, Any] | None:
        a = await self.find(anomaly_id)
        if a is None:
            return None
        return self._full(a, await self._acks(a.device_id))

    def _decorate(self, a: Anomaly, acks: dict[str, Any]) -> dict[str, Any]:
        out = a.to_dict()
        ack = acks.get(a.anomaly_id)
        out["acknowledgement"] = ack.public() if ack else None
        out["confidence"] = detection_confidence(a)
        return out

    async def query(
        self,
        device_id: str | None,
        status: str | None,
        severity: str | None,
        since: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        twin = self._twin.get(device_id)
        if twin is None:
            return []
        active = {a.anomaly_id: a for a in [*twin.anomalies.active.values(), *twin.behavior_active]}
        try:
            acks = await self._admin.acks(twin.device.device_id)
        except Exception:
            acks = {}
        out: dict[str, dict[str, Any]] = {}
        if status in (None, "active"):
            for a in active.values():
                if severity is None or a.severity.value == severity:
                    out[a.anomaly_id] = self._decorate(a, acks)
        if status in (None, "resolved"):
            try:
                stored = await self._repo.list_anomalies(
                    twin.device.device_id, "resolved", severity, since, limit
                )
            except Exception:
                stored = list(twin.anomalies.recent_resolved)
            for a in stored:
                if a.anomaly_id not in out:
                    out[a.anomaly_id] = self._decorate(a, acks)
        items = sorted(out.values(), key=lambda a: a["started_at"], reverse=True)
        if since is not None:
            items = [a for a in items if a["started_at"] >= since.isoformat()]
        return items[:limit]

    async def find(self, anomaly_id: str) -> Anomaly | None:
        for device in self._twin.devices():
            for a in self.live(device.device_id):
                if a.anomaly_id == anomaly_id:
                    return a
            twin = self._twin.get(device.device_id)
            for a in twin.anomalies_recent if twin is not None else ():
                if a.anomaly_id == anomaly_id:
                    return a
        try:
            return await self._repo.get_anomaly(anomaly_id)
        except Exception:
            return None

    async def acknowledge(self, anomaly_id: str, by: str, note: str | None) -> dict[str, Any]:
        anomaly = await self.find(anomaly_id)
        if anomaly is None:
            raise LookupError("Anomaly not found")
        ack = await self._admin.acknowledge(anomaly_id, anomaly.device_id, by, note)
        if self.intelligence is not None:
            self.intelligence.acknowledge(anomaly)
        return self._decorate(anomaly, {anomaly_id: ack})

    async def unacknowledge(self, anomaly_id: str) -> dict[str, Any]:
        anomaly = await self.find(anomaly_id)
        if anomaly is None:
            raise LookupError("Anomaly not found")
        await self._admin.unacknowledge(anomaly_id)
        return self._decorate(anomaly, {})

    async def analyze(self, anomaly_id: str) -> dict[str, Any]:
        """Correlated signals and process attribution over the anomaly window (see services.insights)."""
        anomaly = await self.find(anomaly_id)
        if anomaly is None:
            raise LookupError("Anomaly not found")
        twin = self._twin.get(anomaly.device_id)
        now = datetime.now(UTC)
        end = anomaly.resolved_at or now
        start = max(anomaly.started_at - timedelta(minutes=10), end - timedelta(hours=2))
        bucket = 10 if (end - start) <= timedelta(minutes=40) else 30
        keys = [k for k in CORRELATION_CANDIDATES if k != anomaly.metric_key]
        if twin is not None:
            keys = [k for k in keys if any(k in c.telemetry for c in twin.components.values())]
        series: dict[str, dict[datetime, float]] = {}
        source = "persisted history"
        try:
            hist = await self._telemetry.history(
                anomaly.device_id, [anomaly.metric_key, *keys], start, end, bucket
            )
            series = {k: {p.time: p.avg for p in pts} for k, pts in hist.items()}
        except Exception:
            source = "unavailable (no persistence)"
        target = series.pop(anomaly.metric_key, {})
        correlated = correlate(target, series) if target else []

        before_start = anomaly.started_at - timedelta(minutes=10)
        during = self._processes.window(anomaly.device_id, anomaly.started_at, end)
        before = self._processes.window(anomaly.device_id, before_start, anomaly.started_at)
        by_memory = anomaly.metric_key.startswith("memory")
        procs = attribute_processes(during, before, by_memory) if during else []
        first, count = self._processes.coverage(anomaly.device_id)
        return {
            "anomaly_id": anomaly.anomaly_id,
            "metric_key": anomaly.metric_key,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "bucket_seconds": bucket},
            "confidence": detection_confidence(anomaly, now),
            "correlated_signals": correlated[:6],
            "signal_source": source,
            "target_samples": len(target),
            "process_attribution": {
                "ranked_by": "memory" if by_memory else "cpu",
                "snapshots_during": len(during),
                "snapshots_before": len(before),
                "history_since": first.isoformat() if first else None,
                "history_snapshots": count,
                "processes": procs,
                "note": None
                if during
                else "No process snapshots cover this window (kept in memory for 60 minutes).",
            },
            "summary": build_summary(anomaly, correlated, procs),
        }

    def rules(self) -> list[dict[str, Any]]:
        from app.domain.anomalies.rules import DEFAULT_RULES
        from app.domain.anomalies.signals import SIGNALS

        policy = self.intelligence.policy if self.intelligence is not None else None
        z = policy.z_trigger if policy else 3.5
        persist = policy.persistence_s if policy else 180
        return [
            *(
                {
                    "rule_id": r.rule_id,
                    "detector": "rule",
                    "type": "threshold_anomaly",
                    "metric": r.metric,
                    "condition": f"{r.op} {r.threshold}",
                    "duration_s": r.duration_s,
                    "severity": r.severity.value,
                    "title": r.title,
                }
                for r in DEFAULT_RULES
            ),
            *(
                {
                    "rule_id": f"behavior.{s.signal_id}",
                    "detector": "behavioral",
                    "type": "behavioral_anomaly",
                    "metric": s.field,
                    "condition": f"robust z >= {z:g} / above p99 / EWMA level shift, and deviation >= "
                    f"{s.min_delta:g} {s.unit} (device baseline)",
                    "duration_s": persist,
                    "severity": "points-based (INFO..CRITICAL)",
                    "title": s.title,
                }
                for s in SIGNALS
            ),
            {
                "rule_id": "multivariate.iforest",
                "detector": "multivariate",
                "type": "multivariate_anomaly",
                "metric": "multivariate",
                "condition": "Isolation Forest score above the device's 99.5th training percentile",
                "duration_s": persist,
                "severity": "points-based (INFO..CRITICAL)",
                "title": "Unusual combination of signals",
            },
        ]
