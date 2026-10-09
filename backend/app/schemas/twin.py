"""Response schemas for the REST API (never ORM objects)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

MetricValue = float | int | bool | str | None


class MetricReadingOut(BaseModel):
    key: str
    metric: str
    value: MetricValue
    unit: str
    timestamp: datetime
    source: str
    quality: str
    availability: str
    kind: str
    reason: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class HealthReasonOut(BaseModel):
    severity: str
    message: str
    impact: int
    metric: str | None = None
    value: float | str | None = None


class ComponentHealthOut(BaseModel):
    score: int | None
    status: str
    reasons: list[HealthReasonOut]


class ComponentOut(BaseModel):
    component_id: str
    component_type: str
    name: str
    parent_id: str | None
    manufacturer: str | None
    model: str | None
    properties: dict[str, Any]
    current_state: str
    health: ComponentHealthOut
    availability: str
    last_updated: datetime | None
    telemetry: dict[str, MetricReadingOut] = Field(default_factory=dict)


class ThermalSummaryOut(BaseModel):
    cpu_area_temperature_c: float | None
    sensor: str | None
    metric_key: str | None
    source: str | None
    band: str
    is_cpu_package_sensor: bool


class AnomalyOut(BaseModel):
    anomaly_id: str
    device_id: str
    detector: str
    rule_id: str
    component_id: str
    metric_key: str
    severity: str
    title: str
    message: str
    value: float | str | None
    threshold: float | str | None
    started_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    status: str
    context: dict[str, Any]
    acknowledgement: dict[str, Any] | None = None
    confidence: dict[str, Any] | None = None


class ProcessOut(BaseModel):
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
    path: str | None = None
    user: str | None = None
    publisher: str | None = None


class ProcessListOut(BaseModel):
    timestamp: datetime | None
    source: str | None
    total_processes: int
    sort_by: str
    unavailable_fields: dict[str, str]
    details_collected: bool = False
    processes: list[ProcessOut]


class HealthOverviewOut(BaseModel):
    overall: ComponentHealthOut
    components: dict[str, ComponentHealthOut]


class TwinOut(BaseModel):
    device_id: str
    device_status: str
    last_seen: datetime | None
    generated_at: datetime
    mode: str
    data_source: str
    components: list[ComponentOut]
    health: HealthOverviewOut
    thermal: ThermalSummaryOut
    active_anomalies: list[AnomalyOut]
    processes: dict[str, Any] | None


class GeometryOut(BaseModel):
    kind: str
    label: str
    url: str | None
    detected: str
    note: str | None = None
    attribution: str | None = None
    panel: dict[str, Any] | None = None
    resolution: str | None = None
    profile: dict[str, Any] | None = None
    photo_url: str | None = None
    photo_attribution: str | None = None


class DeviceOut(BaseModel):
    device_id: str
    manufacturer: str | None
    model: str | None
    model_number: str | None
    os_name: str | None
    status: str
    last_seen: datetime | None
    agent_version: str
    data_source: str
    mode: str
    telemetry: str
    sensor_provider: str | None
    geometry: GeometryOut


class HealthEventOut(BaseModel):
    component_id: str
    time: datetime
    previous_score: int | None
    score: int | None
    previous_status: str
    status: str
    reasons: list[dict[str, Any]]


class SystemEventOut(BaseModel):
    time: datetime
    event_type: str
    severity: str
    message: str
    data: dict[str, Any]


class HistoryPointOut(BaseModel):
    t: datetime
    avg: float
    min: float
    max: float
    n: int


class HistoryOut(BaseModel):
    device_id: str
    start: datetime
    end: datetime
    bucket_seconds: int
    series: dict[str, list[HistoryPointOut]]


class RecentOut(BaseModel):
    device_id: str
    seconds: int
    points: list[tuple[int, dict[str, float]]]


class MetricDefOut(BaseModel):
    key: str
    metric: str
    component_id: str
    unit: str
    source: str
    kind: str
    labels: dict[str, str]
