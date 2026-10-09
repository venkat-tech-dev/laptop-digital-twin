"""Routing: who receives an alert, on which channels, and when (preferences, quiet hours, grouping).

Recipients are authorization-aware: a device alert only goes to users who may see the device
(staff roles, or the employee the device is assigned to). Preferences filter channels, severities and
categories; quiet hours and grouping only *defer* (``deliver_after``), they never drop a notification.

Quiet hours are evaluated in the user's own IANA time zone (handles midnight crossing and DST):
    LOW / MEDIUM -> deferred to the end of quiet hours (MEDIUM configurable), HIGH configurable,
    CRITICAL always immediate.
Frequency: immediate | grouped (held for the group window, then merged into one digest per user and
channel) | digest (held until the user's daily digest hour). CRITICAL is never grouped or deferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.alerting.models import Alert, sev_rank
from app.domain.alerting.policy import AlertPolicy, Preferences, QuietHours

STAFF_ROLES = ("viewer", "operator", "admin")


@dataclass(frozen=True, slots=True)
class Recipient:
    user_id: str
    role: str
    owner: bool = False  # the employee the device is assigned to


@dataclass(frozen=True, slots=True)
class Delivery:
    user_id: str
    channel: str
    deliver_after: datetime | None  # None = now
    grouped: bool
    reason: str


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def quiet_until(qh: QuietHours | None, tz: str, now: datetime) -> datetime | None:
    """End of the current quiet period (aware UTC datetime) or None when not in quiet hours."""
    if qh is None:
        return None
    zone = _zone(tz)
    local = now.astimezone(zone)
    start, end = _hm(qh.start), _hm(qh.end)
    t = local.time()
    if start == end:
        return None
    crosses_midnight = start > end
    inside = (t >= start or t < end) if crosses_midnight else (start <= t < end)
    if not inside:
        return None
    end_date = local.date()
    if crosses_midnight and t >= start:
        end_date = end_date + timedelta(days=1)
    end_local = datetime.combine(end_date, end, tzinfo=zone)
    return end_local.astimezone(now.tzinfo)


def next_digest(hour: int, tz: str, now: datetime) -> datetime:
    zone = _zone(tz)
    local = now.astimezone(zone)
    at = datetime.combine(local.date(), time(hour % 24, 0), tzinfo=zone)
    if at <= local:
        at = datetime.combine(local.date() + timedelta(days=1), time(hour % 24, 0), tzinfo=zone)
    return at.astimezone(now.tzinfo)


def recipients(alert: Alert, users: list[Recipient], level_roles: tuple[str, ...] | None) -> list[Recipient]:
    """Level 0 (``level_roles`` None): staff (operator/admin) + the device owner. Escalation levels:
    only the listed roles. Employees never receive alerts of devices that are not theirs."""
    out = []
    for u in users:
        if u.role == "employee":
            if u.owner and level_roles is None:
                out.append(u)
            continue
        if level_roles is None:
            if u.role in ("operator", "admin") or (
                u.role == "viewer" and sev_rank(alert.severity) >= sev_rank("HIGH")
            ):
                out.append(u)
        elif u.role in level_roles:
            out.append(u)
    return out


def route(
    alert: Alert,
    user: Recipient,
    prefs: Preferences,
    policy: AlertPolicy,
    now: datetime,
    group: bool,
    resolution: bool = False,
) -> list[Delivery]:
    critical = alert.severity == "CRITICAL"
    channels = list(prefs.channels)
    if critical and policy.mandatory_critical_in_app and "in_app" not in channels:
        channels.append("in_app")  # enterprise safeguard: critical alerts cannot be silently disabled
    if resolution:
        channels = [c for c in channels if c == "in_app"]
    if not critical and (alert.severity not in prefs.severities or alert.category not in prefs.categories):
        return []
    if "email" in channels and not prefs.email:
        channels.remove("email")
    out: list[Delivery] = []
    for ch in channels:
        if ch == "webhook":
            continue  # webhooks are organisation integrations (admin targets), not per-user channels
        after: datetime | None = None
        grouped = False
        reason = "immediate"
        if not critical and not resolution:
            q = quiet_until(prefs.quiet_hours, prefs.timezone, now)
            if q is not None and prefs.quiet_hours is not None:
                mode = {
                    "LOW": "defer",
                    "INFO": "defer",
                    "MEDIUM": prefs.quiet_hours.medium,
                    "HIGH": prefs.quiet_hours.high,
                }.get(alert.severity, "immediate")
                if mode == "defer":
                    after, reason = q, "quiet hours: deferred"
            if prefs.frequency == "digest":
                d = next_digest(prefs.digest_hour, prefs.timezone, now)
                after, grouped, reason = max(after or d, d), True, "daily digest"
            elif prefs.frequency == "grouped" or group:
                g = now + timedelta(seconds=policy.group_window_s)
                after, grouped, reason = max(after or g, g), True, "grouped"
        out.append(Delivery(user.user_id, ch, after, grouped, reason))
    return out
