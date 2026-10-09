"""EVENT -> ALERT -> NOTIFICATION, three separate concepts.

* ``AlertEvent``   something was detected (Phase 4 anomaly, Phase 5 prediction, connectivity, device
                   health / security posture). Produced by adapters from existing domain events.
* ``Alert``        the system decided the condition needs attention. One alert per ongoing condition
                   (deduplication key); its state machine is validated and every transition audited.
* ``Notification`` one message to one user on one channel. One alert -> many notifications.

State machines (anything else is rejected with ``InvalidTransitionError``):

    Alert:         OPEN -> ONGOING -> ACKNOWLEDGED -> RESOLVED
                   OPEN/ONGOING/ACKNOWLEDGED -> RESOLVED | SUPPRESSED | EXPIRED
                   SUPPRESSED -> RESOLVED | EXPIRED (the condition still ends)
    Notification:  PENDING -> QUEUED -> SENDING -> DELIVERED -> READ
                   SENDING -> RETRYING -> SENDING ... -> FAILED (dead letter); SENDING -> EXPIRED (too old)
                   PENDING/QUEUED/RETRYING -> CANCELLED | EXPIRED;  DELIVERED -> READ
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
CHANNEL_NAMES = ("in_app", "browser", "windows", "email", "webhook")


def sev_rank(s: str | None) -> int:
    return SEVERITIES.index(s) if s in SEVERITIES else -1


class AlertStatus(StrEnum):
    OPEN = "OPEN"
    ONGOING = "ONGOING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    SUPPRESSED = "SUPPRESSED"
    EXPIRED = "EXPIRED"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


ALERT_TRANSITIONS: dict[AlertStatus, frozenset[AlertStatus]] = {
    AlertStatus.OPEN: frozenset(
        {
            AlertStatus.ONGOING,
            AlertStatus.ACKNOWLEDGED,
            AlertStatus.RESOLVED,
            AlertStatus.SUPPRESSED,
            AlertStatus.EXPIRED,
        }
    ),
    AlertStatus.ONGOING: frozenset(
        {AlertStatus.ACKNOWLEDGED, AlertStatus.RESOLVED, AlertStatus.SUPPRESSED, AlertStatus.EXPIRED}
    ),
    AlertStatus.ACKNOWLEDGED: frozenset({AlertStatus.RESOLVED, AlertStatus.SUPPRESSED, AlertStatus.EXPIRED}),
    AlertStatus.SUPPRESSED: frozenset({AlertStatus.RESOLVED, AlertStatus.EXPIRED}),
    AlertStatus.RESOLVED: frozenset(),
    AlertStatus.EXPIRED: frozenset(),
}
OPEN_STATES = frozenset(
    {AlertStatus.OPEN, AlertStatus.ONGOING, AlertStatus.ACKNOWLEDGED, AlertStatus.SUPPRESSED}
)

N = NotificationStatus
NOTIFICATION_TRANSITIONS: dict[NotificationStatus, frozenset[NotificationStatus]] = {
    N.PENDING: frozenset({N.QUEUED, N.SENDING, N.DELIVERED, N.CANCELLED, N.EXPIRED}),
    N.QUEUED: frozenset({N.SENDING, N.CANCELLED, N.EXPIRED}),
    N.SENDING: frozenset({N.DELIVERED, N.RETRYING, N.FAILED, N.QUEUED, N.EXPIRED, N.CANCELLED}),
    N.RETRYING: frozenset({N.SENDING, N.CANCELLED, N.EXPIRED, N.FAILED}),
    N.DELIVERED: frozenset({N.READ}),
    N.READ: frozenset(),
    N.FAILED: frozenset(),
    N.CANCELLED: frozenset(),
    N.EXPIRED: frozenset(),
}


class InvalidTransitionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """Normalised input from an event source (validated before it reaches the policy)."""

    source_type: str  # anomaly | prediction | connectivity | device_health | security
    source_id: str  # anomaly_id / prediction_id / device event id
    device_id: str
    kind: str  # opened | updated | closed
    alert_type: str  # e.g. behavioral_anomaly, resource_exhaustion, device_offline
    category: str  # anomaly | prediction | connectivity | security | system
    severity: str  # INFO..CRITICAL
    confidence: float | None
    title: str
    summary: str
    condition: str  # stable condition id (rule_id, target id ...) for the deduplication key
    occurred_at: datetime
    metric: str | None = None
    threshold: float | None = None
    observed: float | None = None
    expected: float | None = None
    close_reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AuditEntry:
    at: datetime
    actor: str  # "system" or a username
    action: str  # created | updated | acknowledged | resolved | suppressed | expired | escalated | ...
    from_status: str | None
    to_status: str | None
    detail: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "actor": self.actor,
            "action": self.action,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "detail": self.detail,
        }


@dataclass(slots=True)
class Alert:
    alert_id: str
    tenant_id: str
    device_id: str
    event_id: str  # latest triggering source id
    source_type: str
    alert_type: str
    category: str
    severity: str
    title: str
    summary: str
    status: AlertStatus
    priority: int  # 1 (highest) .. 5
    confidence: float | None
    deduplication_key: str
    correlation_key: str | None
    first_detected_at: datetime
    last_updated_at: datetime
    created_at: datetime
    updated_at: datetime
    description: str | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    suppressed_at: datetime | None = None
    suppressed_by: str | None = None
    suppressed_until: datetime | None = None
    expires_at: datetime | None = None
    escalation_level: int = 0
    next_escalation_at: datetime | None = None
    occurrences: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    audit: list[AuditEntry] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATES

    def transition(
        self, to: AlertStatus, at: datetime, actor: str, action: str, detail: str | None = None
    ) -> None:
        if to != self.status and to not in ALERT_TRANSITIONS[self.status]:
            raise InvalidTransitionError(f"alert {self.status.value} -> {to.value} is not allowed")
        self.audit.append(AuditEntry(at, actor, action, self.status.value, to.value, detail))
        self.status = to
        self.updated_at = at

    def note(self, at: datetime, actor: str, action: str, detail: str | None = None) -> None:
        self.audit.append(AuditEntry(at, actor, action, self.status.value, self.status.value, detail))
        self.updated_at = at

    def to_dict(self, with_audit: bool = False) -> dict[str, Any]:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        out: dict[str, Any] = {
            "alert_id": self.alert_id,
            "tenant_id": self.tenant_id,
            "device_id": self.device_id,
            "event_id": self.event_id,
            "source_type": self.source_type,
            "alert_type": self.alert_type,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "summary": self.summary,
            "description": self.description,
            "status": self.status.value,
            "priority": self.priority,
            "confidence": self.confidence,
            "deduplication_key": self.deduplication_key,
            "correlation_key": self.correlation_key,
            "first_detected_at": iso(self.first_detected_at),
            "last_updated_at": iso(self.last_updated_at),
            "acknowledged_at": iso(self.acknowledged_at),
            "acknowledged_by": self.acknowledged_by,
            "resolved_at": iso(self.resolved_at),
            "resolved_by": self.resolved_by,
            "suppressed_at": iso(self.suppressed_at),
            "suppressed_by": self.suppressed_by,
            "suppressed_until": iso(self.suppressed_until),
            "expires_at": iso(self.expires_at),
            "escalation_level": self.escalation_level,
            "next_escalation_at": iso(self.next_escalation_at),
            "occurrences": self.occurrences,
            "metadata": self.metadata,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }
        if with_audit:
            out["audit"] = [e.public() for e in self.audit]
        return out


@dataclass(slots=True)
class Notification:
    notification_id: str
    tenant_id: str
    alert_id: str | None  # None for digests
    user_id: str
    device_id: str | None
    channel: str  # in_app | browser | windows | email | webhook
    status: NotificationStatus
    priority: int
    severity: str
    category: str
    title: str
    body: str
    payload: dict[str, Any]
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    provider: str | None = None
    provider_message_id: str | None = None
    attempt_count: int = 0
    next_retry_at: datetime | None = None
    deliver_after: datetime | None = None  # quiet hours / digests: held until then (never dropped)
    delivered_at: datetime | None = None
    read_at: datetime | None = None
    failed_at: datetime | None = None
    failure_reason: str | None = None
    last_error: str | None = None
    escalation_level: int = 0
    history: list[AuditEntry] = field(default_factory=list)

    def transition(self, to: NotificationStatus, at: datetime, detail: str | None = None) -> None:
        if to != self.status and to not in NOTIFICATION_TRANSITIONS[self.status]:
            raise InvalidTransitionError(f"notification {self.status.value} -> {to.value} is not allowed")
        self.history.append(AuditEntry(at, "system", "status", self.status.value, to.value, detail))
        self.status = to
        self.updated_at = at

    def to_dict(self) -> dict[str, Any]:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        return {
            "notification_id": self.notification_id,
            "tenant_id": self.tenant_id,
            "alert_id": self.alert_id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "channel": self.channel,
            "status": self.status.value,
            "priority": self.priority,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "body": self.body,
            "payload": self.payload,
            "provider": self.provider,
            "provider_message_id": self.provider_message_id,
            "attempt_count": self.attempt_count,
            "next_retry_at": iso(self.next_retry_at),
            "deliver_after": iso(self.deliver_after),
            "delivered_at": iso(self.delivered_at),
            "read_at": iso(self.read_at),
            "failed_at": iso(self.failed_at),
            "failure_reason": self.failure_reason,
            "escalation_level": self.escalation_level,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "history": [h.public() for h in self.history[-20:]],
        }
