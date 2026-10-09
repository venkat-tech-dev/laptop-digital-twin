"""In-memory repositories (bounded). Used in tests and when DATABASE_URL is not configured.

They are NOT a durable store: the readiness endpoint reports persistence as ``memory`` so the
operator knows history will be lost on restart.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from typing import Any

from app.domain.anomalies.models import LEGACY_LEVEL, Anomaly
from app.domain.components.models import Component
from app.domain.devices.models import Device
from app.repositories.base import (
    AnomalyFilter,
    HealthEventRecord,
    HistoryPoint,
    MetricDef,
    SampleRow,
    SystemEventRecord,
)


class MemoryDeviceRepository:
    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        self.components: dict[str, list[Component]] = {}

    async def upsert(self, device: Device) -> None:
        self.devices[device.device_id] = device

    async def touch(self, device_id: str, last_seen: datetime) -> None:
        if device_id in self.devices:
            self.devices[device_id].last_seen = last_seen

    async def get(self, device_id: str) -> Device | None:
        return self.devices.get(device_id)

    async def list_all(self) -> list[Device]:
        return list(self.devices.values())

    async def upsert_components(self, device_id: str, components: list[Component]) -> None:
        self.components[device_id] = list(components)


class MemoryTelemetryRepository:
    def __init__(self, max_points_per_series: int = 20_000) -> None:
        self.metrics: dict[str, dict[str, MetricDef]] = defaultdict(dict)
        self.samples: dict[tuple[str, str], deque[tuple[datetime, float]]] = defaultdict(
            lambda: deque(maxlen=max_points_per_series)
        )

    async def write_samples(
        self, device_id: str, metrics: dict[str, MetricDef], samples: list[SampleRow]
    ) -> int:
        self.metrics[device_id].update(metrics)
        for s in samples:
            self.samples[(device_id, s.key)].append((s.time, s.value))
        return len(samples)

    async def history(
        self, device_id: str, keys: list[str], start: datetime, end: datetime, bucket_s: int
    ) -> dict[str, list[HistoryPoint]]:
        out: dict[str, list[HistoryPoint]] = {}
        for key in keys:
            buckets: dict[float, list[float]] = defaultdict(list)
            for t, v in self.samples.get((device_id, key), ()):
                if start <= t < end:
                    ts = t.timestamp()
                    buckets[ts - ts % bucket_s].append(v)
            out[key] = [
                HistoryPoint(
                    datetime.fromtimestamp(b, tz=start.tzinfo), sum(vs) / len(vs), min(vs), max(vs), len(vs)
                )
                for b, vs in sorted(buckets.items())
            ]
        return out

    async def raw_values(
        self, device_id: str, key: str, start: datetime, end: datetime, limit: int = 20_000
    ) -> list[tuple[datetime, float]]:
        return [p for p in self.samples.get((device_id, key), ()) if start <= p[0] < end][-limit:]

    async def list_metrics(self, device_id: str) -> list[MetricDef]:
        return list(self.metrics.get(device_id, {}).values())

    async def purge_older_than(self, cutoff: datetime) -> int:
        removed = 0
        for series in self.samples.values():
            while series and series[0][0] < cutoff:
                series.popleft()
                removed += 1
        return removed


class MemoryEventRepository:
    def __init__(self, max_items: int = 2000) -> None:
        self.health: deque[HealthEventRecord] = deque(maxlen=max_items)
        self.anomalies: dict[str, Anomaly] = {}
        self.system: deque[SystemEventRecord] = deque(maxlen=max_items)
        self._event_uids: set[str] = set()
        self._max = max_items

    async def add_health_event(self, record: HealthEventRecord) -> None:
        self.health.appendleft(record)

    async def list_health_events(self, device_id: str, limit: int) -> list[HealthEventRecord]:
        return [h for h in self.health if h.device_id == device_id][:limit]

    async def upsert_anomaly(self, anomaly: Anomaly) -> None:
        self.anomalies[anomaly.anomaly_id] = anomaly
        if len(self.anomalies) > self._max:
            oldest = min(self.anomalies.values(), key=lambda a: a.started_at)
            self.anomalies.pop(oldest.anomaly_id, None)

    async def list_anomalies(
        self, device_id: str, status: str | None, severity: str | None, since: datetime | None, limit: int
    ) -> list[Anomaly]:
        items = [
            a
            for a in self.anomalies.values()
            if a.device_id == device_id
            and (status is None or a.status == status)
            and (severity is None or a.severity.value == severity)
            and (since is None or a.started_at >= since)
        ]
        return sorted(items, key=lambda a: a.started_at, reverse=True)[:limit]

    async def fleet_anomalies(self, device_ids: list[str], since: datetime, limit: int) -> list[Anomaly]:
        ids = set(device_ids)
        items = [a for a in self.anomalies.values() if a.device_id in ids and a.started_at >= since]
        return sorted(items, key=lambda a: a.started_at, reverse=True)[:limit]

    async def search_anomalies(
        self, device_id: str, filters: AnomalyFilter, limit: int, offset: int = 0
    ) -> list[Anomaly]:
        items = [a for a in self.anomalies.values() if a.device_id == device_id and matches(a, filters)]
        items.sort(key=lambda a: a.started_at, reverse=True)
        return items[offset : offset + limit]

    async def get_anomaly(self, anomaly_id: str) -> Anomaly | None:
        return self.anomalies.get(anomaly_id)

    async def set_anomaly_feedback(self, anomaly_id: str, feedback: dict[str, Any]) -> bool:
        a = self.anomalies.get(anomaly_id)
        if a is None:
            return False
        a.feedback = feedback
        return True

    async def add_system_event(self, record: SystemEventRecord) -> None:
        if record.event_uid is not None:
            if record.event_uid in self._event_uids:
                return
            self._event_uids.add(record.event_uid)
            if len(self._event_uids) > 50_000:
                self._event_uids.clear()
        self.system.appendleft(record)

    async def purge_system_events_older_than(self, cutoff: datetime) -> int:
        before = len(self.system)
        self.system = deque((e for e in self.system if e.time >= cutoff), maxlen=self.system.maxlen)
        return before - len(self.system)

    async def list_system_events(self, device_id: str, limit: int) -> list[SystemEventRecord]:
        return [e for e in self.system if e.device_id == device_id][:limit]


def matches(a: Anomaly, f: AnomalyFilter) -> bool:
    """In-memory equivalent of the SQL anomaly filters."""
    level = (a.level or LEGACY_LEVEL[a.severity]).value
    return (
        (f.status is None or a.status == f.status)
        and (f.severity is None or a.severity.value == f.severity)
        and (not f.levels or level in f.levels)
        and (not f.types or a.anomaly_type.value in f.types)
        and (f.since is None or a.started_at >= f.since)
        and (f.until is None or a.started_at <= f.until)
        and (f.min_confidence is None or (a.confidence or 0.0) >= f.min_confidence)
        and (f.signal_id is None or a.signal_id == f.signal_id)
    )
