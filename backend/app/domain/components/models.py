"""Digital twin component model (the physical hierarchy of the laptop)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.domain.telemetry.models import MetricReading


class ComponentType(StrEnum):
    LAPTOP = "laptop"
    CHASSIS = "chassis"
    DISPLAY = "display"
    MOTHERBOARD = "motherboard"
    CPU = "cpu"
    GPU = "gpu"
    MEMORY = "memory"
    VRM = "vrm"
    STORAGE = "storage"
    DISK = "disk"
    BATTERY = "battery"
    COOLING = "cooling"
    FAN = "fan"
    THERMAL_SENSORS = "thermal_sensors"
    NETWORK = "network"
    NETWORK_ADAPTER = "network_adapter"
    POWER = "power"
    OPERATING_SYSTEM = "operating_system"
    AGENT = "telemetry_agent"


class Availability(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    NO_TELEMETRY = "no_telemetry"  # structural part with no sensors (chassis, hinge...)


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class HealthReason:
    severity: str  # ok | info | warning | critical
    message: str
    impact: int = 0  # points deducted (<= 0)
    metric: str | None = None
    value: float | str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "message": self.message,
            "impact": self.impact,
            "metric": self.metric,
            "value": self.value,
        }


@dataclass(slots=True)
class ComponentHealth:
    score: int | None
    status: HealthStatus
    reasons: list[HealthReason] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "status": self.status.value,
            "reasons": [r.to_dict() for r in self.reasons],
        }


@dataclass(slots=True)
class Component:
    component_id: str
    component_type: ComponentType
    name: str
    parent_id: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    current_state: str = "unknown"
    health: ComponentHealth = field(default_factory=lambda: ComponentHealth(None, HealthStatus.UNKNOWN))
    telemetry: dict[str, MetricReading] = field(default_factory=dict)
    last_updated: datetime | None = None
    availability: Availability = Availability.NO_TELEMETRY

    def reading(self, metric: str) -> MetricReading | None:
        """Unlabelled reading for ``metric`` (or the first labelled one if no aggregate exists)."""
        direct = self.telemetry.get(metric)
        if direct is not None:
            return direct
        for r in self.telemetry.values():
            if r.metric == metric:
                return r
        return None

    def value(self, metric: str) -> float | None:
        r = self.reading(metric)
        return r.numeric if r is not None else None

    def readings(self, metric: str) -> list[MetricReading]:
        return [r for r in self.telemetry.values() if r.metric == metric]

    def recompute_availability(self) -> None:
        if not self.telemetry:
            self.availability = Availability.NO_TELEMETRY
            return
        avail = [r.available for r in self.telemetry.values()]
        if all(avail):
            self.availability = Availability.AVAILABLE
        elif any(avail):
            self.availability = Availability.PARTIAL
        else:
            self.availability = Availability.UNAVAILABLE
