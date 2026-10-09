"""Wire contract between the host agent and the backend.

These models are the single source of truth for what the agent sends. The backend has a mirror
of this contract in ``backend/app/schemas/ingest.py``; both are validated by tests.

Telemetry is separated into:

* ``InventoryEnvelope``  - DEVICE_STATIC_METADATA (hardware/OS identity; sent on change, not per tick)
* ``MetricSample``       - DEVICE_DYNAMIC_METRICS (one latest value per metric+labels per batch)
* ``DeviceEvent``        - DEVICE_EVENTS (crashes, connectivity changes, service changes, ...)
* ``DeviceHealth``       - DEVICE_HEALTH (normalised compliance/health state with reasons)
* ``AgentHealth``        - AGENT_HEALTH (the agent's own state: queue, sync, collectors, footprint)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

MetricValue = float | int | bool | str | None

#: Wire contract version. 1.0 = Phase-1 (unversioned); 1.1 adds categories, priorities, schema_version;
#: 1.2 adds the per-sample collection interval (drives per-metric freshness) and the agent instance id.
SCHEMA_VERSION = "1.2"


class Category(StrEnum):
    PERFORMANCE = "performance"
    HARDWARE = "hardware"
    OS = "os"
    APPLICATIONS = "applications"
    SECURITY = "security"
    AGENT = "agent"


class Priority(StrEnum):
    """Delivery/retention priority. CRITICAL/HIGH trigger an immediate flush and are kept longest."""

    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"low": 0, "normal": 1, "high": 2, "critical": 3}[self.value]


_PERFORMANCE_PREFIXES = (
    "cpu.",
    "memory.",
    "disk.",
    "network.",
    "thermal.",
    "battery.",
    "power.",
    "gpu.",
    "fan.",
)
_STATIC_HARDWARE = (
    "total_bytes",
    "nominal_frequency",
    "design_capacity",
    "full_charge_capacity",
    "size_bytes",
)


def category_for(metric: str, static: bool = False) -> Category:
    """Telemetry category of a metric name (used for routing, retention and consumers)."""
    if metric.startswith("security."):
        return Category.SECURITY
    if metric.startswith("agent."):
        return Category.AGENT
    if metric.startswith(("system.app_", "process.")):
        return Category.APPLICATIONS
    if metric.startswith(("system.", "os.")):
        return Category.OS
    if static or any(k in metric for k in _STATIC_HARDWARE) or metric.startswith("display."):
        return Category.HARDWARE
    if metric.startswith(_PERFORMANCE_PREFIXES):
        return Category.PERFORMANCE
    return Category.HARDWARE


class Quality(StrEnum):
    GOOD = "GOOD"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class Availability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class MetricKind(StrEnum):
    """How a value was obtained. Never ``simulated`` in a live batch."""

    MEASURED = "measured"  # read directly from an OS / hardware interface
    DERIVED = "derived"  # computed from measured values (rates, ratios, unit products)
    STATIC = "static"  # inventory-like fact (capacity, design values)


class MetricSample(BaseModel):
    metric: str
    component: str
    value: MetricValue
    unit: str
    timestamp: datetime
    source: str
    quality: Quality
    availability: Availability
    kind: MetricKind = MetricKind.MEASURED
    reason: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    category: Category | None = None
    interval_ms: int | None = None  # effective collection interval of the producing collector


class ProcessInfo(BaseModel):
    pid: int
    name: str
    status: str
    cpu_percent: float | None
    memory_rss_bytes: int | None
    memory_percent: float | None
    num_threads: int | None
    gpu_percent: float | None
    io_read_bytes_per_sec: float | None
    io_write_bytes_per_sec: float | None
    handle_count: int | None = None
    started_at: datetime | None = None
    tcp_established: int | None = None
    tcp_listening: int | None = None
    udp_endpoints: int | None = None
    # Opt-in only (COLLECT_PROCESS_DETAILS / backend agent config). Paths have the profile name redacted.
    path: str | None = None
    user: str | None = None
    publisher: str | None = None


class ProcessSnapshot(BaseModel):
    timestamp: datetime
    source: str
    total_processes: int
    processes: list[ProcessInfo]
    unavailable_fields: dict[str, str] = Field(default_factory=dict)
    details_collected: bool = False


class EventSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


EventValue = str | int | float | bool | None


#: Default priority per event type (critical = deliver immediately and never thin away).
EVENT_PRIORITY: dict[str, Priority] = {
    "security_posture_changed": Priority.HIGH,
    "device_health_changed": Priority.HIGH,
    "app_crash": Priority.HIGH,
    "critical_temperature": Priority.CRITICAL,
    "collector_failed": Priority.HIGH,
    "network_disconnected": Priority.HIGH,
    "internet_lost": Priority.HIGH,
    "agent_started": Priority.NORMAL,
    "agent_stopped": Priority.HIGH,
}
EVENT_CATEGORY: dict[str, Category] = {
    "security_posture_changed": Category.SECURITY,
    "device_health_changed": Category.SECURITY,
    "app_crash": Category.APPLICATIONS,
    "app_hang": Category.APPLICATIONS,
    "boot_performance": Category.OS,
    "update_installed": Category.OS,
    "service_state_changed": Category.OS,
    "critical_temperature": Category.PERFORMANCE,
    "network_connected": Category.PERFORMANCE,
    "network_disconnected": Category.PERFORMANCE,
    "internet_lost": Category.PERFORMANCE,
    "internet_restored": Category.PERFORMANCE,
}


class DeviceEvent(BaseModel):
    """A discrete occurrence on the device. ``event_id`` makes replays idempotent."""

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: str
    severity: EventSeverity = EventSeverity.INFO
    timestamp: datetime
    source: str
    message: str
    data: dict[str, EventValue] = Field(default_factory=dict)
    priority: Priority | None = None
    category: Category | None = None

    @model_validator(mode="after")
    def _defaults(self) -> DeviceEvent:
        if self.priority is None:
            critical = self.severity is EventSeverity.CRITICAL
            self.priority = Priority.CRITICAL if critical else EVENT_PRIORITY.get(self.type, Priority.NORMAL)
        if self.category is None:
            self.category = EVENT_CATEGORY.get(
                self.type, Category.AGENT if self.type.startswith(("agent", "collector")) else Category.OS
            )
        return self


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class DeviceHealth(BaseModel):
    """Normalised device posture evaluated on the endpoint from measured facts only."""

    state: HealthState
    reasons: list[str] = Field(default_factory=list)
    checks: dict[str, HealthState] = Field(default_factory=dict)
    evaluated_at: datetime


class CollectorHealth(BaseModel):
    name: str
    lane: str
    interval_ms: int
    last_success_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    total_failures: int = 0
    last_duration_ms: float | None = None


class AgentHealth(BaseModel):
    agent_version: str
    run_mode: str  # service | console | task
    started_at: datetime
    uptime_s: float
    cpu_percent: float | None
    memory_rss_bytes: int | None
    queue_depth: int
    queue_bytes: int
    queue_dropped_total: int
    last_collection_at: datetime | None
    last_sync_at: datetime | None
    sync_failures_consecutive: int
    sync_failures_total: int
    lanes_replaced_total: int
    collectors: list[CollectorHealth] = Field(default_factory=list)
    # Phase-2 pipeline counters (all optional on the wire)
    api_latency_ms: float | None = None
    batches_created_total: int = 0
    batches_uploaded_total: int = 0
    upload_failures_total: int = 0
    events_generated_total: int = 0
    queue_thinned_total: int = 0
    last_sequence: int = 0


class TelemetryBatch(BaseModel):
    schema_version: str = SCHEMA_VERSION
    device_id: str
    agent_version: str
    sequence: int
    sent_at: datetime
    samples: list[MetricSample]
    processes: ProcessSnapshot | None = None
    # Phase-1 additions (all optional on the wire for backward compatibility)
    batch_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    collected_at: datetime | None = None
    replay: bool = False  # True when uploaded from the offline queue after the fact
    priority: Priority = Priority.NORMAL  # highest priority of the batch content
    events: list[DeviceEvent] = Field(default_factory=list)
    device_health: DeviceHealth | None = None
    agent_health: AgentHealth | None = None


class InventoryEnvelope(BaseModel):
    schema_version: str = SCHEMA_VERSION
    device_id: str
    agent_id: str | None = None  # stable per installation (AGENT_ID label, else a generated instance id)
    agent_version: str
    discovered_at: datetime
    inventory: dict[str, Any]
