"""Agent -> backend wire contract (mirror of ``agent/app/contracts.py``; kept in sync by tests)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MetricValue = float | int | bool | str | None

#: Wire contract versions this backend understands. A payload without ``schema_version`` is from a
#: Phase-1 agent (1.0). Anything else is rejected as ``unsupported_schema_version`` so the agent
#: pauses instead of retrying forever.
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0", "1.1", "1.2"})
CURRENT_SCHEMA_VERSION = "1.2"
CATEGORY_PATTERN = r"^(performance|hardware|os|applications|security|agent)$"
PRIORITY_PATTERN = r"^(critical|high|normal|low)$"


def _check_schema_version(v: str) -> str:
    if v not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported_schema_version: {v}")
    return v


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
    MEASURED = "measured"
    DERIVED = "derived"
    STATIC = "static"


class MetricSampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(min_length=1, max_length=96, pattern=r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
    component: str = Field(min_length=1, max_length=48)
    value: MetricValue
    unit: str = Field(max_length=32)
    timestamp: datetime
    source: str = Field(max_length=200)
    quality: Quality
    availability: Availability
    kind: MetricKind = MetricKind.MEASURED
    reason: str | None = Field(default=None, max_length=1000)
    labels: dict[str, str] = Field(default_factory=dict, max_length=8)
    category: str | None = Field(default=None, pattern=CATEGORY_PATTERN)
    # 1.2: effective collection interval of the producing collector (drives per-metric freshness)
    interval_ms: int | None = Field(default=None, ge=0, le=86_400_000)


class ProcessInfoIn(BaseModel):
    pid: int = Field(ge=0)
    name: str = Field(max_length=260)
    status: str = Field(max_length=32)
    cpu_percent: float | None
    memory_rss_bytes: int | None
    memory_percent: float | None
    num_threads: int | None
    gpu_percent: float | None = None
    io_read_bytes_per_sec: float | None
    io_write_bytes_per_sec: float | None
    handle_count: int | None = None
    started_at: datetime | None = None
    tcp_established: int | None = None
    tcp_listening: int | None = None
    udp_endpoints: int | None = None
    path: str | None = Field(default=None, max_length=1024)
    user: str | None = Field(default=None, max_length=256)
    publisher: str | None = Field(default=None, max_length=256)


class ProcessSnapshotIn(BaseModel):
    timestamp: datetime
    source: str
    total_processes: int
    processes: list[ProcessInfoIn] = Field(max_length=500)
    unavailable_fields: dict[str, str] = Field(default_factory=dict)
    details_collected: bool = False


EventValue = str | int | float | bool | None


class DeviceEventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=64)
    type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    severity: str = Field(default="info", pattern=r"^(info|warning|error|critical)$")
    timestamp: datetime
    source: str = Field(max_length=200)
    message: str = Field(max_length=1000)
    data: dict[str, EventValue] = Field(default_factory=dict, max_length=32)
    priority: str | None = Field(default=None, pattern=PRIORITY_PATTERN)
    category: str | None = Field(default=None, pattern=CATEGORY_PATTERN)


class DeviceHealthIn(BaseModel):
    state: str = Field(pattern=r"^(HEALTHY|WARNING|CRITICAL|UNKNOWN)$")
    reasons: list[str] = Field(default_factory=list, max_length=32)
    checks: dict[str, str] = Field(default_factory=dict, max_length=32)
    evaluated_at: datetime


class CollectorHealthIn(BaseModel):
    name: str = Field(max_length=64)
    lane: str = Field(max_length=32)
    interval_ms: int
    last_success_at: datetime | None = None
    last_error: str | None = Field(default=None, max_length=400)
    consecutive_failures: int = 0
    total_failures: int = 0
    last_duration_ms: float | None = None


class AgentHealthIn(BaseModel):
    agent_version: str = Field(max_length=32)
    run_mode: str = Field(max_length=16)
    started_at: datetime
    uptime_s: float
    cpu_percent: float | None = None
    memory_rss_bytes: int | None = None
    queue_depth: int = 0
    queue_bytes: int = 0
    queue_dropped_total: int = 0
    last_collection_at: datetime | None = None
    last_sync_at: datetime | None = None
    sync_failures_consecutive: int = 0
    sync_failures_total: int = 0
    lanes_replaced_total: int = 0
    collectors: list[CollectorHealthIn] = Field(default_factory=list, max_length=64)
    api_latency_ms: float | None = None
    batches_created_total: int = 0
    batches_uploaded_total: int = 0
    upload_failures_total: int = 0
    events_generated_total: int = 0
    queue_thinned_total: int = 0
    last_sequence: int = 0


class TelemetryBatchIn(BaseModel):
    schema_version: str = Field(default="1.0", max_length=8)
    device_id: str = Field(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    agent_version: str = Field(max_length=32)
    sequence: int = Field(ge=0)
    sent_at: datetime
    samples: list[MetricSampleIn] = Field(max_length=5000)
    processes: ProcessSnapshotIn | None = None
    # Phase-1 additions (optional for backward compatibility with older agents)
    batch_id: str | None = Field(default=None, max_length=64)
    collected_at: datetime | None = None
    replay: bool = False
    events: list[DeviceEventIn] = Field(default_factory=list, max_length=500)
    device_health: DeviceHealthIn | None = None
    agent_health: AgentHealthIn | None = None
    priority: str = Field(default="normal", pattern=PRIORITY_PATTERN)

    _schema = field_validator("schema_version")(_check_schema_version)


class BulkBatchesIn(BaseModel):
    batches: list[TelemetryBatchIn] = Field(min_length=1, max_length=500)


class BatchResultOut(BaseModel):
    batch_id: str
    status: str  # accepted | duplicate | rejected
    detail: str | None = None


class BulkAck(BaseModel):
    """Summary counts plus per-batch outcomes. The agent deletes accepted and duplicate batches from
    its outbox, dead-letters rejected ones and keeps everything else for retry."""

    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    last_sequence: int | None = None
    server_received_at: datetime | None = None
    results: list[BatchResultOut]
    # Phase 10: accepted batches are confirmed durable later (heartbeat); agents >= 1.8 keep them until then
    durable_confirmation: bool = False


class RegisterIn(BaseModel):
    device_id: str = Field(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    agent_version: str = Field(max_length=32)


class RegisterOut(BaseModel):
    device_id: str
    device_token: str


class InventoryEnvelopeIn(BaseModel):
    schema_version: str = Field(default="1.0", max_length=8)
    device_id: str = Field(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    agent_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    agent_version: str = Field(max_length=32)
    discovered_at: datetime
    inventory: dict[str, Any]

    _schema = field_validator("schema_version")(_check_schema_version)


class IngestAck(BaseModel):
    accepted: int
    device_id: str
    sequence: int
    duplicate: bool = False
    server_received_at: datetime | None = None


ActionId = Annotated[str, Field(max_length=48, pattern=r"^[A-Z_]+$")]
ApplicationId = Annotated[str, Field(max_length=64, pattern=r"^[a-z0-9.-]+$")]


class HeartbeatIn(BaseModel):
    """Agent liveness ping (every ~30 s, also while the telemetry queue is backed up)."""

    schema_version: str = Field(default="1.1", max_length=8)
    device_id: str = Field(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    agent_version: str = Field(max_length=32)
    sent_at: datetime
    agent_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    run_mode: str | None = Field(default=None, max_length=16)
    last_sequence: int | None = Field(default=None, ge=0)
    queue_depth: int | None = Field(default=None, ge=0)
    queue_oldest_age_s: float | None = None
    sync_online: bool | None = None
    api_latency_ms: float | None = None
    collectors_failing: int | None = Field(default=None, ge=0)
    # Phase 8: the device's local remediation allowlist (the platform never dispatches beyond it)
    remediation_actions: list[ActionId] | None = Field(default=None, max_length=16)
    restartable_applications: list[ApplicationId] | None = Field(default=None, max_length=32)
    # Phase 10: accepted batches the agent still holds, asking whether they are durable
    unconfirmed_batch_ids: list[Annotated[str, Field(max_length=64)]] | None = Field(
        default=None, max_length=500
    )

    _schema = field_validator("schema_version")(_check_schema_version)


class HeartbeatOut(BaseModel):
    server_time: datetime
    presence: str
    last_sequence_received: int | None
    clock_offset_s: float  # server_time - agent sent_at (includes transit)
    credential_expires_at: datetime | None = None  # Phase 9: the agent rotates its credential before this
    # Phase 10 durable confirmation (answers unconfirmed_batch_ids): written / still in memory / unknown
    durable_batch_ids: list[str] | None = None
    pending_batch_ids: list[str] | None = None
    unknown_batch_ids: list[str] | None = None
