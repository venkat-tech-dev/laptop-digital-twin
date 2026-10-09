"""Telemetry ingest orchestration and telemetry queries."""

from __future__ import annotations

import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.core.metrics import (
    COLLECTION_FAILURES,
    EVENT_PROCESSING,
    INGEST_BATCHES,
    INGEST_SAMPLES,
    SENSOR_AVAILABLE,
)
from app.domain.anomalies.models import Anomaly
from app.domain.events.bus import EventBus
from app.domain.events.events import (
    BatteryStateChanged,
    DeviceStatusChanged,
    DomainEvent,
    HealthChanged,
    SensorUnavailable,
    ThermalStateChanged,
)
from app.domain.telemetry.models import MetricReading
from app.infrastructure.redis.client import RedisGateway
from app.repositories.base import (
    EventRepository,
    HealthEventRecord,
    HistoryPoint,
    MetricDef,
    SystemEventRecord,
    TelemetryRepository,
)
from app.schemas.ingest import InventoryEnvelopeIn, TelemetryBatchIn
from app.services.digital_twin import DigitalTwinService
from app.services.persistence import EventRecorder, SamplePersister

log = structlog.get_logger("telemetry")

# Series excluded from the short-term chart buffer (high cardinality, not charted).
_RECENT_EXCLUDE = ("cpu.core_usage_percent", "gpu.engine_usage_percent", "agent.")


class RecentBuffer:
    """Short-term numeric history for chart backfill (Redis stream when configured, else memory)."""

    def __init__(self, redis: RedisGateway | None, maxlen: int = 2000) -> None:
        self._redis = redis
        self._memory: dict[str, deque[tuple[int, dict[str, float]]]] = {}
        self._maxlen = maxlen

    async def append(self, device_id: str, readings: list[MetricReading]) -> None:
        values = {
            r.key: float(r.numeric)
            for r in readings
            if r.numeric is not None and not r.metric.startswith(_RECENT_EXCLUDE)
        }
        if not values:
            return
        ts_ms = int(max(r.timestamp for r in readings).timestamp() * 1000)
        mem = self._memory.setdefault(device_id, deque(maxlen=self._maxlen))
        mem.append((ts_ms, values))
        if self._redis is not None:
            try:
                await self._redis.append_recent(device_id, ts_ms, values)
            except (
                Exception
            ) as exc:  # Redis is optional: fall back silently to memory, but log once per error
                log.debug("redis_recent_failed", error=str(exc))

    async def read(self, device_id: str, since_ms: int) -> list[tuple[int, dict[str, float]]]:
        mem = [p for p in self._memory.get(device_id, ()) if p[0] >= since_ms]
        if mem or self._redis is None:
            return mem
        try:
            return await self._redis.read_recent(device_id, since_ms)
        except Exception:
            return mem


class TelemetryService:
    def __init__(
        self,
        twin: DigitalTwinService,
        bus: EventBus,
        persister: SamplePersister,
        recorder: EventRecorder,
        telemetry_repo: TelemetryRepository,
        event_repo: EventRepository,
        recent: RecentBuffer,
        redis: RedisGateway | None,
    ) -> None:
        self._twin = twin
        self._bus = bus
        self._persister = persister
        self._recorder = recorder
        self._telemetry_repo = telemetry_repo
        self._event_repo = event_repo
        self._recent = recent
        self._redis = redis
        self._last_hot_save: dict[str, float] = {}
        self.twin_state: Any = None  # TwinStateService, wired by the container
        #: Full-twin snapshot to Redis per device at most this often (CPU: ~1 snapshot/device/interval).
        self.hot_state_interval_s = 10.0

    # ------------------------------------------------------------------ ingest
    async def ingest_inventory(self, envelope: InventoryEnvelopeIn, device_repo: Any) -> bool:
        twin, created = self._twin.apply_inventory(envelope)
        if self.twin_state is not None:
            if not created:
                await self.twin_state.topology_changed(envelope.device_id)
            await self.twin_state.on_applied(envelope.device_id)
        self._recorder.submit(lambda: device_repo.upsert(twin.device))
        self._recorder.submit(
            lambda: device_repo.upsert_components(envelope.device_id, list(twin.components.values()))
        )
        if created:
            await self._record_system(
                envelope.device_id,
                "inventory_received",
                "info",
                f"Hardware discovered: {twin.device.manufacturer} {twin.device.model}",
                {"agent_version": envelope.agent_version},
            )
        if self._redis is not None:
            try:
                await self._redis.set_last_device(envelope.device_id)
            except Exception as exc:
                log.debug("redis_set_last_device_failed", error=str(exc))
        return created

    async def ingest(self, batch: TelemetryBatchIn, received_at: datetime | None = None) -> int:
        started = time.perf_counter()
        result = self._twin.update(batch, received_at)
        EVENT_PROCESSING.observe(time.perf_counter() - started)
        INGEST_BATCHES.labels("accepted").inc()
        qualities: dict[str, int] = {}
        primary = batch.device_id == self._twin.primary_device_id
        for r in result.readings:
            q = r.quality.value
            qualities[q] = qualities.get(q, 0) + 1
            if not primary:
                continue  # per-metric gauges describe the primary device (bounded label cardinality)
            if r.metric == "agent.provider_failures_total" and r.numeric is not None:
                COLLECTION_FAILURES.labels(r.labels.get("provider", "?")).set(r.numeric)
            elif not r.labels:
                SENSOR_AVAILABLE.labels(r.metric).set(1 if r.available else 0)
        for q, n in qualities.items():
            INGEST_SAMPLES.labels(q).inc(n)

        self._persister.enqueue(batch.device_id, result.readings)
        await self._recent.append(batch.device_id, result.readings)
        for a in result.anomalies_changed:
            self._record_anomaly(a)
        for h in result.health_changes:
            self._record_health(h)
        for e in result.events:
            self._record_event(e)
        await self._bus.publish_all(result.events)
        if self.twin_state is not None:
            await self.twin_state.on_applied(batch.device_id, received_at)
        await self._save_hot_state(batch.device_id)
        return len(result.readings)

    async def check_liveness(self) -> None:
        events = self._twin.check_liveness()
        for e in events:
            self._record_event(e)
        await self._bus.publish_all(events)

    async def _save_hot_state(self, device_id: str) -> None:
        now = time.monotonic()
        if self._redis is None or now - self._last_hot_save.get(device_id, 0.0) < self.hot_state_interval_s:
            return
        self._last_hot_save[device_id] = now
        snapshot = self._twin.snapshot(device_id)
        if snapshot is None:
            return
        try:
            await self._redis.save_twin(device_id, snapshot)
        except Exception as exc:
            log.debug("redis_save_failed", error=str(exc))

    # -------------------------------------------------------------- recording
    def _record_anomaly(self, anomaly: Anomaly) -> None:
        self._recorder.submit(lambda: self._event_repo.upsert_anomaly(anomaly))

    def record_anomaly(self, anomaly: Anomaly) -> None:
        """Durably upsert an anomaly (Phase-4 behavioral path; bounded queue, never blocks)."""
        self._record_anomaly(anomaly)

    def _record_health(self, event: HealthChanged) -> None:
        significant = event.previous_status != event.status or (
            event.previous_score is not None
            and event.score is not None
            and abs(event.score - event.previous_score) >= 5
        )
        if not significant:
            return
        record = HealthEventRecord(
            event.device_id,
            event.component_id,
            event.occurred_at,
            event.previous_score,
            event.score,
            event.previous_status,
            event.status,
            event.reasons,
        )
        self._recorder.submit(lambda: self._event_repo.add_health_event(record))

    def _record_event(self, event: DomainEvent) -> None:
        if isinstance(event, DeviceStatusChanged):
            self._submit_system(
                event.device_id,
                event.name,
                "warning" if event.status == "OFFLINE" else "info",
                f"Device status {event.previous_status} -> {event.status}",
                event.payload(),
            )
        elif isinstance(event, SensorUnavailable):
            self._submit_system(
                event.device_id,
                "SensorRecovered" if event.available else "SensorUnavailable",
                "info",
                f"{event.metric_key}: {'available' if event.available else event.reason}",
                event.payload(),
            )
        elif isinstance(event, (BatteryStateChanged, ThermalStateChanged)):
            self._submit_system(
                event.device_id,
                event.name,
                "info",
                f"{event.component_id}: {event.previous_state} -> {event.current_state}",
                event.payload(),
            )

    def record_device_events(self, device_id: str, events: list[Any]) -> None:
        """Persist agent-reported DEVICE_EVENTS (crashes, connectivity, services...) as system events."""
        for e in events:
            self._submit_record(
                SystemEventRecord(
                    device_id,
                    e.timestamp,
                    f"device.{e.type}",
                    e.severity,
                    e.message,
                    {**e.data, "event_id": e.event_id, "source": e.source},
                    event_uid=e.event_id,
                    priority=getattr(e, "priority", None),
                    category=getattr(e, "category", None),
                )
            )

    def record_system_event(self, record: SystemEventRecord) -> None:
        self._submit_record(record)

    def _submit_record(self, record: SystemEventRecord) -> None:
        self._recorder.submit(lambda: self._event_repo.add_system_event(record))

    def _submit_system(
        self, device_id: str, event_type: str, severity: str, message: str, data: dict[str, Any]
    ) -> None:
        record = SystemEventRecord(device_id, datetime.now(UTC), event_type, severity, message, data)
        self._recorder.submit(lambda: self._event_repo.add_system_event(record))

    async def _record_system(
        self, device_id: str, event_type: str, severity: str, message: str, data: dict[str, Any]
    ) -> None:
        self._submit_system(device_id, event_type, severity, message, data)

    # ----------------------------------------------------------------- queries
    async def recent(self, device_id: str, seconds: int) -> list[tuple[int, dict[str, float]]]:
        since = int((datetime.now(UTC) - timedelta(seconds=seconds)).timestamp() * 1000)
        return await self._recent.read(device_id, since)

    async def history(
        self, device_id: str, keys: list[str], start: datetime, end: datetime, bucket_s: int
    ) -> dict[str, list[HistoryPoint]]:
        return await self._telemetry_repo.history(device_id, keys, start, end, bucket_s)

    async def catalog(self, device_id: str) -> list[MetricDef]:
        return await self._telemetry_repo.list_metrics(device_id)
