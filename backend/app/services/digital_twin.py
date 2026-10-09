"""DigitalTwinService: the authoritative current state of each physical device.

Responsibilities:
  * maintain the component hierarchy built from the hardware inventory
  * apply normalized telemetry to the right physical component
  * derive component states and detect state transitions
  * compute explainable health and run anomaly detection
  * produce domain events (published by the caller) and expose the current twin state

It holds no transport or persistence code; routes and background tasks call into it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.domain.anomalies.engine import AnomalyEngine
from app.domain.anomalies.models import Anomaly
from app.domain.components.models import Component, ComponentHealth, ComponentType, HealthStatus
from app.domain.components.state import cpu_temperature, derive_state, thermal_band
from app.domain.components.topology import LAPTOP, build_topology, route_metric
from app.domain.devices.models import Device, DeviceStatus, status_from_age
from app.domain.events.events import (
    AnomalyChanged,
    AnomalyDetected,
    AnomalyResolved,
    BatteryStateChanged,
    ComponentUpdated,
    DeviceOffline,
    DeviceOnline,
    DeviceStatusChanged,
    DomainEvent,
    HealthChanged,
    SensorUnavailable,
    TelemetryReceived,
    ThermalStateChanged,
)
from app.domain.health.engine import HealthEngine
from app.domain.telemetry.models import MetricReading, MetricWindow, Quality, metric_key
from app.schemas.ingest import InventoryEnvelopeIn, MetricSampleIn, ProcessSnapshotIn, TelemetryBatchIn


class UnknownDeviceError(Exception):
    """Telemetry arrived for a device whose inventory has not been received yet."""


@dataclass
class TwinState:
    device: Device
    components: dict[str, Component]
    window: MetricWindow = field(default_factory=MetricWindow)
    health: HealthEngine = field(init=False)
    anomalies: AnomalyEngine = field(init=False)
    overall: ComponentHealth = field(default_factory=lambda: ComponentHealth(None, HealthStatus.UNKNOWN))
    processes: dict[str, Any] | None = None
    batches: int = 0
    device_health: dict[str, Any] | None = None
    agent_health: dict[str, Any] | None = None
    recent_events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))
    #: reading key -> (batch_id, sequence, received_at): which telemetry produced the current value
    provenance: dict[str, tuple[str | None, int, datetime]] = field(default_factory=dict)
    #: highest sequence applied to live state (diagnostics; ordering is per reading timestamp)
    last_applied_sequence: int | None = None
    #: Phase 4: active behavioral/multivariate anomalies (owned by IntelligenceService) and the most
    #: recently closed anomalies of every type (twin "recent_anomalies")
    behavior_active: list[Anomaly] = field(default_factory=list)
    anomalies_recent: deque[Anomaly] = field(default_factory=lambda: deque(maxlen=20))
    #: Phase 5: per-target forecast summary (owned by ForecastService) - PREDICTED, never observed
    predictions: dict[str, Any] = field(default_factory=dict)
    #: Phase 7: latest diagnosis summary (owned by DiagnosisService) - DIAGNOSED, an explanation, not a fact
    diagnoses: dict[str, Any] = field(default_factory=dict)
    #: Phase 8: remediation status (owned by RemediationService) - never replaces observed state
    remediation: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.health = HealthEngine(self.window)
        self.anomalies = AnomalyEngine(self.device.device_id)


@dataclass
class UpdateResult:
    events: list[DomainEvent]
    readings: list[MetricReading]
    anomalies_changed: list[Any]
    health_changes: list[HealthChanged]


def _reading_from(sample: MetricSampleIn, component_id: str, received_at: datetime) -> MetricReading:
    ts = sample.timestamp if sample.timestamp.tzinfo else sample.timestamp.replace(tzinfo=UTC)
    # Guard against agent clock skew: never accept readings from the future.
    if ts > received_at:
        ts = received_at
    quality = Quality(sample.quality.value)
    available = sample.availability.value == "available" and sample.value is not None
    return MetricReading(
        key=metric_key(sample.metric, sample.labels),
        metric=sample.metric,
        component_id=component_id,
        value=sample.value,
        unit=sample.unit,
        timestamp=ts,
        source=sample.source,
        quality=quality,
        available=available,
        kind=sample.kind.value,
        reason=sample.reason,
        labels=dict(sample.labels),
        interval_s=sample.interval_ms / 1000.0 if sample.interval_ms else None,
    )


class DigitalTwinService:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._twins: dict[str, TwinState] = {}
        self.primary_device_id: str | None = None

    # ---------------------------------------------------------------- devices
    def _maybe_promote_primary(self, device_id: str) -> None:
        """The primary device (what legacy, unsubscribed dashboards show) is stable: PRIMARY_DEVICE_ID
        if configured, else the first device seen; it only moves when the current one is offline."""
        configured = self._s.primary_device_id
        if configured:
            self.primary_device_id = configured
            return
        current = self._twins.get(self.primary_device_id) if self.primary_device_id else None
        if current is None or current.device.status is DeviceStatus.OFFLINE:
            self.primary_device_id = device_id

    def has_device(self, device_id: str) -> bool:
        return device_id in self._twins

    def get(self, device_id: str | None = None) -> TwinState | None:
        did = device_id or self.primary_device_id
        return self._twins.get(did) if did else None

    def devices(self) -> list[Device]:
        return [t.device for t in self._twins.values()]

    def apply_inventory(
        self, envelope: InventoryEnvelopeIn, now: datetime | None = None
    ) -> tuple[TwinState, bool]:
        """Create or refresh a device from its hardware inventory. Returns (twin, created)."""
        now = now or datetime.now(UTC)
        inv = envelope.inventory
        existing = self._twins.get(envelope.device_id)
        if envelope.agent_id:
            inv = {**inv, "agent_id": envelope.agent_id}
        components = build_topology(inv)
        if existing is not None:
            for cid, comp in components.items():  # keep live telemetry across inventory refreshes
                old = existing.components.get(cid)
                if old is not None:
                    comp.telemetry, comp.current_state, comp.health = (
                        old.telemetry,
                        old.current_state,
                        old.health,
                    )
                    comp.last_updated, comp.availability = old.last_updated, old.availability
            existing.components = components
            existing.device.inventory = inv
            existing.device.agent_version = envelope.agent_version
            existing.device.last_inventory_at = now
            self.primary_device_id = self.primary_device_id or envelope.device_id
            return existing, False
        device = Device(
            device_id=envelope.device_id,
            manufacturer=inv.get("manufacturer"),
            model=inv.get("model"),
            model_number=inv.get("model_number"),
            os_name=(inv.get("os") or {}).get("name"),
            inventory=inv,
            agent_version=envelope.agent_version,
            first_seen=now,
            last_inventory_at=now,
            sensor_provider=inv.get("sensor_provider"),
        )
        twin = TwinState(device, components)
        self._twins[device.device_id] = twin
        self.primary_device_id = self.primary_device_id or device.device_id
        return twin, True

    def restore_device(self, device: Device) -> TwinState:
        """Rebuild a twin from a persisted device (after a backend restart). Telemetry arrives fresh."""
        twin = self._twins.get(device.device_id)
        if twin is None:
            twin = TwinState(device, build_topology(device.inventory))
            device.status = DeviceStatus.OFFLINE
            self._twins[device.device_id] = twin
            self.primary_device_id = self.primary_device_id or device.device_id
        return twin

    # ---------------------------------------------------------------- updates
    def update(self, batch: TelemetryBatchIn, now: datetime | None = None) -> UpdateResult:
        now = now or datetime.now(UTC)
        twin = self._twins.get(batch.device_id)
        if twin is None:
            raise UnknownDeviceError(batch.device_id)
        events: list[DomainEvent] = []
        comps = twin.components
        touched: dict[str, dict[str, MetricReading]] = {}
        accepted: list[MetricReading] = []
        applied: list[MetricReading] = []  # readings that updated live state (anomaly input)

        for sample in batch.samples:
            cid = route_metric(sample.metric, sample.labels, comps)
            reading = _reading_from(sample, cid, now)
            accepted.append(reading)
            comp = comps[cid]
            current = comp.telemetry.get(reading.key)
            if current is not None and reading.timestamp < current.timestamp:
                continue  # replayed older sample: persisted, but does not overwrite live state
            if (
                current is not None
                and reading.timestamp == current.timestamp
                and reading.value == current.value
                and reading.available == current.available
            ):
                continue  # the very same reading again (duplicate delivery): no state change
            warming_up = current is not None and "warming up" in (current.reason or "").lower()
            if current is not None and current.available != reading.available and not warming_up:
                events.append(
                    SensorUnavailable(
                        device_id=batch.device_id,
                        metric_key=reading.key,
                        component_id=cid,
                        reason=reading.reason,
                        available=reading.available,
                    )
                )
            elif current is None and not reading.available and reading.quality is Quality.ERROR:
                events.append(
                    SensorUnavailable(
                        device_id=batch.device_id,
                        metric_key=reading.key,
                        component_id=cid,
                        reason=reading.reason,
                        available=False,
                    )
                )
            comp.telemetry[reading.key] = reading
            if reading.labels and reading.available:
                placeholder = comp.telemetry.get(reading.metric)
                if placeholder is not None and not placeholder.available:
                    del comp.telemetry[
                        reading.metric
                    ]  # provider-failure placeholder superseded by real series
            comp.last_updated = max(comp.last_updated or reading.timestamp, reading.timestamp)
            applied.append(reading)
            twin.provenance[reading.key] = (batch.batch_id, batch.sequence, now)
            touched.setdefault(cid, {})[reading.key] = reading
            if reading.numeric is not None and reading.quality is Quality.GOOD:
                twin.window.add(reading.key, reading.timestamp.timestamp(), reading.numeric)

        live_processes = batch.processes is not None and not batch.replay
        if live_processes and batch.processes is not None:
            twin.processes = _processes_dict(batch.processes)
        self._apply_endpoint_state(twin, batch)

        events += self._refresh_states(twin, touched.keys())
        health_events = self._refresh_health(twin, now)
        events += health_events

        transitions = twin.anomalies.evaluate(applied, comps)
        for a in transitions.opened:
            body = a.to_dict()
            events.append(AnomalyDetected(device_id=batch.device_id, anomaly=body))
            events.append(AnomalyChanged(device_id=batch.device_id, kind="detected", anomaly=body))
        for a in transitions.resolved:
            body = a.to_dict()
            twin.anomalies_recent.appendleft(a)
            events.append(AnomalyResolved(device_id=batch.device_id, anomaly=body))
            events.append(AnomalyChanged(device_id=batch.device_id, kind="resolved", anomaly=body))

        device = twin.device
        previous = device.status
        device.last_seen = now
        device.last_sequence = batch.sequence
        device.agent_version = batch.agent_version
        device.status = DeviceStatus.LIVE
        twin.batches += 1
        self._maybe_promote_primary(batch.device_id)
        if previous is not DeviceStatus.LIVE:
            cls = (
                DeviceOnline
                if previous in (DeviceStatus.OFFLINE, DeviceStatus.STALE)
                else DeviceStatusChanged
            )
            events.append(
                cls(
                    device_id=device.device_id,
                    previous_status=previous.value,
                    status=device.status.value,
                    last_seen_at=now,
                )
            )

        events.insert(
            0,
            TelemetryReceived(
                device_id=device.device_id,
                sequence=batch.sequence,
                components={
                    cid: self.component_delta(comps[cid], keys.values(), now) for cid, keys in touched.items()
                },
                device_status=device.status.value,
                processes=twin.processes if live_processes else None,
                timing={
                    "collected_at": (batch.collected_at or batch.sent_at).isoformat(),
                    "sent_at": batch.sent_at.isoformat(),
                    "server_received_at": now.isoformat(),
                    "published_at": datetime.now(UTC).isoformat(),
                    "replay": batch.replay,
                },
            ),
        )
        return UpdateResult(events, accepted, transitions.opened + transitions.resolved, health_events)

    @staticmethod
    def _apply_endpoint_state(twin: TwinState, batch: TelemetryBatchIn) -> None:
        """Device/agent health (newest wins, so replayed batches never roll state back) and events."""
        if batch.device_health is not None:
            new = batch.device_health.model_dump(mode="json")
            old = twin.device_health
            if old is None or new["evaluated_at"] >= old.get("evaluated_at", ""):
                twin.device_health = new
        if batch.agent_health is not None:
            new = batch.agent_health.model_dump(mode="json")
            new["received_at"] = datetime.now(UTC).isoformat()
            old = twin.agent_health
            if old is None or (not batch.replay and new["uptime_s"] is not None):
                twin.agent_health = new
        for e in batch.events:
            twin.recent_events.appendleft(e.model_dump(mode="json"))

    def _refresh_states(self, twin: TwinState, touched: Iterable[str]) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        for cid in touched:
            twin.components[cid].recompute_availability()
        for comp in twin.components.values():
            new_state = derive_state(comp, twin.components)
            if new_state != comp.current_state:
                old = comp.current_state
                comp.current_state = new_state
                if old == "unknown" and comp.component_type in (ComponentType.CHASSIS, ComponentType.VRM):
                    continue
                cls: type[ComponentUpdated] = ComponentUpdated
                if comp.component_type is ComponentType.BATTERY:
                    cls = BatteryStateChanged
                elif comp.component_type is ComponentType.THERMAL_SENSORS:
                    cls = ThermalStateChanged
                events.append(
                    cls(
                        device_id=twin.device.device_id,
                        component_id=comp.component_id,
                        previous_state=old,
                        current_state=new_state,
                    )
                )
        return events

    def _refresh_health(self, twin: TwinState, now: datetime) -> list[HealthChanged]:
        results = twin.health.evaluate(twin.components, now.timestamp())
        events: list[HealthChanged] = []
        for cid, new in results.items():
            comp = twin.components[cid]
            old = comp.health
            comp.health = new
            if _health_changed(old, new):
                events.append(
                    HealthChanged(
                        device_id=twin.device.device_id,
                        component_id=cid,
                        previous_score=old.score,
                        score=new.score,
                        previous_status=old.status.value,
                        status=new.status.value,
                        reasons=[r.to_dict() for r in new.reasons],
                    )
                )
        old_overall = twin.overall
        twin.overall = twin.health.overall(results, twin.components)
        twin.components[LAPTOP].health = twin.overall
        if _health_changed(old_overall, twin.overall):
            events.append(
                HealthChanged(
                    device_id=twin.device.device_id,
                    component_id=LAPTOP,
                    previous_score=old_overall.score,
                    score=twin.overall.score,
                    previous_status=old_overall.status.value,
                    status=twin.overall.status.value,
                    reasons=[r.to_dict() for r in twin.overall.reasons],
                )
            )
        return events

    # --------------------------------------------------------------- liveness
    def check_liveness(self, now: datetime | None = None) -> list[DomainEvent]:
        now = now or datetime.now(UTC)
        events: list[DomainEvent] = []
        for twin in self._twins.values():
            d = twin.device
            age = (now - d.last_seen).total_seconds() if d.last_seen else None
            status = status_from_age(
                age, self._s.degraded_after_s, self._s.stale_after_s, self._s.offline_after_s
            )
            if status is not d.status:
                previous = d.status
                d.status = status
                cls = DeviceOffline if status is DeviceStatus.OFFLINE else DeviceStatusChanged
                events.append(
                    cls(
                        device_id=d.device_id,
                        previous_status=previous.value,
                        status=status.value,
                        last_seen_at=d.last_seen,
                    )
                )
        return events

    # --------------------------------------------------------------- read side
    def _ro(self, r: MetricReading, now: datetime) -> dict[str, Any]:
        return r.to_dict(now, self._s.degraded_after_s, self._s.stale_after_s)

    def component_delta(
        self, comp: Component, readings: Iterable[MetricReading], now: datetime
    ) -> dict[str, Any]:
        return {
            "current_state": comp.current_state,
            "availability": comp.availability.value,
            "health": comp.health.to_dict(),
            "last_updated": comp.last_updated.isoformat() if comp.last_updated else None,
            "telemetry": {r.key: self._ro(r, now) for r in readings},
        }

    def component_dict(
        self, comp: Component, now: datetime, include_telemetry: bool = True
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "component_id": comp.component_id,
            "component_type": comp.component_type.value,
            "name": comp.name,
            "parent_id": comp.parent_id,
            "manufacturer": comp.manufacturer,
            "model": comp.model,
            "properties": comp.properties,
            "current_state": comp.current_state,
            "health": comp.health.to_dict(),
            "availability": comp.availability.value,
            "last_updated": comp.last_updated.isoformat() if comp.last_updated else None,
        }
        if include_telemetry:
            out["telemetry"] = {k: self._ro(r, now) for k, r in sorted(comp.telemetry.items())}
        return out

    def thermal_summary(self, twin: TwinState) -> dict[str, Any]:
        temp = cpu_temperature(twin.components)
        return {
            "cpu_area_temperature_c": round(temp.value, 2) if temp else None,
            "sensor": temp.label if temp else None,
            "metric_key": temp.metric if temp else None,
            "source": temp.source if temp else None,
            "band": thermal_band(temp.value if temp else None),
            "is_cpu_package_sensor": bool(temp and temp.label == "CPU package sensor"),
        }

    def snapshot(self, device_id: str | None = None, now: datetime | None = None) -> dict[str, Any] | None:
        twin = self.get(device_id)
        if twin is None:
            return None
        now = now or datetime.now(UTC)
        return {
            "device_id": twin.device.device_id,
            "device_status": twin.device.status.value,
            "last_seen": twin.device.last_seen.isoformat() if twin.device.last_seen else None,
            "generated_at": now.isoformat(),
            "mode": "live",
            "data_source": "LOCAL WINDOWS HARDWARE",
            "components": [self.component_dict(c, now) for c in twin.components.values()],
            "health": {
                "overall": twin.overall.to_dict(),
                "components": {
                    cid: c.health.to_dict()
                    for cid, c in twin.components.items()
                    if c.health.status is not HealthStatus.UNKNOWN or c.health.reasons
                },
            },
            "thermal": self.thermal_summary(twin),
            "active_anomalies": [a.to_dict() for a in twin.anomalies.active.values()],
            "processes": twin.processes,
        }


def _health_changed(old: ComponentHealth, new: ComponentHealth) -> bool:
    if old.status is not new.status:
        return True
    if old.score is None or new.score is None:
        return old.score != new.score
    return abs(old.score - new.score) >= 3


def _processes_dict(snapshot: ProcessSnapshotIn) -> dict[str, Any]:
    return {
        "timestamp": snapshot.timestamp.isoformat(),
        "source": snapshot.source,
        "total_processes": snapshot.total_processes,
        "unavailable_fields": snapshot.unavailable_fields,
        "details_collected": snapshot.details_collected,
        "processes": [p.model_dump(mode="json") for p in snapshot.processes],
    }
