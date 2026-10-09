"""Alert engine: events -> alerts (deduplication, persistence, cooldown, correlation, escalation, expiry).

Pure and deterministic (the caller passes ``now``). Output is a list of ``AlertChange`` telling the
service what happened and whether recipients should be notified.

Deduplication key = tenant + device + source + alert type + condition (+ threshold): while an alert
with that key is open, further events update it instead of creating another one.

Hysteresis / material change: the sources already debounce (Phase 4 recovery bars and persistence,
Phase 5 trend gates and lifecycle, presence stale/offline thresholds). The alert resolves only when the
source closes the condition; updates notify only when the severity *rises* (never on every new value),
so a value oscillating around a limit produces one alert and one notification.

Cooldown: a recurrence of a recently resolved condition (same key within ``cooldown_s``) becomes a new
alert that is visible in-app but does not notify again, unless it is more severe than before.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.domain.alerting.models import Alert, AlertEvent, AlertStatus, sev_rank
from app.domain.alerting.policy import AlertPolicy

PRIORITY = {"CRITICAL": 1, "HIGH": 2, "MEDIUM": 3, "LOW": 4, "INFO": 5}


@dataclass(frozen=True, slots=True)
class AlertChange:
    kind: str  # created | updated | resolved | expired | escalated | acknowledged | suppressed | ignored
    alert: Alert | None
    notify: bool = False
    reason: str = ""
    event: AlertEvent | None = None
    escalation_roles: tuple[str, ...] = ()


@dataclass
class _Pending:
    event: AlertEvent
    since: datetime


@dataclass
class EngineState:
    open: dict[str, Alert] = field(default_factory=dict)  # dedupe key -> open alert
    pending: dict[str, _Pending] = field(default_factory=dict)  # waiting for persistence
    recent_closed: dict[str, tuple[datetime, str]] = field(
        default_factory=dict
    )  # key -> (closed at, severity)
    by_device: dict[str, set[str]] = field(default_factory=dict)  # device -> open dedupe keys (index)


def dedupe_key(tenant_id: str, e: AlertEvent) -> str:
    threshold = "" if e.threshold is None else f"{e.threshold:g}"
    raw = f"{tenant_id}|{e.device_id}|{e.source_type}|{e.alert_type}|{e.condition}|{threshold}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class AlertEngine:
    def __init__(
        self, policy: AlertPolicy, tenant_id: str = "default", id_factory: Callable[[], str] | None = None
    ):
        self.policy = policy
        self.tenant_id = tenant_id
        self._new_id = id_factory or (lambda: str(uuid.uuid4()))
        self.state = EngineState()
        self.stats = {
            "events": 0,
            "ignored": 0,
            "deduplicated": 0,
            "created": 0,
            "resolved": 0,
            "expired": 0,
            "escalated": 0,
            "cooldown_silenced": 0,
        }

    # -------------------------------------------------------------- restore
    def adopt(self, alert: Alert) -> None:
        if alert.is_open:
            self.state.open[alert.deduplication_key] = alert
            self.state.by_device.setdefault(alert.device_id, set()).add(alert.deduplication_key)

    def open_alerts(self) -> list[Alert]:
        return list(self.state.open.values())

    # --------------------------------------------------------------- events
    def ingest(self, e: AlertEvent, now: datetime) -> list[AlertChange]:
        self.stats["events"] += 1
        key = dedupe_key(self.tenant_id, e)
        alert = self.state.open.get(key)
        if e.kind == "closed":
            self.state.pending.pop(key, None)
            if alert is None:
                return []
            return [
                self._close(
                    key, alert, now, AlertStatus.RESOLVED, "system", e.close_reason or "condition ended"
                )
            ]
        if alert is not None:
            self.stats["deduplicated"] += 1
            return [self._update(alert, e, now)]
        rule = self.policy.rule_for(e.source_type, e.severity, e.confidence)
        if rule is None:
            self.stats["ignored"] += 1
            return [
                AlertChange(
                    "ignored",
                    None,
                    reason="no alert rule matches (severity/confidence below policy)",
                    event=e,
                )
            ]
        if rule.persistence_s > 0:
            p = self.state.pending.get(key)
            if p is None:
                self.state.pending[key] = _Pending(e, now)
                return []
            p.event = e
            if (now - p.since).total_seconds() < rule.persistence_s:
                return []
            since = p.since
            del self.state.pending[key]
            return [self._create(key, e, now, rule.group, since)]
        return [self._create(key, e, now, rule.group, now)]

    def _create(self, key: str, e: AlertEvent, now: datetime, group: bool, since: datetime) -> AlertChange:
        p = self.policy
        alert = Alert(
            alert_id=self._new_id(),
            tenant_id=self.tenant_id,
            device_id=e.device_id,
            event_id=e.source_id,
            source_type=e.source_type,
            alert_type=e.alert_type,
            category=e.category,
            severity=e.severity,
            title=e.title,
            summary=e.summary,
            status=AlertStatus.OPEN,
            priority=PRIORITY.get(e.severity, 5),
            confidence=e.confidence,
            deduplication_key=key,
            correlation_key=self._correlate(e, now),
            first_detected_at=min(since, e.occurred_at),
            last_updated_at=now,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=p.expire_after_s),
            metadata={
                "metric": e.metric,
                "threshold": e.threshold,
                "observed": e.observed,
                "expected": e.expected,
                "evidence": e.evidence,
                "grouped": group and e.severity != "CRITICAL",
            },
        )
        if e.severity in p.escalation_severities and p.escalation:
            alert.next_escalation_at = now + timedelta(seconds=p.escalation[0].after_s)
        alert.note(now, "system", "created", f"{e.source_type} {e.source_id}: {e.summary}")
        self.state.open[key] = alert
        self.state.by_device.setdefault(e.device_id, set()).add(key)
        self.stats["created"] += 1
        notify, reason = True, "new condition"
        prev = self.state.recent_closed.get(key)
        if (
            prev
            and (now - prev[0]).total_seconds() <= p.cooldown_s
            and sev_rank(e.severity) <= sev_rank(prev[1])
        ):
            notify, reason = (
                False,
                f"recurrence within the {p.cooldown_s:.0f} s cooldown: visible, not re-notified",
            )
            alert.metadata["recurrence"] = True
            self.stats["cooldown_silenced"] += 1
        return AlertChange("created", alert, notify, reason, e)

    def _update(self, alert: Alert, e: AlertEvent, now: datetime) -> AlertChange:
        escalated = sev_rank(e.severity) > sev_rank(alert.severity)
        alert.event_id = e.source_id
        alert.summary = e.summary
        alert.confidence = e.confidence if e.confidence is not None else alert.confidence
        alert.last_updated_at = now
        alert.occurrences += 1
        alert.expires_at = now + timedelta(seconds=self.policy.expire_after_s)
        alert.metadata.update({"observed": e.observed, "expected": e.expected, "evidence": e.evidence})
        if escalated:
            alert.note(now, "system", "severity_raised", f"{alert.severity} -> {e.severity}")
            alert.severity, alert.priority = e.severity, PRIORITY.get(e.severity, 5)
            if (
                e.severity in self.policy.escalation_severities
                and alert.next_escalation_at is None
                and self.policy.escalation
            ):
                alert.next_escalation_at = now + timedelta(seconds=self.policy.escalation[0].after_s)
        if alert.status is AlertStatus.OPEN:
            alert.transition(AlertStatus.ONGOING, now, "system", "ongoing", "condition persists")
        else:
            alert.updated_at = now
        held = (now - alert.first_detected_at).total_seconds()
        alert.metadata["duration_s"] = round(held)
        notify = escalated and alert.status is not AlertStatus.SUPPRESSED
        return AlertChange(
            "updated", alert, notify, "severity raised" if escalated else "same condition: updated", e
        )

    def _correlate(self, e: AlertEvent, now: datetime) -> str | None:
        """Open alerts of the same device and family within the window share a correlation key."""
        window = timedelta(seconds=self.policy.correlation_window_s)
        family = "performance" if e.category in ("anomaly", "prediction") else e.category
        for a in self._device_open(e.device_id):
            a_family = "performance" if a.category in ("anomaly", "prediction") else a.category
            if a.device_id == e.device_id and a_family == family and now - a.created_at <= window:
                if a.correlation_key is None:
                    a.correlation_key = f"{e.device_id}:{family}:{int(a.created_at.timestamp())}"
                return a.correlation_key
        return None

    def _device_open(self, device_id: str) -> list[Alert]:
        keys = self.state.by_device.get(device_id, set())
        return [self.state.open[k] for k in keys if k in self.state.open]

    # ---------------------------------------------------------- user actions
    def acknowledge(self, alert: Alert, actor: str, now: datetime, note: str | None = None) -> AlertChange:
        alert.transition(AlertStatus.ACKNOWLEDGED, now, actor, "acknowledged", note)
        alert.acknowledged_at, alert.acknowledged_by = now, actor
        alert.next_escalation_at = None  # acknowledged: escalation stops
        return AlertChange("acknowledged", alert)

    def resolve(self, alert: Alert, actor: str, now: datetime, note: str | None = None) -> AlertChange:
        return self._close(
            alert.deduplication_key, alert, now, AlertStatus.RESOLVED, actor, note or "resolved by user"
        )

    def suppress(
        self, alert: Alert, actor: str, now: datetime, until: datetime | None, note: str | None
    ) -> AlertChange:
        alert.transition(AlertStatus.SUPPRESSED, now, actor, "suppressed", note)
        alert.suppressed_at, alert.suppressed_by, alert.suppressed_until = now, actor, until
        alert.next_escalation_at = None
        return AlertChange("suppressed", alert)

    def _close(
        self, key: str, alert: Alert, now: datetime, to: AlertStatus, actor: str, why: str
    ) -> AlertChange:
        alert.transition(to, now, actor, to.value.lower(), why)
        if to is AlertStatus.RESOLVED:
            alert.resolved_at, alert.resolved_by = now, actor
            self.stats["resolved"] += 1
        else:
            self.stats["expired"] += 1
        alert.next_escalation_at = None
        self.state.open.pop(key, None)
        self.state.by_device.get(alert.device_id, set()).discard(key)
        self.state.recent_closed[key] = (now, alert.severity)
        notify = to is AlertStatus.RESOLVED and self.policy.notify_on_resolution
        return AlertChange("resolved" if to is AlertStatus.RESOLVED else "expired", alert, notify, why)

    # -------------------------------------------------------------- periodic
    def tick(self, now: datetime) -> list[AlertChange]:
        """Escalations, expiry, end of manual suppression windows, cooldown bookkeeping."""
        out: list[AlertChange] = []
        p = self.policy
        for key, alert in list(self.state.open.items()):
            if alert.expires_at and now >= alert.expires_at:
                out.append(
                    self._close(
                        key,
                        alert,
                        now,
                        AlertStatus.EXPIRED,
                        "system",
                        "no update from the source for too long",
                    )
                )
                continue
            if (
                alert.next_escalation_at
                and now >= alert.next_escalation_at
                and alert.status in (AlertStatus.OPEN, AlertStatus.ONGOING)
            ):
                level = alert.escalation_level
                if level < len(p.escalation):
                    step = p.escalation[level]
                    alert.escalation_level = level + 1
                    alert.note(
                        now,
                        "system",
                        "escalated",
                        f"level {level + 1}: not acknowledged after "
                        f"{step.after_s / 60:.0f} min -> {', '.join(step.roles)}",
                    )
                    nxt = p.escalation[level + 1].after_s if level + 1 < len(p.escalation) else None
                    alert.next_escalation_at = (
                        (alert.first_detected_at + timedelta(seconds=nxt)) if nxt else None
                    )
                    self.stats["escalated"] += 1
                    out.append(
                        AlertChange(
                            "escalated",
                            alert,
                            True,
                            f"escalation level {level + 1}",
                            escalation_roles=step.roles,
                        )
                    )
                else:
                    alert.next_escalation_at = None
        horizon = now - timedelta(seconds=max(p.cooldown_s, 60))
        for key, (at, _) in list(self.state.recent_closed.items()):
            if at < horizon:
                del self.state.recent_closed[key]
        return out
