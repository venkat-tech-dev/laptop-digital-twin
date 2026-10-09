"""AlertService (Phase 6): events -> alerts -> notifications -> providers, with audit and metrics.

    EventBus (AnomalyChanged, PredictionChanged, PresenceChanged, twin health events)
      -> adapters (validation) -> AlertEngine (policy, dedupe, persistence, cooldown, correlation)
      -> alerts table (+ audit) + alert.* WebSocket events + twin timeline entries
      -> routing (recipients, preferences, quiet hours, grouping, fatigue guard)
      -> notifications table = durable queue  -> delivery worker -> providers -> retry / dead letter
      -> notification.* WebSocket events to the recipient only

Provider calls never run in an API request: the worker loop claims due notifications (SKIP LOCKED),
delivers them with bounded concurrency and per-provider timeouts, and schedules retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.core.config import Settings
from app.core.metrics import (
    ALERT_EVENTS_DROPPED,
    ALERTS_TOTAL,
    NOTIFICATION_BACKLOG,
    NOTIFICATION_OLDEST_DUE,
    NOTIFICATION_RETRIES,
    NOTIFICATIONS_REQUEUED,
    NOTIFICATIONS_TOTAL,
    PROVIDER_LATENCY,
)
from app.core.supervisor import watch
from app.domain.alerting.delivery import DeliveryResult, NotificationProvider, next_retry
from app.domain.alerting.engine import AlertChange, AlertEngine
from app.domain.alerting.models import (
    Alert,
    AlertEvent,
    AlertStatus,
    Notification,
    NotificationStatus,
    sev_rank,
)
from app.domain.alerting.policy import CHANNELS, AlertPolicy, Preferences, WebhookTarget
from app.domain.alerting.routing import Delivery, Recipient, recipients, route
from app.domain.events.events import (
    AlertChanged,
    AnomalyChanged,
    DomainEvent,
    NotificationChanged,
    PredictionChanged,
    PresenceChanged,
    RemediationChanged,
    TwinMessage,
)
from app.repositories.alerting import AlertRepository
from app.repositories.base import SystemEventRecord

#: a notification claimed longer ago than this is treated as interrupted (provider timeout is 15 s)
STUCK_SENDING_S = 300

log = structlog.get_logger("alerting")

POLICY_KEY = "alert_policy"
UPDATE_PERSIST_S = 30.0
WEBHOOK_KEY = "webhook_targets"
EPHEMERAL = {"browser": 3600.0, "windows": 3600.0}
TIMELINE_SEVERITY = {
    "CRITICAL": "critical",
    "HIGH": "error",
    "MEDIUM": "warning",
    "LOW": "info",
    "INFO": "info",
}


# ------------------------------------------------------------------------------------ adapters
def from_anomaly(e: AnomalyChanged) -> AlertEvent | None:
    a = e.anomaly
    if a.get("lifecycle") == "SUPPRESSED":
        return None
    kind = {"detected": "opened", "updated": "updated", "resolved": "closed"}.get(e.kind)
    if kind is None:
        return None
    ev = a.get("evidence") or {}
    return AlertEvent(
        source_type="anomaly",
        source_id=str(a.get("anomaly_id")),
        device_id=e.device_id,
        kind=kind,
        alert_type=str(a.get("anomaly_type") or "anomaly"),
        category="anomaly",
        severity=str(a.get("level") or "LOW"),
        confidence=a.get("confidence"),
        title=str(a.get("title") or "Anomaly"),
        summary=str(a.get("message") or ""),
        condition=str(a.get("rule_id") or a.get("metric_key")),
        occurred_at=datetime.fromisoformat(a["started_at"]) if a.get("started_at") else e.occurred_at,
        metric=a.get("metric_key"),
        threshold=_num(a.get("threshold")),
        observed=_num(a.get("value")),
        expected=_num(a.get("expected_value")),
        close_reason="anomaly "
        + str(a.get("lifecycle", "resolved")).lower()
        + ": "
        + str(ev.get("closed_because") or "condition ended"),
        evidence={
            "anomaly_id": a.get("anomaly_id"),
            "summary": ev.get("summary"),
            "expected_range": [a.get("expected_min"), a.get("expected_max")],
            "deviation_score": a.get("deviation_score"),
            "detector": a.get("detector"),
        },
    )


def from_prediction(e: PredictionChanged, policy: AlertPolicy) -> AlertEvent | None:
    p = e.prediction
    kind = {"created": "opened", "updated": "updated"}.get(e.kind, "closed")
    eta = p.get("time_to_threshold_s")
    sev = policy.prediction_severity(str(p.get("severity") or "INFO"), eta, p.get("confidence_band"))
    reason = {
        "invalidated": "prediction invalidated",
        "expired": "prediction expired",
        "confirmed": "the predicted threshold was reached (now an observed condition)",
        "cancelled": "prediction cancelled",
    }.get(e.kind, e.kind)
    return AlertEvent(
        source_type="prediction",
        source_id=str(p.get("prediction_id")),
        device_id=e.device_id,
        kind=kind,
        alert_type=str(p.get("prediction_type") or "prediction"),
        category="prediction",
        severity=sev,
        confidence=p.get("confidence"),
        title=_prediction_title(p),
        summary=str(p.get("statement") or ""),
        condition=str(p.get("target_id")),
        occurred_at=e.occurred_at,
        metric=p.get("metric"),
        threshold=_num(p.get("threshold")),
        observed=_num(p.get("current_value")),
        expected=_num(p.get("forecast_value")),
        close_reason=f"{reason}{': ' + p['reason'] if p.get('reason') else ''}",
        evidence={
            "prediction_id": p.get("prediction_id"),
            "crossing_at": p.get("crossing_at"),
            "crossing_earliest": p.get("crossing_earliest"),
            "crossing_latest": p.get("crossing_latest"),
            "time_to_threshold_s": eta,
            "confidence_band": p.get("confidence_band"),
            "model": f"{p.get('model_type')} {p.get('model_version')}",
        },
    )


def _prediction_title(p: dict[str, Any]) -> str:
    names = {
        "disk": "Storage capacity risk",
        "memory": "Memory exhaustion risk",
        "battery": "Battery depletion risk",
        "temperature": "Thermal escalation risk",
        "cpu": "Sustained CPU pressure",
    }
    return names.get(str(p.get("target_id")), "Predicted threshold crossing")


#: remediation lifecycle -> (alert condition, opened-by kinds, closed-by kinds, severity, title)
REMEDIATION_ALERTS = (
    (
        "approval",
        ("approval_required",),
        ("approved", "rejected", "expired", "cancelled"),
        "MEDIUM",
        "Approval required: {name}",
    ),
    ("failed", ("failed",), (), "MEDIUM", "Remediation failed: {name}"),
    ("circuit", ("circuit_open",), (), "HIGH", "Remediation stopped after repeated failures: {name}"),
)


def from_remediation(e: RemediationChanged) -> list[AlertEvent]:
    """Phase 8: people learn that an action waits for their approval, failed, or was stopped."""
    r = e.remediation
    out = []
    for cond, opens, closes, sev, title in REMEDIATION_ALERTS:
        if e.kind not in opens and e.kind not in closes:
            continue
        if cond == "approval" and e.kind not in opens and r.get("status") == "PENDING_APPROVAL":
            continue
        summary = {
            "approval": f"{r.get('risk_level')} risk on {e.device_id}. {str(r.get('reason') or '')[:300]}",
            "failed": f"{str(r.get('failure_reason') or 'failed')[:300]}. No additional action was taken.",
            "circuit": "Automatic proposals for this action on this device are paused; review the failures.",
        }[cond]
        out.append(AlertEvent(
            source_type="remediation", source_id=f"{r.get('remediation_id')}:{cond}" if cond != "circuit"
            else f"{e.device_id}:{r.get('action_type')}:circuit", device_id=e.device_id,
            kind="opened" if e.kind in opens else "closed", alert_type=f"remediation_{cond}", category="system",  # noqa: E501
            severity=sev, confidence=None, title=title.format(name=r.get("action_name") or r.get("action_type")),  # noqa: E501
            summary=summary, condition=f"remediation:{cond}:{r.get('action_type')}", occurred_at=e.occurred_at,  # noqa: E501
            close_reason=f"remediation {e.kind}", evidence={"remediation_id": r.get("remediation_id"),
                                                           "action_type": r.get("action_type")},
        ))  # fmt: skip
    return out


def from_presence(e: PresenceChanged) -> AlertEvent | None:
    if e.presence == "OFFLINE":
        kind = "opened"
    elif e.presence == "ONLINE" and e.previous_presence in ("OFFLINE", "STALE"):
        kind = "closed"
    else:
        return None
    last = e.last_contact_at.isoformat() if e.last_contact_at else "unknown"
    return AlertEvent(
        source_type="connectivity",
        source_id=f"{e.device_id}:presence:{int(e.occurred_at.timestamp())}",
        device_id=e.device_id,
        kind=kind,
        alert_type="agent_offline",
        category="connectivity",
        severity="MEDIUM",
        confidence=None,
        title="Endpoint agent offline",
        summary=f"No contact from the agent since {last}.",
        condition="presence",
        occurred_at=e.occurred_at,
        close_reason="the agent is reporting again",
        evidence={"last_contact_at": last},
    )


def from_twin_health(msg: TwinMessage) -> list[AlertEvent]:
    """Twin health transitions -> security / agent alerts (performance reasons already alert via Phase 4).

    The alert quotes the matching reasons themselves (e.g. "Secure Boot: off"), not the first health
    reason. A standing configuration finding is MEDIUM (no escalation); a CRITICAL one is HIGH."""
    ev = (msg.body or {}).get("timeline_event") or {}
    if ev.get("type") != "twin.health":
        return []
    data = ev.get("data") or {}
    to = data.get("to")
    details = data.get("details") or [
        {"rule": r, "state": to, "message": r} for r in data.get("reasons") or []
    ]
    when = datetime.fromisoformat(ev["timestamp"]) if ev.get("timestamp") else msg.occurred_at
    out = []
    for cond, rules, source, category, title in (
        ("security", {"security", "posture"}, "security", "security", "Security posture degraded"),
        ("agent", {"agent"}, "device_health", "system", "Endpoint agent degraded"),
    ):
        hits = [d for d in details if d.get("rule") in rules and d.get("state") in ("WARNING", "CRITICAL")]
        worst = "CRITICAL" if any(d.get("state") == "CRITICAL" for d in hits) else "WARNING"
        severity = "HIGH" if cond == "security" and worst == "CRITICAL" else "MEDIUM"
        summary = "; ".join(dict.fromkeys(str(d.get("message")) for d in hits))[:500] or str(
            ev.get("message") or ""
        )
        out.append(
            AlertEvent(
                source_type=source,
                source_id=str(ev.get("event_id")),
                device_id=msg.device_id,
                kind="opened" if hits else "closed",
                alert_type=f"{cond}_health",
                category=category,
                severity=severity,
                confidence=None,
                title=title,
                summary=summary,
                condition=cond,
                occurred_at=when,
                close_reason="health recovered",
                evidence={"health": to, "findings": hits},
            )
        )
    return out


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _idem(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:48]


# ------------------------------------------------------------------------------------- service
class AlertService:
    def __init__(
        self,
        settings: Settings,
        repo: AlertRepository,
        settings_store: Any,
        admin_repo: Any,
        assignments: Any,
        ws: Any,
        publish: Callable[[list[DomainEvent]], Awaitable[None]],
        record_system_event: Callable[[SystemEventRecord], None],
        providers: dict[str, NotificationProvider],
        default_subject: str,
    ) -> None:
        self._s = settings
        self.repo = repo
        self._store = settings_store
        self._admin_repo = admin_repo
        self._assignments = assignments
        self._ws = ws
        self._publish = publish
        self._record = record_system_event
        self.providers = providers
        self._default_subject = default_subject  # recipient when no user accounts exist (auth none / api key)
        self.tenant_id = "default"
        self.tenancy: Any = None  # Phase 9: TenancyService (recipients are members of the device's organisation)  # noqa: E501
        self.policy = AlertPolicy()
        self.engine = AlertEngine(self.policy, self.tenant_id)
        self.webhooks: dict[str, WebhookTarget] = {}
        self._lock = asyncio.Lock()
        self._kick = asyncio.Event()
        self._queue: asyncio.Queue[DomainEvent] = asyncio.Queue(maxsize=10_000)
        self._running = False
        self._last_saved: dict[str, float] = {}
        self._audit_seen: dict[str, int] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._sent_recent: dict[str, deque[float]] = {}
        self._prefs_cache: dict[str, Preferences] = {}
        self.stats = {
            "events_dropped": 0,
            "update_writes_coalesced": 0,
            "events": 0,
            "delivery_runs": 0,
            "delivered": 0,
            "failed": 0,
            "retries": 0,
            "dead_letter": 0,
            "throttled_to_digest": 0,
            "provider_ms_total": 0.0,
        }
        self.config_meta: dict[str, Any] = {"version": 0, "updated_at": None, "updated_by": None}

    # ----------------------------------------------------------------------- lifecycle
    async def load(self) -> None:
        try:
            stored = await self._store.get_setting(POLICY_KEY)
            if stored:
                self._apply_policy(stored)
            hooks = await self._store.get_setting(WEBHOOK_KEY)
            if hooks:
                self.webhooks = {
                    t["name"]: WebhookTarget(
                        t["name"],
                        t["url"],
                        t.get("min_severity", "HIGH"),
                        tuple(t.get("categories") or ()),
                        bool(t.get("enabled", True)),
                    )
                    for t in hooks.get("targets", [])
                }
            for a in await self.repo.open_alerts(self.tenant_id):
                self.engine.adopt(a)
        except Exception as exc:
            log.warning("alert_state_load_failed", error=str(exc)[:200])

    def _apply_policy(self, stored: dict[str, Any]) -> None:
        self.policy = AlertPolicy().merged(stored.get("policy") or {})
        self.engine.policy = self.policy
        self.config_meta = {k: stored.get(k) for k in ("version", "updated_at", "updated_by")}

    async def run(self, stop: asyncio.Event) -> None:
        self._running = True
        tasks = [
            asyncio.create_task(self._delivery_loop(stop), name="notify_delivery"),
            asyncio.create_task(self._tick_loop(stop), name="alert_tick"),
            asyncio.create_task(self._event_loop(stop), name="alert_events"),
        ]
        try:
            await watch(tasks, stop)  # a dead delivery / tick / event loop restarts the whole service
        finally:
            self._kick.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # -------------------------------------------------------------------------- inputs
    async def on_event(self, event: DomainEvent) -> None:
        """EventBus subscriber: only enqueues (never blocks telemetry, never raises into the bus)."""
        if not isinstance(
            event, (AnomalyChanged, PredictionChanged, PresenceChanged, TwinMessage, RemediationChanged)
        ):
            return
        if isinstance(event, TwinMessage) and event.kind != "twin.event.created":
            return
        if self._running:
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                self.stats["events_dropped"] += 1
                ALERT_EVENTS_DROPPED.inc()
                if self.stats["events_dropped"] % 100 == 1:
                    log.warning("alert_event_queue_full", dropped=self.stats["events_dropped"])
            return
        await self.process(event)  # no background loop (tests / tools): process inline

    async def _event_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            event = await self._queue.get()
            try:
                await self.process(event)
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """Wait until every queued event is processed (shutdown, tests)."""
        if self._running:
            await self._queue.join()

    async def process(self, event: DomainEvent) -> None:
        """Adapt -> engine -> outputs. Never raises."""
        try:
            items: list[AlertEvent] = []
            if isinstance(event, AnomalyChanged):
                ev = from_anomaly(event)
                items = [ev] if ev else []
            elif isinstance(event, PredictionChanged):
                ev = from_prediction(event, self.policy)
                items = [ev] if ev else []
            elif isinstance(event, PresenceChanged):
                ev = from_presence(event)
                items = [ev] if ev else []
            elif isinstance(event, TwinMessage) and event.kind == "twin.event.created":
                items = from_twin_health(event)
            elif isinstance(event, RemediationChanged):
                items = from_remediation(event)
            for item in items:
                await self.ingest(item)
        except Exception:
            log.exception("alert_event_failed", event=getattr(event, "name", "?"))

    async def ingest(self, e: AlertEvent, now: datetime | None = None) -> list[AlertChange]:
        now = now or datetime.now(UTC)
        self.stats["events"] += 1
        async with self._lock:
            changes = self.engine.ingest(e, now)
        for ch in changes:
            await self._handle(ch, now)
        return changes

    # ------------------------------------------------------------------------- outputs
    async def _handle(self, ch: AlertChange, now: datetime, actor: str = "system") -> None:
        if ch.kind == "ignored" or ch.alert is None:
            return
        a = ch.alert
        ALERTS_TOTAL.labels(ch.kind, a.severity).inc()
        mono = time.monotonic()
        repetitive = (
            ch.kind == "updated" and not ch.notify and len(a.audit) == self._audit_seen.get(a.alert_id)
        )
        if repetitive and mono - self._last_saved.get(a.alert_id, 0.0) < UPDATE_PERSIST_S:
            self.stats["update_writes_coalesced"] += 1  # same condition again: written at most every 30 s
        else:
            try:
                await self.repo.save_alert(a)
                self._last_saved[a.alert_id] = mono
                self._audit_seen[a.alert_id] = len(a.audit)
                if not a.is_open:
                    self._last_saved.pop(a.alert_id, None)
                    self._audit_seen.pop(a.alert_id, None)
            except Exception as exc:
                log.warning("alert_persist_failed", alert_id=a.alert_id, error=str(exc)[:200])
        log.info(
            "alert_" + ch.kind,
            alert_id=a.alert_id,
            device_id=a.device_id,
            severity=a.severity,
            source=a.source_type,
            event_id=a.event_id,
            correlation_id=a.alert_id,
            notify=ch.notify,
            reason=ch.reason,
            actor=actor,
        )
        await self._timeline(a, ch.kind, now, actor)
        events: list[DomainEvent] = [AlertChanged(device_id=a.device_id, kind=ch.kind, alert=a.to_dict())]
        if a.status in (AlertStatus.RESOLVED, AlertStatus.EXPIRED, AlertStatus.SUPPRESSED):
            await self._cancel_pending(a, now, f"alert {a.status.value.lower()}")
        if ch.notify:
            events += await self._notify(a, ch, now)
        await self._publish(events)
        if ch.notify:
            self._kick.set()

    async def _timeline(self, a: Alert, kind: str, now: datetime, actor: str) -> None:
        msg = {
            "created": f"{a.severity} alert: {a.title}",
            "updated": f"Alert updated: {a.title} ({a.summary[:80]})",
            "acknowledged": f"Alert acknowledged by {actor}: {a.title}",
            "resolved": f"Alert resolved: {a.title}",
            "expired": f"Alert expired: {a.title}",
            "suppressed": f"Alert suppressed by {actor}: {a.title}",
            "escalated": f"Alert escalated (level {a.escalation_level}): {a.title}",
        }.get(kind)
        if msg is None or (kind == "updated" and a.occurrences % 10 != 1):
            return  # updates of an ongoing alert are summarised, not one timeline line each
        sev = TIMELINE_SEVERITY.get(a.severity, "info") if kind in ("created", "escalated") else "info"
        rec = SystemEventRecord(
            a.device_id,
            now,
            f"alert.{kind}",
            sev,
            msg[:300],
            {"alert_id": a.alert_id, "severity": a.severity, "status": a.status.value},
        )
        try:
            self._record(rec)
        except Exception as exc:
            log.debug("alert_timeline_record_failed", error=str(exc)[:200])
        await self._publish(
            [
                TwinMessage(
                    device_id=a.device_id,
                    kind="twin.event.created",
                    body={
                        "timeline_event": {
                            "event_id": f"alert:{a.alert_id}:{kind}:{len(a.audit)}",
                            "device_id": a.device_id,
                            "type": f"alert.{kind}",
                            "severity": sev,
                            "timestamp": now.isoformat(),
                            "message": msg[:300],
                            "data": {"alert_id": a.alert_id, "severity": a.severity},
                        }
                    },
                )
            ]
        )

    # ------------------------------------------------------------------------- routing
    async def _users(self, device_id: str) -> list[Recipient]:
        owner = self._assignments.get(device_id) if self._assignments else None
        owner_name = owner.username if owner else None
        tenancy = getattr(self, "tenancy", None)
        if tenancy is not None and tenancy.org_of(device_id) is not None:
            # Phase 9: only active members of the device's organisation who may see this device
            members = tenancy.recipients(device_id)
            if (
                not members
                and tenancy.org_of(device_id) == "default"
                and self._default_subject in ("local", "api-key")
            ):
                return [Recipient(self._default_subject, "admin")]
            return [Recipient(u, role, u == owner_name) for u, role in members]
        try:
            users = await self._admin_repo.list_users()
        except Exception:
            users = []
        if not users:
            return [Recipient(self._default_subject, "admin")]
        return [
            Recipient(u.username, u.role.value, u.username == owner_name) for u in users if not u.disabled
        ]

    async def preferences(self, user_id: str) -> Preferences:
        cached = self._prefs_cache.get(user_id)
        if cached is not None:
            return cached
        try:
            raw = await self.repo.get_preferences(user_id)
        except Exception:
            raw = None
        prefs = Preferences.from_dict(raw) if raw else Preferences()
        self._prefs_cache[user_id] = prefs
        return prefs

    def _throttled(self, user_id: str, now: datetime) -> bool:
        """Fatigue guard: beyond the hourly budget, further non-critical notifications are grouped."""
        q = self._sent_recent.setdefault(user_id, deque())
        ts = now.timestamp()
        while q and ts - q[0] > 3600:
            q.popleft()
        if len(q) >= self.policy.max_notifications_per_user_hour:
            return True
        q.append(ts)
        return False

    async def _notify(self, a: Alert, ch: AlertChange, now: datetime) -> list[DomainEvent]:
        level_roles = ch.escalation_roles if ch.kind == "escalated" else None
        users = recipients(a, await self._users(a.device_id), level_roles)
        group = bool(a.metadata.get("grouped"))
        resolution = ch.kind == "resolved"
        events: list[DomainEvent] = []
        batch: list[Notification] = []
        for u in users:
            prefs = await self.preferences(u.user_id)
            deliveries = route(a, u, prefs, self.policy, now, group, resolution)
            if deliveries and a.severity != "CRITICAL" and not resolution and self._throttled(u.user_id, now):
                self.stats["throttled_to_digest"] += 1
                deliveries = [
                    Delivery(
                        d.user_id,
                        d.channel,
                        now + timedelta(seconds=self.policy.group_window_s),
                        True,
                        "notification budget reached: grouped",
                    )
                    for d in deliveries
                ]
            batch.extend(self._build(a, ch, d, prefs, now) for d in deliveries)
        for n in await self.repo.insert_notifications(batch):  # one transaction for all recipients
            NOTIFICATIONS_TOTAL.labels("created", n.channel).inc()
        if not resolution:
            events += await self._webhooks(a, ch, now)
        return events

    def _build(
        self, a: Alert, ch: AlertChange, d: Delivery, prefs: Preferences, now: datetime
    ) -> Notification:
        title, body = message_for(a, ch.kind)
        payload = {
            "alert_type": a.alert_type,
            "status": a.status.value,
            "metric": a.metadata.get("metric"),
            "observed": a.metadata.get("observed"),
            "expected": a.metadata.get("expected"),
            "threshold": a.metadata.get("threshold"),
            "confidence": a.confidence,
            "first_detected_at": a.first_detected_at.isoformat(),
            "crossing_at": (a.metadata.get("evidence") or {}).get("crossing_at"),
            "correlation_key": a.correlation_key,
            "grouped": d.grouped,
            "route_reason": d.reason,
            "kind": ch.kind,
        }
        if d.channel == "email":
            payload["email"] = prefs.email
        n = Notification(
            notification_id=str(uuid.uuid4()),
            tenant_id=a.tenant_id,
            alert_id=a.alert_id,
            user_id=d.user_id,
            device_id=a.device_id,
            channel=d.channel,
            status=NotificationStatus.PENDING,
            priority=a.priority,
            severity=a.severity,
            category=a.category,
            title=title,
            body=body,
            payload=payload,
            idempotency_key=_idem(
                a.alert_id, d.user_id, d.channel, ch.kind, a.escalation_level, a.occurrences
            ),
            created_at=now,
            updated_at=now,
            deliver_after=d.deliver_after,
            escalation_level=a.escalation_level,
        )
        n.history.append(_entry(now, "created", None, "PENDING", d.reason))
        return n

    async def _webhooks(self, a: Alert, ch: AlertChange, now: datetime) -> list[DomainEvent]:
        for t in self.webhooks.values():
            if not t.enabled or sev_rank(a.severity) < sev_rank(t.min_severity):
                continue
            if t.categories and a.category not in t.categories:
                continue
            d = Delivery(f"webhook:{t.name}", "webhook", None, False, "organisation integration")
            n = self._build(a, ch, d, Preferences(), now)
            if await self.repo.insert_notification(n):
                NOTIFICATIONS_TOTAL.labels("created", "webhook").inc()
        return []

    async def _cancel_pending(self, a: Alert, now: datetime, why: str) -> None:
        cancelled: list[Notification] = []
        try:
            for n in await self.repo.notifications_for_alert(a.alert_id):
                if (
                    n.status in (NotificationStatus.PENDING, NotificationStatus.RETRYING)
                    and n.payload.get("kind") != "resolved"
                ):
                    n.transition(NotificationStatus.CANCELLED, now, why)
                    cancelled.append(n)
            if cancelled:
                await self.repo.save_notifications(cancelled)
        except Exception as exc:
            log.debug("cancel_pending_failed", error=str(exc)[:200])

    # ------------------------------------------------------------------------- delivery
    async def _delivery_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._kick.wait(), timeout=2.0)
            self._kick.clear()
            try:
                while await self.deliver_due(datetime.now(UTC)) >= 50:
                    pass  # keep draining a backlog
            except Exception:
                log.exception("notification_delivery_loop_failed")

    async def deliver_due(self, now: datetime, limit: int = 50) -> int:
        batch = await self.repo.claim_due(now, limit)
        if not batch:
            return 0
        self.stats["delivery_runs"] += 1
        batch = await self._merge_digests(batch, now)
        sem = asyncio.Semaphore(16)

        async def one(n: Notification) -> None:
            async with sem:
                await self._deliver(n, now)

        await asyncio.gather(*(one(n) for n in batch))
        return len(batch)

    async def _merge_digests(self, batch: list[Notification], now: datetime) -> list[Notification]:
        """Grouped notifications due together for one user and channel become one digest."""
        groups: dict[tuple[str, str], list[Notification]] = {}
        rest: list[Notification] = []
        for n in batch:
            if n.payload.get("grouped") and n.severity != "CRITICAL" and n.alert_id:
                groups.setdefault((n.user_id, n.channel), []).append(n)
            else:
                rest.append(n)
        for (user, channel), members in groups.items():
            if len(members) == 1:
                rest.append(members[0])
                continue
            top = max(members, key=lambda m: sev_rank(m.severity))
            devices = sorted({m.device_id or "?" for m in members})
            digest = Notification(
                notification_id=str(uuid.uuid4()),
                tenant_id=top.tenant_id,
                alert_id=None,
                user_id=user,
                device_id=devices[0] if len(devices) == 1 else None,
                channel=channel,
                status=NotificationStatus.SENDING,
                priority=top.priority,
                severity=top.severity,
                category=top.category,
                title=f"{len(members)} related alerts" + (f" on {devices[0]}" if len(devices) == 1 else ""),
                body="; ".join(m.title for m in members[:6]) + (" ..." if len(members) > 6 else ""),
                payload={
                    "digest": True,
                    "alert_ids": [m.alert_id for m in members],
                    "notification_ids": [m.notification_id for m in members],
                },
                idempotency_key=_idem("digest", *sorted(m.notification_id for m in members)),
                created_at=now,
                updated_at=now,
            )
            digest.history.append(_entry(now, "created", None, "SENDING", f"digest of {len(members)}"))
            if await self.repo.insert_notification(digest):
                for m in members:
                    m.transition(NotificationStatus.QUEUED, now, "merged")
                    m.transition(
                        NotificationStatus.CANCELLED, now, f"delivered in digest {digest.notification_id}"
                    )
                await self.repo.save_notifications(members)
                rest.append(digest)
        return rest

    async def _deliver(self, n: Notification, now: datetime) -> None:
        provider = self.providers.get(n.channel)
        max_age = EPHEMERAL.get(n.channel)
        if max_age and (now - n.created_at).total_seconds() > max_age:
            n.transition(
                NotificationStatus.EXPIRED,
                now,
                f"{n.channel} notifications expire after {max_age / 60:.0f} min",
            )
            await self._save(n)
            return
        if provider is None:
            n.transition(NotificationStatus.FAILED, now, "no provider for this channel")
            n.failed_at, n.failure_reason = now, "no provider for this channel"
            await self._save(n)
            return
        n.attempt_count += 1
        n.provider = provider.name
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(provider.send(n), timeout=15.0)
        except TimeoutError:
            result = DeliveryResult(False, "transient", "provider timeout")
        except Exception as exc:  # a provider bug must not stop the worker
            result = DeliveryResult(False, "transient", f"provider error: {type(exc).__name__}")
        ms = (time.perf_counter() - started) * 1000
        PROVIDER_LATENCY.labels(provider.name).observe(ms)
        self.stats["provider_ms_total"] += ms
        if result.ok:
            n.provider_message_id = result.provider_message_id
            if result.pending_pickup:
                n.transition(NotificationStatus.QUEUED, now, "waiting for the agent to show it")
            else:
                n.transition(NotificationStatus.DELIVERED, now, f"attempt {n.attempt_count}")
                n.delivered_at = now
                self.stats["delivered"] += 1
                NOTIFICATIONS_TOTAL.labels("delivered", n.channel).inc()
            await self._save(n, notify=True)
            return
        n.last_error = (result.error or "")[:300]
        if result.failure == "deferred":
            n.attempt_count -= 1  # waiting for the recipient is not a failed attempt
        retry_at = next_retry(n.attempt_count, result, now)
        if retry_at is None:
            n.transition(NotificationStatus.FAILED, now, f"{result.failure}: {n.last_error}")
            n.failed_at, n.failure_reason = now, f"{result.failure}: {n.last_error}"
            self.stats["dead_letter"] += 1
            self.stats["failed"] += 1
            NOTIFICATIONS_TOTAL.labels("failed", n.channel).inc()
            log.warning(
                "notification_dead_letter",
                notification_id=n.notification_id,
                alert_id=n.alert_id,
                channel=n.channel,
                provider=provider.name,
                attempts=n.attempt_count,
                failure=result.failure,
                error=n.last_error,
            )
        else:
            n.transition(NotificationStatus.RETRYING, now, f"{result.failure}: {n.last_error}")
            n.next_retry_at = retry_at
            if result.failure != "deferred":
                self.stats["retries"] += 1
                NOTIFICATION_RETRIES.labels(n.channel).inc()
        await self._save(n)

    async def _save(self, n: Notification, notify: bool = False) -> None:
        try:
            await self.repo.save_notification(n)
        except Exception as exc:
            log.warning(
                "notification_persist_failed", notification_id=n.notification_id, error=str(exc)[:200]
            )
        if notify and n.channel == "in_app":
            self.push_to_user(n, "created")

    def push_to_user(self, n: Notification, kind: str) -> int:
        """Send a notification.* message to the recipient's open sessions only. Returns their count."""
        msg = NotificationChanged(
            device_id=n.device_id or "", kind=kind, notification=n.to_dict(), recipient=n.user_id
        )

        async def send() -> None:
            await self._publish([msg])

        task = asyncio.get_running_loop().create_task(send())
        self._background.add(task)  # keep a reference until it ran
        task.add_done_callback(self._background.discard)
        return int(self._ws.sessions_of(n.user_id)) if self._ws else 0

    # ----------------------------------------------------------------------------- tick
    async def _tick_loop(self, stop: asyncio.Event) -> None:
        last_purge = 0.0
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=15.0)
            if stop.is_set():
                break
            now = datetime.now(UTC)
            try:
                await self.tick(now)
                await self.delivery_health(now)
                if time.monotonic() - last_purge > 3600:
                    last_purge = time.monotonic()
                    cutoff = now - timedelta(days=self._s.notification_retention_days)
                    removed = await self.repo.purge_closed_before(cutoff)
                    if removed:
                        log.info("alert_retention_purged", rows=removed)
            except Exception:
                log.exception("alert_tick_failed")

    async def delivery_health(self, now: datetime) -> dict[str, Any]:
        """Phase 10: recover deliveries stuck in SENDING (crash mid-send) and export the backlog SLIs."""
        requeued = await self.repo.requeue_stuck(now - timedelta(seconds=STUCK_SENDING_S), now)
        if requeued:
            NOTIFICATIONS_REQUEUED.inc(requeued)
            log.warning("notifications_requeued_after_interrupted_delivery", count=requeued)
            self._kick.set()
        count, oldest = await self.repo.backlog(now)
        NOTIFICATION_BACKLOG.set(count)
        NOTIFICATION_OLDEST_DUE.set(oldest or 0.0)
        return {"requeued": requeued, "backlog": count, "oldest_due_s": oldest}

    async def tick(self, now: datetime) -> list[AlertChange]:
        async with self._lock:
            changes = self.engine.tick(now)
        for ch in changes:
            await self._handle(ch, now)
        return changes

    # ---------------------------------------------------------------------- user actions
    async def act(
        self, alert: Alert, action: str, actor: str, note: str | None, until: datetime | None = None
    ) -> Alert:
        now = datetime.now(UTC)
        async with self._lock:
            live = next((a for a in self.engine.open_alerts() if a.alert_id == alert.alert_id), alert)
            if action == "acknowledge":
                ch = self.engine.acknowledge(live, actor, now, note)
            elif action == "resolve":
                ch = self.engine.resolve(live, actor, now, note)
            elif action == "suppress":
                ch = self.engine.suppress(live, actor, now, until, note)
            else:
                raise ValueError(action)
        await self._handle(ch, now, actor)
        return live

    async def mark_read(self, n: Notification, now: datetime) -> Notification:
        if n.status is NotificationStatus.DELIVERED:
            n.transition(NotificationStatus.READ, now, "read by the user")
            n.read_at = now
            await self.repo.save_notification(n)
            self.push_to_user(n, "read")
        return n

    async def agent_ack(self, n: Notification, now: datetime) -> None:
        if n.status is NotificationStatus.QUEUED:
            n.transition(NotificationStatus.SENDING, now, "agent fetched")
            n.transition(NotificationStatus.DELIVERED, now, "shown by the agent")
            n.delivered_at = now
            NOTIFICATIONS_TOTAL.labels("delivered", n.channel).inc()
            await self.repo.save_notification(n)

    def channels_status(self) -> dict[str, Any]:
        out = {}
        for ch in CHANNELS:
            p = self.providers.get(ch)
            ok, why = p.available() if p else (False, "no provider")
            out[ch] = {"available": ok, "reason": why}
        return out

    async def set_preferences(self, user_id: str, prefs: Preferences) -> None:
        await self.repo.set_preferences(self.tenant_id, user_id, prefs.public(), datetime.now(UTC))
        self._prefs_cache[user_id] = prefs


def _entry(at: datetime, action: str, frm: str | None, to: str | None, detail: str | None) -> Any:
    from app.domain.alerting.models import AuditEntry

    return AuditEntry(at, "system", action, frm, to, detail)


def message_for(a: Alert, kind: str) -> tuple[str, str]:
    """Short, factual wording; never 'CPU high' x 10: ongoing alerts say for how long."""
    dur = a.metadata.get("duration_s")
    lead = {
        "created": "",
        "updated": "Escalated: ",
        "escalated": "Not yet acknowledged: ",
        "resolved": "Resolved: ",
    }.get(kind, "")
    title = f"{lead}{a.title}"[:200]
    body = a.summary
    if kind == "updated" and dur:
        body = f"{a.summary} (ongoing for {round(dur / 60)} min)"
    if kind == "resolved":
        body = f"{a.title} on {a.device_id} is resolved."
    if a.confidence is not None and kind != "resolved":
        body += f" Confidence {round(a.confidence * 100)}%."
    return title, body[:1000]
