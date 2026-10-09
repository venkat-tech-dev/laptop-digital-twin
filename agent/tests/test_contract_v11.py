"""Phase-2 wire contract v1.1: schema version, categories, priorities, static suppression."""

from __future__ import annotations

from datetime import UTC, datetime

from app.contracts import (
    SCHEMA_VERSION,
    Availability,
    Category,
    DeviceEvent,
    EventSeverity,
    MetricKind,
    MetricSample,
    Priority,
    Quality,
    TelemetryBatch,
    category_for,
)
from app.publisher.pipeline import BatchAccumulator


def _sample(metric: str, value: float, kind: MetricKind = MetricKind.MEASURED) -> MetricSample:
    return MetricSample(
        metric=metric,
        component="cpu",
        value=value,
        unit="x",
        timestamp=datetime.now(UTC),
        source="t",
        quality=Quality.GOOD,
        availability=Availability.AVAILABLE,
        kind=kind,
    )


def test_batches_carry_schema_version() -> None:
    b = TelemetryBatch(device_id="d", agent_version="t", sequence=1, sent_at=datetime.now(UTC), samples=[])
    assert b.schema_version == SCHEMA_VERSION == "1.2" and b.priority is Priority.NORMAL


def test_categories() -> None:
    assert category_for("cpu.usage_percent") is Category.PERFORMANCE
    assert category_for("memory.total_bytes") is Category.HARDWARE
    assert category_for("security.firewall_enabled") is Category.SECURITY
    assert category_for("system.updates_pending") is Category.OS
    assert category_for("system.app_crashes_24h") is Category.APPLICATIONS
    assert category_for("agent.config_version") is Category.AGENT
    assert category_for("battery.design_capacity_wh", static=True) is Category.HARDWARE


def test_event_priority_defaults() -> None:
    now = datetime.now(UTC)
    crash = DeviceEvent(
        type="app_crash", severity=EventSeverity.ERROR, timestamp=now, source="t", message="m"
    )
    crit = DeviceEvent(
        type="anything", severity=EventSeverity.CRITICAL, timestamp=now, source="t", message="m"
    )
    info = DeviceEvent(type="update_installed", timestamp=now, source="t", message="m")
    assert crash.priority is Priority.HIGH and crash.category is Category.APPLICATIONS
    assert crit.priority is Priority.CRITICAL
    assert info.priority is Priority.NORMAL and info.category is Category.OS
    assert BatchAccumulator.batch_priority([info, crash]) is Priority.HIGH


def test_urgent_events_and_static_suppression() -> None:
    acc = BatchAccumulator(static_resend_s=600)
    now = datetime.now(UTC)
    acc.add_events([DeviceEvent(type="update_installed", timestamp=now, source="t", message="m")])
    assert not acc.urgent()
    acc.add_events([DeviceEvent(type="internet_lost", timestamp=now, source="t", message="m")])
    assert acc.urgent() and acc.events_generated_total == 2
    static = _sample("memory.total_bytes", 16, MetricKind.STATIC)
    live = _sample("cpu.usage_percent", 5.0)
    acc.add([static, live])
    assert len(acc.drain()[0]) == 2
    acc.add([static, live])
    assert [s.metric for s in acc.drain()[0]] == ["cpu.usage_percent"]  # unchanged static suppressed
    acc.add([_sample("memory.total_bytes", 32, MetricKind.STATIC)])
    assert len(acc.drain()[0]) == 1  # a changed static value is sent at once
    acc.resend_static()
    acc.add([_sample("memory.total_bytes", 32, MetricKind.STATIC)])
    assert len(acc.drain()[0]) == 1 and acc.static_suppressed_total == 1
