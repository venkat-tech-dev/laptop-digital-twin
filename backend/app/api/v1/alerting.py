"""Phase-6 alert and notification APIs.

Authorization is enforced here, server-side:
* alerts: callers see alerts of devices they may see (employees: assigned devices only; anything else
  answers 404, so ids cannot be probed); acknowledge = operator or the device's owner; resolve and
  suppress = operator; every action is audited with the actor
* notifications and preferences: only the caller's own (the user id comes from the token, never from
  the request); another user's notification answers 404
* alert policy and webhook targets: administrators; webhook signing secrets are never returned
* agent endpoints (Windows toasts): the device token may only fetch / acknowledge its own device's items
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from app.api.access import Staff, check_device, visible_devices
from app.api.deps import Admin, AgentIdentity, ContainerDep, Reader, require_agent, require_platform_scope
from app.domain.alerting.models import CHANNEL_NAMES, InvalidTransitionError, Notification
from app.domain.alerting.policy import FREQUENCIES, Preferences, QuietHours
from app.repositories.alerting import AlertFilter, NotificationFilter
from app.services.alerting import AlertService
from app.services.notify_providers import check_webhook_url

alert_router = APIRouter(prefix="/alerts", tags=["alerting"])
notification_router = APIRouter(prefix="/notifications", tags=["alerting"])
prefs_router = APIRouter(prefix="/notification-preferences", tags=["alerting"])
admin_router = APIRouter(tags=["alerting (admin)"])
agent_router = APIRouter(prefix="/agent/notifications", tags=["ingest (agent only)"])

Severity = Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
Category = Literal["anomaly", "prediction", "connectivity", "security", "system"]
AlertStatusLit = Literal["OPEN", "ONGOING", "ACKNOWLEDGED", "RESOLVED", "SUPPRESSED", "EXPIRED"]
Source = Literal["anomaly", "prediction", "connectivity", "device_health", "security", "remediation"]


def _svc(container: Any) -> AlertService:
    if container.alerts is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Alerting is disabled")
    svc: AlertService = container.alerts
    return svc


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown {what}")


async def _alert_for(container: Any, principal: Any, alert_id: str) -> Any:
    svc = _svc(container)
    a = next((x for x in svc.engine.open_alerts() if x.alert_id == alert_id), None)
    if a is None:
        a = await svc.repo.get_alert(alert_id[:64])
    if a is None or a.tenant_id != svc.tenant_id:
        raise _not_found("alert")
    try:
        check_device(principal, container, a.device_id)
    except HTTPException as exc:
        raise _not_found("alert") from exc
    return a


# ---------------------------------------------------------------------------------- alerts
@alert_router.get("", summary="Alerts (newest first) with filters and pagination")
async def list_alerts(
    container: ContainerDep,
    principal: Reader,
    device_id: str | None = Query(default=None, max_length=64),
    severity: list[Severity] = Query(default=[]),
    category: list[Category] = Query(default=[]),
    status_: list[AlertStatusLit] = Query(default=[], alias="status"),
    source_type: Source | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    svc = _svc(container)
    allowed = visible_devices(principal, container)
    if device_id is not None and allowed is not None and device_id not in allowed:
        raise _not_found("device")
    f = AlertFilter(
        svc.tenant_id,
        frozenset(allowed) if allowed is not None else None,
        device_id,
        tuple(severity),
        tuple(category),
        tuple(status_),
        source_type,
        since,
        until,
    )
    items = await svc.repo.search_alerts(f, limit, offset)
    live = {a.alert_id: a for a in svc.engine.open_alerts()}
    return {"items": [(live.get(a.alert_id) or a).to_dict() for a in items], "limit": limit, "offset": offset}


@alert_router.get("/{alert_id}", summary="One alert: facts, evidence, audit trail and delivery history")
async def get_alert(alert_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    a = await _alert_for(container, principal, alert_id)
    svc = _svc(container)
    notes = await svc.repo.notifications_for_alert(a.alert_id)
    staff = principal.has_role("viewer")
    deliveries = [
        {
            k: n.to_dict()[k]
            for k in (
                "notification_id",
                "user_id",
                "channel",
                "status",
                "attempt_count",
                "provider",
                "delivered_at",
                "read_at",
                "failed_at",
                "failure_reason",
                "created_at",
                "escalation_level",
            )
        }
        for n in notes
        if staff or n.user_id == principal.subject
    ]
    stored = await svc.repo.get_alert(a.alert_id)
    audit_src = stored if stored is not None and len(stored.audit) > len(a.audit) else a
    return {**a.to_dict(), "audit": [e.public() for e in audit_src.audit], "deliveries": deliveries}


class ActionIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class SuppressIn(ActionIn):
    hours: float = Field(default=24.0, gt=0, le=168)


async def _act(
    container: Any,
    principal: Any,
    alert_id: str,
    action: str,
    note: str | None,
    until: datetime | None = None,
) -> dict[str, Any]:
    a = await _alert_for(container, principal, alert_id)
    if action == "acknowledge":
        owner = container.assignments.get(a.device_id) if container.assignments else None
        if not (principal.has_role("operator") or (owner and owner.username == principal.subject)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role or device owner required")
    elif not principal.has_role("operator"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required")
    try:
        updated = await _svc(container).act(a, action, principal.subject, note, until)
    except InvalidTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return updated.to_dict()


@alert_router.post("/{alert_id}/acknowledge", summary="Acknowledge (stops escalation; audited)")
async def acknowledge_alert(
    alert_id: str, body: ActionIn, container: ContainerDep, principal: Reader
) -> dict[str, Any]:
    return await _act(container, principal, alert_id, "acknowledge", body.note)


@alert_router.post("/{alert_id}/resolve", summary="Resolve (operator; audited)")
async def resolve_alert(
    alert_id: str, body: ActionIn, container: ContainerDep, principal: Reader
) -> dict[str, Any]:
    return await _act(container, principal, alert_id, "resolve", body.note)


@alert_router.post(
    "/{alert_id}/suppress", summary="Suppress notifications for this alert (operator; audited)"
)
async def suppress_alert(
    alert_id: str, body: SuppressIn, container: ContainerDep, principal: Reader
) -> dict[str, Any]:
    until = datetime.now(UTC) + timedelta(hours=body.hours)
    return await _act(container, principal, alert_id, "suppress", body.note, until)


# ---------------------------------------------------------------------------- notifications
@notification_router.get("", summary="My notifications (in-app inbox), newest first")
async def list_notifications(
    container: ContainerDep,
    principal: Reader,
    unread: bool | None = None,
    severity: list[Severity] = Query(default=[]),
    category: list[Category] = Query(default=[]),
    device_id: str | None = Query(default=None, max_length=64),
    since: datetime | None = None,
    until: datetime | None = None,
    channel: Literal["in_app", "browser", "windows", "email"] = "in_app",
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    svc = _svc(container)
    f = NotificationFilter(
        svc.tenant_id,
        principal.subject,
        channel,
        unread,
        tuple(severity),
        tuple(category),
        ("DELIVERED", "READ") if channel == "in_app" else (),
        device_id,
        since,
        until,
    )
    items = await svc.repo.search_notifications(f, limit, offset)
    counts = await svc.repo.unread_count(svc.tenant_id, principal.subject)
    allowed = visible_devices(
        principal, container
    )  # current organisation only (a user may belong to several)
    return {
        "items": [n.to_dict() for n in items if n.device_id is None or n.device_id in allowed],
        "unread": sum(counts.values()),
        "unread_by_severity": counts,
        "limit": limit,
        "offset": offset,
        "server_time": datetime.now(UTC).isoformat(),
    }


@notification_router.get("/unread-count", summary="Unread in-app notifications (badge)")
async def unread_count(container: ContainerDep, principal: Reader) -> dict[str, Any]:
    svc = _svc(container)
    counts = await svc.repo.unread_count(svc.tenant_id, principal.subject)
    return {"unread": sum(counts.values()), "by_severity": counts}


async def _own(container: Any, principal: Any, notification_id: str) -> Notification:
    svc = _svc(container)
    n = await svc.repo.get_notification(notification_id[:64])
    if n is None or n.user_id != principal.subject or n.tenant_id != svc.tenant_id:
        raise _not_found("notification")
    return n


@notification_router.get("/{notification_id}", summary="One of my notifications")
async def get_notification(
    notification_id: str, container: ContainerDep, principal: Reader
) -> dict[str, Any]:
    return (await _own(container, principal, notification_id)).to_dict()


@notification_router.post("/{notification_id}/read", summary="Mark one of my notifications as read")
async def read_notification(
    notification_id: str, container: ContainerDep, principal: Reader
) -> dict[str, Any]:
    n = await _own(container, principal, notification_id)
    return (await _svc(container).mark_read(n, datetime.now(UTC))).to_dict()


@notification_router.post("/read-all", summary="Mark all my in-app notifications as read")
async def read_all(container: ContainerDep, principal: Reader) -> dict[str, Any]:
    svc = _svc(container)
    ids = await svc.repo.mark_all_read(svc.tenant_id, principal.subject, datetime.now(UTC))
    return {"marked": len(ids)}


# ------------------------------------------------------------------------------ preferences
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")


class QuietHoursIn(BaseModel):
    start: str
    end: str
    high: Literal["immediate", "defer"] = "immediate"
    medium: Literal["immediate", "defer"] = "defer"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM.match(v):
            raise ValueError("use HH:MM (24 h)")
        return v


class PreferencesIn(BaseModel):
    model_config = {"extra": "forbid"}

    channels: list[Literal["in_app", "browser", "windows", "email"]] = ["in_app", "browser"]
    severities: list[Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]] = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    categories: list[Category] = ["anomaly", "prediction", "connectivity", "security", "system"]
    frequency: Literal["immediate", "grouped", "digest"] = "immediate"
    digest_hour: int = Field(default=8, ge=0, le=23)
    timezone: str = Field(default="UTC", max_length=64)
    quiet_hours: QuietHoursIn | None = None
    email: str | None = Field(default=None, max_length=254)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown IANA time zone (e.g. Asia/Kolkata)") from exc
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v and not EMAIL.match(v):
            raise ValueError("invalid e-mail address")
        return v or None


@prefs_router.get("", summary="My notification preferences (+ which channels are available)")
async def get_preferences(container: ContainerDep, principal: Reader) -> dict[str, Any]:
    svc = _svc(container)
    p = await svc.preferences(principal.subject)
    return {
        "preferences": p.public(),
        "channels": svc.channels_status(),
        "frequencies": list(FREQUENCIES),
        "safeguards": {"critical_always_in_app": svc.policy.mandatory_critical_in_app},
    }


@prefs_router.put("", summary="Change my notification preferences")
async def put_preferences(body: PreferencesIn, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    if "email" in body.channels and not body.email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "e-mail channel needs an e-mail address")
    qh = QuietHours(**body.quiet_hours.model_dump()) if body.quiet_hours else None
    prefs = Preferences(
        tuple(dict.fromkeys(body.channels)),
        tuple(dict.fromkeys(body.severities)),
        tuple(dict.fromkeys(body.categories)),
        body.frequency,
        body.digest_hour,
        body.timezone,
        qh,
        body.email,
    )
    await _svc(container).set_preferences(principal.subject, prefs)
    return await get_preferences(container, principal)


# ------------------------------------------------------------------------------------ admin
class PolicyIn(BaseModel):
    model_config = {"extra": "forbid"}

    cooldown_s: float | None = Field(default=None, ge=0, le=86400)
    correlation_window_s: float | None = Field(default=None, ge=0, le=86400)
    expire_after_s: float | None = Field(default=None, ge=300, le=30 * 86400)
    group_window_s: float | None = Field(default=None, ge=30, le=86400)
    max_notifications_per_user_hour: int | None = Field(default=None, ge=1, le=1000)
    notify_on_resolution: bool | None = None
    mandatory_critical_in_app: bool | None = None
    escalation_severities: list[Severity] | None = None
    escalation: list[dict[str, Any]] | None = None
    prediction_bands: list[dict[str, Any]] | None = None
    rules: list[dict[str, Any]] | None = None


def _validate_policy(body: PolicyIn) -> dict[str, Any]:
    changes = body.model_dump(exclude_none=True)
    for step in changes.get("escalation", []):
        if (
            set(step) != {"after_s", "roles"}
            or not 60 <= float(step["after_s"]) <= 7 * 86400
            or not set(step["roles"]) <= {"operator", "admin"}
        ):
            raise ValueError("escalation steps: {after_s: 60..604800, roles: [operator|admin]}")
    for b in changes.get("prediction_bands", []):
        if (
            set(b) != {"max_eta_s", "severity"}
            or b["severity"] not in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
            or not 0 < float(b["max_eta_s"]) <= 365 * 86400
        ):
            raise ValueError("prediction_bands: {max_eta_s: seconds, severity: LOW..CRITICAL}")
    for r in changes.get("rules", []):
        if (
            not re.match(r"^[a-z0-9-]{1,40}$", str(r.get("rule_id", "")))
            or not set(r.get("sources", []))
            <= {"anomaly", "prediction", "connectivity", "device_health", "security"}
            or r.get("min_severity") not in ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
            or not 0 <= float(r.get("persistence_s", 0)) <= 86400
            or (r.get("min_confidence") is not None and not 0 <= float(r["min_confidence"]) <= 1)
            or set(r)
            - {"rule_id", "sources", "min_severity", "min_confidence", "persistence_s", "group", "enabled"}
        ):
            raise ValueError(
                "rules: {rule_id, sources, min_severity, min_confidence?, persistence_s?, group?, enabled?}"
            )
    return changes


@admin_router.get("/alert-policy", summary="Alert policy (admin)")
async def get_policy(container: ContainerDep, _: Admin) -> dict[str, Any]:
    svc = _svc(container)
    return {"policy": svc.policy.public(), **svc.config_meta}


@admin_router.put("/alert-policy", summary="Change the alert policy (admin; validated, versioned, audited)")
async def put_policy(body: PolicyIn, container: ContainerDep, admin: Admin) -> dict[str, Any]:
    svc = _svc(container)
    try:
        changes = _validate_policy(body)
        merged = svc.policy.merged(changes)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    stored = (await svc._store.get_setting("alert_policy")) or {}
    new = {
        "policy": {**(stored.get("policy") or {}), **changes},
        "version": int(stored.get("version") or 0) + 1,
        "updated_at": datetime.now(UTC).isoformat(),
        "updated_by": admin.subject,
    }
    await svc._store.set_setting("alert_policy", new, admin.subject)
    svc._apply_policy(new)
    del merged
    return {"policy": svc.policy.public(), **svc.config_meta}


class WebhookIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    url: str = Field(max_length=500)
    min_severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"
    categories: list[Category] = ["anomaly", "prediction", "connectivity", "security", "system"]
    enabled: bool = True


@admin_router.get("/notification-webhooks", summary="Webhook integrations (admin; secrets never returned)")
async def get_webhooks(container: ContainerDep, _: Admin) -> dict[str, Any]:
    svc = _svc(container)
    ok, why = svc.providers["webhook"].available()
    return {
        "targets": [
            {
                "name": t.name,
                "url": t.url,
                "min_severity": t.min_severity,
                "categories": list(t.categories),
                "enabled": t.enabled,
            }
            for t in svc.webhooks.values()
        ],
        "signing": {
            "configured": ok,
            "reason": why,
            "header": "X-LDT-Signature (sha256 HMAC of timestamp.body)",
        },
    }


@admin_router.put("/notification-webhooks", summary="Replace webhook integrations (admin; URLs validated)")
async def put_webhooks(body: list[WebhookIn], container: ContainerDep, admin: Admin) -> dict[str, Any]:
    svc = _svc(container)
    s = container.settings
    if len(body) > 20 or len({t.name for t in body}) != len(body):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "at most 20 targets with unique names")
    for t in body:
        bad = check_webhook_url(t.url, s.webhook_allow_http, s.webhook_allow_private)
        if bad:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{t.name}: {bad}")
    await svc._store.set_setting(
        "webhook_targets", {"targets": [t.model_dump() for t in body]}, admin.subject
    )
    await svc.load()
    return await get_webhooks(container, admin)


@admin_router.get("/alerting/stats", summary="Alert fatigue and delivery statistics (staff)")
async def alerting_stats(
    container: ContainerDep, principal: Staff, hours: int = Query(default=24, ge=1, le=24 * 90)
) -> dict[str, Any]:
    require_platform_scope(principal)  # platform-wide aggregates
    svc = _svc(container)
    stats = await svc.repo.stats(svc.tenant_id, datetime.now(UTC) - timedelta(hours=hours))
    return {
        "window_hours": hours,
        **stats,
        "engine": dict(svc.engine.stats),
        "worker": dict(svc.stats),
        "channels": svc.channels_status(),
        "open_alerts": len(svc.engine.open_alerts()),
    }


# ------------------------------------------------------------------------------------ agent
AgentDep = Annotated[AgentIdentity, Depends(require_agent)]


@agent_router.get("", summary="Windows notifications waiting for this device's agent")
async def agent_pull(
    container: ContainerDep, agent: AgentDep, device_id: str = Query(max_length=64)
) -> dict[str, Any]:
    if not agent.allows(device_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token is not valid for this device")
    items = await _svc(container).repo.pending_pickup(device_id, "windows", 10)
    return {
        "items": [
            {"notification_id": n.notification_id, "title": n.title, "body": n.body, "severity": n.severity}
            for n in items
        ]
    }


@agent_router.post("/{notification_id}/ack", summary="The agent showed the Windows notification")
async def agent_ack(notification_id: str, container: ContainerDep, agent: AgentDep) -> dict[str, Any]:
    svc = _svc(container)
    n = await svc.repo.get_notification(notification_id[:64])
    if n is None or n.channel != "windows" or not n.device_id or not agent.allows(n.device_id):
        raise _not_found("notification")
    await svc.agent_ack(n, datetime.now(UTC))
    return {"notification_id": n.notification_id, "status": n.status.value}


_ = CHANNEL_NAMES
