"""Digital twin domain events.

Domain events are transport-agnostic. ``app.infrastructure.websocket.protocol`` maps them onto the
WebSocket event types consumed by the UI, so the agent, the twin engine and the UI stay decoupled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    name: ClassVar[str] = "domain_event"
    device_id: str
    occurred_at: datetime = field(default_factory=_now)

    def payload(self) -> dict[str, Any]:
        return {}


@dataclass(frozen=True, kw_only=True)
class TelemetryReceived(DomainEvent):
    name: ClassVar[str] = "TelemetryReceived"
    sequence: int
    components: dict[str, Any]
    device_status: str
    processes: dict[str, Any] | None = None
    # Latency instrumentation: collected_at (device clock), sent_at, server_received_at, published_at.
    timing: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sequence": self.sequence,
            "components": self.components,
            "device_status": self.device_status,
        }
        if self.processes is not None:
            out["processes"] = self.processes
        if self.timing is not None:
            out["timing"] = self.timing
        return out


@dataclass(frozen=True, kw_only=True)
class ComponentUpdated(DomainEvent):
    name: ClassVar[str] = "ComponentUpdated"
    component_id: str
    previous_state: str
    current_state: str

    def payload(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "previous_state": self.previous_state,
            "current_state": self.current_state,
        }


@dataclass(frozen=True, kw_only=True)
class HealthChanged(DomainEvent):
    name: ClassVar[str] = "HealthChanged"
    component_id: str
    previous_score: int | None
    score: int | None
    previous_status: str
    status: str
    reasons: list[dict[str, Any]]

    def payload(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "previous_score": self.previous_score,
            "score": self.score,
            "previous_status": self.previous_status,
            "status": self.status,
            "reasons": self.reasons,
        }


@dataclass(frozen=True, kw_only=True)
class AnomalyDetected(DomainEvent):
    name: ClassVar[str] = "AnomalyDetected"
    anomaly: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {"anomaly": self.anomaly}


@dataclass(frozen=True, kw_only=True)
class AnomalyResolved(DomainEvent):
    name: ClassVar[str] = "AnomalyResolved"
    anomaly: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {"anomaly": self.anomaly}


@dataclass(frozen=True, kw_only=True)
class AnomalyChanged(DomainEvent):
    """Phase-4 lifecycle event (any anomaly type): kind = detected | updated | resolved."""

    name: ClassVar[str] = "AnomalyChanged"
    kind: str
    anomaly: dict[str, Any]
    changed: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {"anomaly": self.anomaly, "changed": list(self.changed)}


@dataclass(frozen=True, kw_only=True)
class PredictionChanged(DomainEvent):
    """Phase-5 forecast lifecycle: kind = created | updated | invalidated | expired | confirmed | cancelled"""

    name: ClassVar[str] = "PredictionChanged"
    kind: str
    prediction: dict[str, Any]
    changed: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {"prediction": self.prediction, "changed": list(self.changed)}


@dataclass(frozen=True, kw_only=True)
class AlertChanged(DomainEvent):
    """Phase-6 alert lifecycle: created, updated, acknowledged, resolved, suppressed, expired, escalated."""

    name: ClassVar[str] = "AlertChanged"
    kind: str
    alert: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {"alert": self.alert}


@dataclass(frozen=True, kw_only=True)
class RemediationChanged(DomainEvent):
    """Phase-8 remediation lifecycle (proposed, approval_required, approved, rejected, queued, started,
    progress, verifying, succeeded, failed, cancelled, expired, rolled_back, circuit_open); device topic."""

    name: ClassVar[str] = "RemediationChanged"
    kind: str
    remediation: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {"remediation": self.remediation}


@dataclass(frozen=True, kw_only=True)
class DiagnosisChanged(DomainEvent):
    """Phase-7 diagnosis lifecycle: started | updated | available | failed | expired (device topic)."""

    name: ClassVar[str] = "DiagnosisChanged"
    kind: str
    diagnosis: dict[str, Any]  # summary form (no evidence body); clients fetch details over REST

    def payload(self) -> dict[str, Any]:
        return {"diagnosis": self.diagnosis}


@dataclass(frozen=True, kw_only=True)
class NotificationChanged(DomainEvent):
    """Phase-6 notification for one user (created | updated | read); delivered only to ``recipient``."""

    name: ClassVar[str] = "NotificationChanged"
    kind: str
    notification: dict[str, Any]
    recipient: str

    def payload(self) -> dict[str, Any]:
        return {"notification": self.notification, "recipient": self.recipient}


@dataclass(frozen=True, kw_only=True)
class SensorUnavailable(DomainEvent):
    name: ClassVar[str] = "SensorUnavailable"
    metric_key: str
    component_id: str
    reason: str | None
    available: bool  # False -> became unavailable; True -> recovered

    def payload(self) -> dict[str, Any]:
        return {
            "metric_key": self.metric_key,
            "component_id": self.component_id,
            "reason": self.reason,
            "available": self.available,
        }


@dataclass(frozen=True, kw_only=True)
class DeviceStatusChanged(DomainEvent):
    name: ClassVar[str] = "DeviceStatusChanged"
    previous_status: str
    status: str
    last_seen_at: datetime | None

    def payload(self) -> dict[str, Any]:
        return {
            "previous_status": self.previous_status,
            "status": self.status,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }


@dataclass(frozen=True, kw_only=True)
class DeviceOnline(DeviceStatusChanged):
    name: ClassVar[str] = "DeviceOnline"


@dataclass(frozen=True, kw_only=True)
class DeviceOffline(DeviceStatusChanged):
    name: ClassVar[str] = "DeviceOffline"


@dataclass(frozen=True, kw_only=True)
class BatteryStateChanged(ComponentUpdated):
    name: ClassVar[str] = "BatteryStateChanged"


@dataclass(frozen=True, kw_only=True)
class ThermalStateChanged(ComponentUpdated):
    name: ClassVar[str] = "ThermalStateChanged"


@dataclass(frozen=True, kw_only=True)
class SystemEvent(DomainEvent):
    name: ClassVar[str] = "SystemEvent"
    event_type: str
    severity: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "severity": self.severity,
            "message": self.message,
            "data": self.data,
        }


@dataclass(frozen=True, kw_only=True)
class PresenceChanged(DomainEvent):
    """Agent presence transition (ONLINE / STALE / OFFLINE / UNKNOWN), driven by agent heartbeats."""

    name: ClassVar[str] = "PresenceChanged"
    previous_presence: str
    presence: str
    last_contact_at: datetime | None

    def payload(self) -> dict[str, Any]:
        return {
            "previous_presence": self.previous_presence,
            "presence": self.presence,
            "last_contact_at": self.last_contact_at.isoformat() if self.last_contact_at else None,
        }


@dataclass(frozen=True, kw_only=True)
class TwinMessage(DomainEvent):
    """A message produced by the twin state engine; ``kind`` is the WebSocket event type
    (``twin.state.patch``, ``twin.status.changed``, ``twin.event.created``, ``twin.summary``,
    ``twin.sync.required``)."""

    name: ClassVar[str] = "TwinMessage"
    kind: str
    body: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return self.body
