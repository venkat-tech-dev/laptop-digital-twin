"""Repository interfaces. Services depend on these protocols, never on SQLAlchemy directly."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.domain.anomalies.models import Anomaly
from app.domain.components.models import Component
from app.domain.devices.models import Device


@dataclass(frozen=True, slots=True)
class AnomalyFilter:
    """History filters (all optional; combined with AND)."""

    status: str | None = None  # active | resolved
    severity: str | None = None  # legacy info | warning | critical
    levels: tuple[str, ...] = ()  # INFO .. CRITICAL (legacy rows: derived from severity)
    types: tuple[str, ...] = ()  # threshold_anomaly | behavioral_anomaly | ...
    since: datetime | None = None
    until: datetime | None = None
    min_confidence: float | None = None
    signal_id: str | None = None


@dataclass(frozen=True, slots=True)
class MetricDef:
    key: str
    metric: str
    component_id: str
    unit: str
    source: str
    kind: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SampleRow:
    key: str
    time: datetime
    value: float
    quality: int  # 0 GOOD, 1 DEGRADED


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    time: datetime
    avg: float
    min: float
    max: float
    count: int


@dataclass(frozen=True, slots=True)
class HealthEventRecord:
    device_id: str
    component_id: str
    time: datetime
    previous_score: int | None
    score: int | None
    previous_status: str
    status: str
    reasons: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class SystemEventRecord:
    device_id: str
    time: datetime
    event_type: str
    severity: str
    message: str
    data: dict[str, Any]
    event_uid: str | None = None  # agent event id: persistence is idempotent on it
    priority: str | None = None
    category: str | None = None


class DeviceRepository(Protocol):
    async def upsert(self, device: Device) -> None: ...

    async def touch(self, device_id: str, last_seen: datetime) -> None: ...

    async def get(self, device_id: str) -> Device | None: ...

    async def list_all(self) -> list[Device]: ...

    async def upsert_components(self, device_id: str, components: list[Component]) -> None: ...


class TelemetryRepository(Protocol):
    async def write_samples(
        self, device_id: str, metrics: dict[str, MetricDef], samples: list[SampleRow]
    ) -> int: ...

    async def history(
        self, device_id: str, keys: list[str], start: datetime, end: datetime, bucket_s: int
    ) -> dict[str, list[HistoryPoint]]: ...

    async def raw_values(
        self, device_id: str, key: str, start: datetime, end: datetime, limit: int = 20_000
    ) -> list[tuple[datetime, float]]: ...

    async def list_metrics(self, device_id: str) -> list[MetricDef]: ...

    async def purge_older_than(self, cutoff: datetime) -> int: ...


class EventRepository(Protocol):
    async def add_health_event(self, record: HealthEventRecord) -> None: ...

    async def list_health_events(self, device_id: str, limit: int) -> list[HealthEventRecord]: ...

    async def upsert_anomaly(self, anomaly: Anomaly) -> None: ...

    async def list_anomalies(
        self, device_id: str, status: str | None, severity: str | None, since: datetime | None, limit: int
    ) -> list[Anomaly]: ...

    async def search_anomalies(
        self, device_id: str, filters: AnomalyFilter, limit: int, offset: int = 0
    ) -> list[Anomaly]: ...

    async def fleet_anomalies(self, device_ids: list[str], since: datetime, limit: int) -> list[Anomaly]:
        """Anomalies of a set of devices (one tenant's visible devices) that started since ``since``."""
        ...

    async def get_anomaly(self, anomaly_id: str) -> Anomaly | None: ...

    async def set_anomaly_feedback(self, anomaly_id: str, feedback: dict[str, Any]) -> bool: ...

    async def add_system_event(self, record: SystemEventRecord) -> None: ...

    async def list_system_events(self, device_id: str, limit: int) -> list[SystemEventRecord]: ...

    async def purge_system_events_older_than(self, cutoff: datetime) -> int: ...
