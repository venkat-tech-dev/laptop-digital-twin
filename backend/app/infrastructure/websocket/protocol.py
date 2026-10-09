"""Maps domain events onto the WebSocket event vocabulary consumed by the frontend.

WebSocket event types: telemetry_update, component_state_changed, anomaly_detected, anomaly_resolved,
anomaly.detected, anomaly.updated, anomaly.resolved (Phase 4: every anomaly type, full record),
prediction.created / updated / invalidated / expired / confirmed / cancelled (Phase 5),
alert.* (device topic) and notification.* (only to the recipient's sessions) (Phase 6),
diagnosis.started / updated / available / failed / expired (Phase 7, device topic),
remediation.proposed / approval_required / approved / rejected / queued / started / progress / verifying /
succeeded / failed / cancelled / expired / rolled_back / circuit_open (Phase 8, device topic),
health_changed, device_status_changed, device_presence_changed, connection_status, system_event,
twin_snapshot, subscribed, unsubscribed, subscription_error, heartbeat, pong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.events.events import (
    AlertChanged,
    AnomalyChanged,
    AnomalyDetected,
    AnomalyResolved,
    ComponentUpdated,
    DeviceStatusChanged,
    DiagnosisChanged,
    DomainEvent,
    HealthChanged,
    NotificationChanged,
    PredictionChanged,
    PresenceChanged,
    RemediationChanged,
    SensorUnavailable,
    SystemEvent,
    TelemetryReceived,
    TwinMessage,
)

PROTOCOL_VERSION = 1


def ws_event_type(event: DomainEvent) -> str:
    if isinstance(event, TwinMessage):
        return event.kind
    if isinstance(event, TelemetryReceived):
        return "telemetry_update"
    if isinstance(event, ComponentUpdated):
        return "component_state_changed"
    if isinstance(event, HealthChanged):
        return "health_changed"
    if isinstance(event, AlertChanged):
        return f"alert.{event.kind}"
    if isinstance(event, NotificationChanged):
        return f"notification.{event.kind}"
    if isinstance(event, PredictionChanged):
        return f"prediction.{event.kind}"
    if isinstance(event, DiagnosisChanged):
        return f"diagnosis.{event.kind}"
    if isinstance(event, RemediationChanged):
        return f"remediation.{event.kind}"
    if isinstance(event, AnomalyChanged):
        return f"anomaly.{event.kind}"
    if isinstance(event, AnomalyDetected):
        return "anomaly_detected"
    if isinstance(event, AnomalyResolved):
        return "anomaly_resolved"
    if isinstance(event, DeviceStatusChanged):
        return "device_status_changed"
    if isinstance(event, PresenceChanged):
        return "device_presence_changed"
    if isinstance(event, (SensorUnavailable, SystemEvent)):
        return "system_event"
    return "system_event"


RESERVED_KEYS = frozenset({"event", "domain_event", "timestamp", "device_id", "mode"})


def to_message(event: DomainEvent) -> dict[str, Any]:
    payload = event.payload()
    clash = RESERVED_KEYS & payload.keys()
    if clash:  # a payload must never overwrite the envelope (e.g. the event name)
        raise ValueError(f"{event.name} payload uses reserved keys: {sorted(clash)}")
    if isinstance(event, SensorUnavailable):
        payload = {
            "event_type": "sensor_recovered" if event.available else "sensor_unavailable",
            "severity": "info",
            "message": f"{event.metric_key} {'available again' if event.available else 'unavailable'}"
            + (f": {event.reason}" if event.reason and not event.available else ""),
            "data": payload,
        }
    return {
        "event": ws_event_type(event),
        "domain_event": event.name,
        "timestamp": event.occurred_at.isoformat(),
        "device_id": event.device_id,
        "mode": "live",
        **payload,
    }


def envelope(event: str, **fields: Any) -> dict[str, Any]:
    return {
        "event": event,
        "timestamp": datetime.now(UTC).isoformat(),
        "protocol": PROTOCOL_VERSION,
        **fields,
    }
