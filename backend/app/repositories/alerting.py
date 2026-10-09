"""Storage for alerts, their audit trail, notifications (the durable delivery queue) and preferences.

The SQL implementation claims due notifications with ``FOR UPDATE SKIP LOCKED`` so several backend
replicas can run delivery workers without sending a notification twice; inserts are idempotent on
``idempotency_key``. Every query is bounded (pagination / limits) and served by an index.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.domain.alerting.models import (
    Alert,
    AlertStatus,
    AuditEntry,
    Notification,
    NotificationStatus,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import (
    AlertAuditRow,
    AlertRow,
    NotificationPreferenceRow,
    NotificationRow,
)

OPEN = ("OPEN", "ONGOING", "ACKNOWLEDGED", "SUPPRESSED")
DUE = ("PENDING", "QUEUED", "RETRYING")


@dataclass(frozen=True, slots=True)
class AlertFilter:
    tenant_id: str = "default"
    devices: frozenset[str] | None = None  # authorization: None = every device
    device_id: str | None = None
    severities: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    source_type: str | None = None
    since: datetime | None = None
    until: datetime | None = None


@dataclass(frozen=True, slots=True)
class NotificationFilter:
    tenant_id: str = "default"
    user_id: str = ""
    channel: str | None = "in_app"
    unread: bool | None = None
    severities: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    device_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None


class AlertRepository(Protocol):
    async def save_alert(self, alert: Alert) -> None: ...
    async def get_alert(self, alert_id: str) -> Alert | None: ...
    async def search_alerts(self, f: AlertFilter, limit: int, offset: int = 0) -> list[Alert]: ...
    async def open_alerts(self, tenant_id: str) -> list[Alert]: ...
    async def insert_notification(self, n: Notification) -> bool: ...
    async def insert_notifications(self, items: list[Notification]) -> list[Notification]: ...
    async def save_notification(self, n: Notification) -> None: ...
    async def save_notifications(self, items: list[Notification]) -> None: ...
    async def get_notification(self, notification_id: str) -> Notification | None: ...
    async def search_notifications(
        self, f: NotificationFilter, limit: int, offset: int = 0
    ) -> list[Notification]: ...
    async def unread_count(self, tenant_id: str, user_id: str) -> dict[str, int]: ...
    async def mark_all_read(self, tenant_id: str, user_id: str, now: datetime) -> list[str]: ...
    async def claim_due(
        self, now: datetime, limit: int, channels: tuple[str, ...] | None = None
    ) -> list[Notification]: ...
    async def notifications_for_alert(self, alert_id: str) -> list[Notification]: ...
    async def pending_pickup(self, device_id: str, channel: str, limit: int) -> list[Notification]: ...
    async def get_preferences(self, user_id: str) -> dict[str, Any] | None: ...
    async def set_preferences(
        self, tenant_id: str, user_id: str, prefs: dict[str, Any], now: datetime
    ) -> None: ...
    async def purge_closed_before(self, cutoff: datetime) -> int: ...
    async def stats(self, tenant_id: str, since: datetime) -> dict[str, Any]: ...
    async def requeue_stuck(self, claimed_before: datetime, now: datetime) -> int: ...
    async def backlog(self, now: datetime) -> tuple[int, float | None]: ...


# ------------------------------------------------------------------------------------- memory
def _alert_match(a: Alert, f: AlertFilter) -> bool:
    return (
        a.tenant_id == f.tenant_id
        and (f.devices is None or a.device_id in f.devices)
        and (f.device_id is None or a.device_id == f.device_id)
        and (not f.severities or a.severity in f.severities)
        and (not f.categories or a.category in f.categories)
        and (not f.statuses or a.status.value in f.statuses)
        and (f.source_type is None or a.source_type == f.source_type)
        and (f.since is None or a.created_at >= f.since)
        and (f.until is None or a.created_at <= f.until)
    )


def _notif_match(n: Notification, f: NotificationFilter) -> bool:
    return (
        n.tenant_id == f.tenant_id
        and n.user_id == f.user_id
        and (f.channel is None or n.channel == f.channel)
        and (f.unread is None or (n.read_at is None) == f.unread)
        and (not f.severities or n.severity in f.severities)
        and (not f.categories or n.category in f.categories)
        and (not f.statuses or n.status.value in f.statuses)
        and (f.device_id is None or n.device_id == f.device_id)
        and (f.since is None or n.created_at >= f.since)
        and (f.until is None or n.created_at <= f.until)
    )


def _due(n: Notification, now: datetime, channels: tuple[str, ...] | None) -> bool:
    return (
        n.status.value in DUE
        and (n.deliver_after is None or n.deliver_after <= now)
        and (n.next_retry_at is None or n.next_retry_at <= now)
        and (channels is None or n.channel in channels)
    )


class MemoryAlertRepository:
    def __init__(self, max_items: int = 20_000) -> None:
        self.alerts: dict[str, Alert] = {}
        self.notifications: dict[str, Notification] = {}
        self.keys: set[str] = set()
        self.prefs: dict[str, dict[str, Any]] = {}
        self._max = max_items
        self._open_keys: dict[tuple[str, str], str] = {}  # (tenant, dedupe key) -> open alert id
        self._due: set[str] = set()  # notifications that may need delivery

    async def save_alert(self, alert: Alert) -> None:
        key = (alert.tenant_id, alert.deduplication_key)
        holder = self._open_keys.get(key)
        if alert.is_open:
            if holder is not None and holder != alert.alert_id:
                raise ValueError("duplicate open alert for the same condition")
            self._open_keys[key] = alert.alert_id
        elif holder == alert.alert_id:
            del self._open_keys[key]
        self.alerts[alert.alert_id] = alert

    async def get_alert(self, alert_id: str) -> Alert | None:
        return self.alerts.get(alert_id)

    async def search_alerts(self, f: AlertFilter, limit: int, offset: int = 0) -> list[Alert]:
        rows = sorted(
            (a for a in self.alerts.values() if _alert_match(a, f)), key=lambda a: a.created_at, reverse=True
        )
        return rows[offset : offset + limit]

    async def open_alerts(self, tenant_id: str) -> list[Alert]:
        return [a for a in self.alerts.values() if a.tenant_id == tenant_id and a.is_open]

    async def insert_notifications(self, items: list[Notification]) -> list[Notification]:
        return [n for n in items if await self.insert_notification(n)]

    async def insert_notification(self, n: Notification) -> bool:
        if n.idempotency_key in self.keys:
            return False
        self.keys.add(n.idempotency_key)
        self.notifications[n.notification_id] = n
        self._track(n)
        if len(self.notifications) > self._max:
            oldest = min(self.notifications.values(), key=lambda x: x.created_at)
            self.notifications.pop(oldest.notification_id, None)
        return True

    async def save_notification(self, n: Notification) -> None:
        self.notifications[n.notification_id] = n
        self._track(n)

    def _track(self, n: Notification) -> None:
        if n.status.value in DUE:
            self._due.add(n.notification_id)
        else:
            self._due.discard(n.notification_id)

    async def save_notifications(self, items: list[Notification]) -> None:
        for n in items:
            await self.save_notification(n)

    async def get_notification(self, notification_id: str) -> Notification | None:
        return self.notifications.get(notification_id)

    async def search_notifications(
        self, f: NotificationFilter, limit: int, offset: int = 0
    ) -> list[Notification]:
        rows = sorted(
            (n for n in self.notifications.values() if _notif_match(n, f)),
            key=lambda n: n.created_at,
            reverse=True,
        )
        return rows[offset : offset + limit]

    async def unread_count(self, tenant_id: str, user_id: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for n in self.notifications.values():
            if (
                n.tenant_id == tenant_id
                and n.user_id == user_id
                and n.channel == "in_app"
                and n.read_at is None
                and n.status in (NotificationStatus.DELIVERED,)
            ):
                out[n.severity] = out.get(n.severity, 0) + 1
        return out

    async def mark_all_read(self, tenant_id: str, user_id: str, now: datetime) -> list[str]:
        ids = []
        for n in self.notifications.values():
            if (
                n.tenant_id == tenant_id
                and n.user_id == user_id
                and n.channel == "in_app"
                and n.read_at is None
                and n.status is NotificationStatus.DELIVERED
            ):
                n.transition(NotificationStatus.READ, now, "mark all read")
                n.read_at = now
                ids.append(n.notification_id)
        return ids

    async def claim_due(
        self, now: datetime, limit: int, channels: tuple[str, ...] | None = None
    ) -> list[Notification]:
        cands = [self.notifications[i] for i in self._due if i in self.notifications]
        out: list[Notification] = []
        for n in sorted(
            (c for c in cands if _due(c, now, channels)), key=lambda x: (x.priority, x.created_at)
        ):
            if len(out) >= limit:
                break
            n.transition(NotificationStatus.SENDING, now, "claimed by the delivery worker")
            self._due.discard(n.notification_id)
            out.append(n)
        return out

    async def requeue_stuck(self, claimed_before: datetime, now: datetime) -> int:
        count = 0
        for n in self.notifications.values():
            if n.status is NotificationStatus.SENDING and n.updated_at < claimed_before:
                n.transition(
                    NotificationStatus.RETRYING, now, "recovered: delivery interrupted while SENDING"
                )
                n.next_retry_at = now
                self._due.add(n.notification_id)
                count += 1
        return count

    async def backlog(self, now: datetime) -> tuple[int, float | None]:
        due = [self.notifications[i] for i in self._due if i in self.notifications]
        due = [n for n in due if n.status.value in DUE]
        oldest = min((n.created_at for n in due), default=None)
        return len(due), (now - oldest).total_seconds() if oldest else None

    async def notifications_for_alert(self, alert_id: str) -> list[Notification]:
        return sorted(
            (n for n in self.notifications.values() if n.alert_id == alert_id), key=lambda n: n.created_at
        )

    async def pending_pickup(self, device_id: str, channel: str, limit: int) -> list[Notification]:
        return [
            n
            for n in self.notifications.values()
            if n.device_id == device_id and n.channel == channel and n.status is NotificationStatus.QUEUED
        ][:limit]

    async def get_preferences(self, user_id: str) -> dict[str, Any] | None:
        return self.prefs.get(user_id)

    async def set_preferences(
        self, tenant_id: str, user_id: str, prefs: dict[str, Any], now: datetime
    ) -> None:
        self.prefs[user_id] = prefs

    async def purge_closed_before(self, cutoff: datetime) -> int:
        old = [
            k for k, n in self.notifications.items() if n.created_at < cutoff and n.status.value not in DUE
        ]
        for k in old:
            del self.notifications[k]
        return len(old)

    async def stats(self, tenant_id: str, since: datetime) -> dict[str, Any]:
        alerts = [a for a in self.alerts.values() if a.tenant_id == tenant_id and a.created_at >= since]
        notes = [n for n in self.notifications.values() if n.tenant_id == tenant_id and n.created_at >= since]
        return _stats(alerts, notes)


# ---------------------------------------------------------------------------------------- SQL
def _alert_values(a: Alert) -> dict[str, Any]:
    return {
        "id": a.alert_id,
        "tenant_id": a.tenant_id,
        "device_id": a.device_id,
        "event_id": a.event_id[:64],
        "source_type": a.source_type,
        "alert_type": a.alert_type,
        "category": a.category,
        "severity": a.severity,
        "title": a.title[:200],
        "summary": a.summary,
        "description": a.description,
        "status": a.status.value,
        "priority": a.priority,
        "confidence": a.confidence,
        "deduplication_key": a.deduplication_key,
        "correlation_key": a.correlation_key,
        "first_detected_at": a.first_detected_at,
        "last_updated_at": a.last_updated_at,
        "acknowledged_at": a.acknowledged_at,
        "acknowledged_by": a.acknowledged_by,
        "resolved_at": a.resolved_at,
        "resolved_by": a.resolved_by,
        "suppressed_at": a.suppressed_at,
        "suppressed_by": a.suppressed_by,
        "suppressed_until": a.suppressed_until,
        "expires_at": a.expires_at,
        "escalation_level": a.escalation_level,
        "next_escalation_at": a.next_escalation_at,
        "occurrences": a.occurrences,
        "metadata": a.metadata,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
    }


def _alert_from(r: AlertRow, audit: list[AlertAuditRow]) -> Alert:
    return Alert(
        alert_id=r.id,
        tenant_id=r.tenant_id,
        device_id=r.device_id,
        event_id=r.event_id,
        source_type=r.source_type,
        alert_type=r.alert_type,
        category=r.category,
        severity=r.severity,
        title=r.title,
        summary=r.summary,
        status=AlertStatus(r.status),
        priority=r.priority,
        confidence=r.confidence,
        deduplication_key=r.deduplication_key,
        correlation_key=r.correlation_key,
        first_detected_at=r.first_detected_at,
        last_updated_at=r.last_updated_at,
        created_at=r.created_at,
        updated_at=r.updated_at,
        description=r.description,
        acknowledged_at=r.acknowledged_at,
        acknowledged_by=r.acknowledged_by,
        resolved_at=r.resolved_at,
        resolved_by=r.resolved_by,
        suppressed_at=r.suppressed_at,
        suppressed_by=r.suppressed_by,
        suppressed_until=r.suppressed_until,
        expires_at=r.expires_at,
        escalation_level=r.escalation_level,
        next_escalation_at=r.next_escalation_at,
        occurrences=r.occurrences,
        metadata=r.metadata_ or {},
        audit=[AuditEntry(x.at, x.actor, x.action, x.from_status, x.to_status, x.detail) for x in audit],
    )


_NOTIF_COLS = (
    "tenant_id",
    "alert_id",
    "user_id",
    "device_id",
    "channel",
    "priority",
    "severity",
    "category",
    "title",
    "body",
    "payload",
    "idempotency_key",
    "provider",
    "provider_message_id",
    "attempt_count",
    "next_retry_at",
    "deliver_after",
    "delivered_at",
    "read_at",
    "failed_at",
    "failure_reason",
    "last_error",
    "escalation_level",
    "created_at",
    "updated_at",
)


def _notif_values(n: Notification) -> dict[str, Any]:
    v = {c: getattr(n, c) for c in _NOTIF_COLS}
    v.update(
        id=n.notification_id,
        status=n.status.value,
        title=n.title[:200],
        history=[h.public() for h in n.history[-30:]],
    )
    return v


def _notif_from(r: NotificationRow) -> Notification:
    kw = {c: getattr(r, c) for c in _NOTIF_COLS}
    kw["payload"] = r.payload or {}
    n = Notification(notification_id=r.id, status=NotificationStatus(r.status), **kw)
    for h in r.history or []:
        n.history.append(
            AuditEntry(
                datetime.fromisoformat(h["at"]),
                h["actor"],
                h["action"],
                h.get("from_status"),
                h.get("to_status"),
                h.get("detail"),
            )
        )
    return n


class SqlAlertRepository:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._saved_audit: dict[str, int] = {}

    async def save_alert(self, alert: Alert) -> None:
        values = _alert_values(alert)
        stmt = pg_insert(AlertRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[AlertRow.id], set_={k: stmt.excluded[k] for k in values if k != "id"}
        )
        done = self._saved_audit.get(alert.alert_id, 0)
        new_audit = alert.audit[done:]
        async with self._db.sessions.begin() as session:
            await session.execute(stmt)
            for e in new_audit:
                session.add(
                    AlertAuditRow(
                        alert_id=alert.alert_id,
                        at=e.at,
                        actor=e.actor[:64],
                        action=e.action[:32],
                        from_status=e.from_status,
                        to_status=e.to_status,
                        detail=e.detail,
                    )
                )
        self._saved_audit[alert.alert_id] = len(alert.audit)
        if not alert.is_open:
            self._saved_audit.pop(alert.alert_id, None)

    async def _audit(self, session: Any, alert_ids: list[str]) -> dict[str, list[AlertAuditRow]]:
        if not alert_ids:
            return {}
        rows = (
            (
                await session.execute(
                    select(AlertAuditRow)
                    .where(AlertAuditRow.alert_id.in_(alert_ids))
                    .order_by(AlertAuditRow.at, AlertAuditRow.id)
                )
            )
            .scalars()
            .all()
        )
        out: dict[str, list[AlertAuditRow]] = {}
        for r in rows:
            out.setdefault(r.alert_id, []).append(r)
        return out

    async def get_alert(self, alert_id: str) -> Alert | None:
        async with self._db.sessions() as session:
            row = await session.get(AlertRow, alert_id)
            if row is None:
                return None
            audit = await self._audit(session, [alert_id])
        a = _alert_from(row, audit.get(alert_id, []))
        self._saved_audit.setdefault(a.alert_id, len(a.audit))
        return a

    async def search_alerts(self, f: AlertFilter, limit: int, offset: int = 0) -> list[Alert]:
        stmt = select(AlertRow).where(AlertRow.tenant_id == f.tenant_id)
        if f.devices is not None:
            stmt = stmt.where(AlertRow.device_id.in_(sorted(f.devices) or ["-"]))
        if f.device_id:
            stmt = stmt.where(AlertRow.device_id == f.device_id)
        if f.severities:
            stmt = stmt.where(AlertRow.severity.in_(f.severities))
        if f.categories:
            stmt = stmt.where(AlertRow.category.in_(f.categories))
        if f.statuses:
            stmt = stmt.where(AlertRow.status.in_(f.statuses))
        if f.source_type:
            stmt = stmt.where(AlertRow.source_type == f.source_type)
        if f.since:
            stmt = stmt.where(AlertRow.created_at >= f.since)
        if f.until:
            stmt = stmt.where(AlertRow.created_at <= f.until)
        stmt = stmt.order_by(AlertRow.created_at.desc()).offset(offset).limit(limit)
        async with self._db.sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_alert_from(r, []) for r in rows]  # list view: audit loaded on detail only

    async def open_alerts(self, tenant_id: str) -> list[Alert]:
        async with self._db.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(AlertRow)
                        .where(AlertRow.tenant_id == tenant_id, AlertRow.status.in_(OPEN))
                        .limit(50_000)
                    )
                )
                .scalars()
                .all()
            )
            audit = await self._audit(session, [r.id for r in rows])
        out = [_alert_from(r, audit.get(r.id, [])) for r in rows]
        for a in out:
            self._saved_audit[a.alert_id] = len(a.audit)
        return out

    async def insert_notifications(self, items: list[Notification]) -> list[Notification]:
        """Idempotent multi-insert in one transaction; returns the rows that were new."""
        if not items:
            return []
        inserted: list[Notification] = []
        async with self._db.sessions.begin() as session:
            for n in items:
                stmt = (
                    pg_insert(NotificationRow)
                    .values(**_notif_values(n))
                    .on_conflict_do_nothing(index_elements=[NotificationRow.idempotency_key])
                )
                if getattr(await session.execute(stmt), "rowcount", 0):
                    inserted.append(n)
        return inserted

    async def insert_notification(self, n: Notification) -> bool:
        stmt = (
            pg_insert(NotificationRow)
            .values(**_notif_values(n))
            .on_conflict_do_nothing(index_elements=[NotificationRow.idempotency_key])
        )
        async with self._db.sessions.begin() as session:
            result = await session.execute(stmt)
        return bool(getattr(result, "rowcount", 0))

    async def save_notification(self, n: Notification) -> None:
        values = _notif_values(n)
        async with self._db.sessions.begin() as session:
            await session.execute(
                update(NotificationRow)
                .where(NotificationRow.id == n.notification_id)
                .values(**{k: v for k, v in values.items() if k != "id"})
            )

    async def save_notifications(self, items: list[Notification]) -> None:
        """One transaction for many (digest members, cancellations)."""
        if not items:
            return
        async with self._db.sessions.begin() as session:
            for n in items:
                values = _notif_values(n)
                await session.execute(
                    update(NotificationRow)
                    .where(NotificationRow.id == n.notification_id)
                    .values(**{k: v for k, v in values.items() if k != "id"})
                )

    async def get_notification(self, notification_id: str) -> Notification | None:
        async with self._db.sessions() as session:
            row = await session.get(NotificationRow, notification_id)
        return _notif_from(row) if row else None

    async def search_notifications(
        self, f: NotificationFilter, limit: int, offset: int = 0
    ) -> list[Notification]:
        stmt = select(NotificationRow).where(
            NotificationRow.tenant_id == f.tenant_id, NotificationRow.user_id == f.user_id
        )
        if f.channel:
            stmt = stmt.where(NotificationRow.channel == f.channel)
        if f.unread is not None:
            stmt = stmt.where(
                NotificationRow.read_at.is_(None) if f.unread else NotificationRow.read_at.is_not(None)
            )
        if f.severities:
            stmt = stmt.where(NotificationRow.severity.in_(f.severities))
        if f.categories:
            stmt = stmt.where(NotificationRow.category.in_(f.categories))
        if f.statuses:
            stmt = stmt.where(NotificationRow.status.in_(f.statuses))
        if f.device_id:
            stmt = stmt.where(NotificationRow.device_id == f.device_id)
        if f.since:
            stmt = stmt.where(NotificationRow.created_at >= f.since)
        if f.until:
            stmt = stmt.where(NotificationRow.created_at <= f.until)
        stmt = stmt.order_by(NotificationRow.created_at.desc()).offset(offset).limit(limit)
        async with self._db.sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_notif_from(r) for r in rows]

    async def unread_count(self, tenant_id: str, user_id: str) -> dict[str, int]:
        stmt = (
            select(NotificationRow.severity, func.count())
            .where(
                NotificationRow.tenant_id == tenant_id,
                NotificationRow.user_id == user_id,
                NotificationRow.channel == "in_app",
                NotificationRow.read_at.is_(None),
                NotificationRow.status == "DELIVERED",
            )
            .group_by(NotificationRow.severity)
        )
        async with self._db.sessions() as session:
            rows = (await session.execute(stmt)).all()
        return {str(s): int(c) for s, c in rows}

    async def mark_all_read(self, tenant_id: str, user_id: str, now: datetime) -> list[str]:
        cond = and_(
            NotificationRow.tenant_id == tenant_id,
            NotificationRow.user_id == user_id,
            NotificationRow.channel == "in_app",
            NotificationRow.read_at.is_(None),
            NotificationRow.status == "DELIVERED",
        )
        async with self._db.sessions.begin() as session:
            ids = (
                (await session.execute(select(NotificationRow.id).where(cond).limit(10_000))).scalars().all()
            )
            if ids:
                await session.execute(
                    update(NotificationRow)
                    .where(NotificationRow.id.in_(ids))
                    .values(read_at=now, status="READ", updated_at=now)
                )
        return list(ids)

    async def claim_due(
        self, now: datetime, limit: int, channels: tuple[str, ...] | None = None
    ) -> list[Notification]:
        cond = and_(
            NotificationRow.status.in_(DUE),
            or_(NotificationRow.deliver_after.is_(None), NotificationRow.deliver_after <= now),
            or_(NotificationRow.next_retry_at.is_(None), NotificationRow.next_retry_at <= now),
        )
        if channels:
            cond = and_(cond, NotificationRow.channel.in_(channels))
        async with self._db.sessions.begin() as session:
            rows = (
                (
                    await session.execute(
                        select(NotificationRow)
                        .where(cond)
                        .order_by(NotificationRow.priority, NotificationRow.created_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            out = [_notif_from(r) for r in rows]
            for n in out:
                n.transition(NotificationStatus.SENDING, now, "claimed by the delivery worker")
            if out:
                await session.execute(
                    update(NotificationRow)
                    .where(NotificationRow.id.in_([n.notification_id for n in out]))
                    .values(status="SENDING", updated_at=now)
                )
        return out

    async def requeue_stuck(self, claimed_before: datetime, now: datetime) -> int:
        """A worker that died mid-delivery leaves rows in SENDING: return them to RETRYING (at-least-once)."""
        async with self._db.sessions.begin() as session:
            res = await session.execute(
                update(NotificationRow)
                .where(NotificationRow.status == "SENDING", NotificationRow.updated_at < claimed_before)
                .values(status="RETRYING", next_retry_at=now, updated_at=now)
            )
        return int(getattr(res, "rowcount", 0) or 0)

    async def backlog(self, now: datetime) -> tuple[int, float | None]:
        async with self._db.sessions() as session:
            row = (
                await session.execute(
                    select(func.count(), func.min(NotificationRow.created_at)).where(
                        NotificationRow.status.in_(DUE)
                    )
                )
            ).one()
        count, oldest = int(row[0] or 0), row[1]
        return count, (now - oldest).total_seconds() if oldest else None

    async def notifications_for_alert(self, alert_id: str) -> list[Notification]:
        async with self._db.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(NotificationRow)
                        .where(NotificationRow.alert_id == alert_id)
                        .order_by(NotificationRow.created_at)
                        .limit(500)
                    )
                )
                .scalars()
                .all()
            )
        return [_notif_from(r) for r in rows]

    async def pending_pickup(self, device_id: str, channel: str, limit: int) -> list[Notification]:
        async with self._db.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(NotificationRow)
                        .where(
                            NotificationRow.device_id == device_id,
                            NotificationRow.channel == channel,
                            NotificationRow.status == "QUEUED",
                        )
                        .order_by(NotificationRow.created_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [_notif_from(r) for r in rows]

    async def get_preferences(self, user_id: str) -> dict[str, Any] | None:
        async with self._db.sessions() as session:
            row = await session.get(NotificationPreferenceRow, user_id)
        return dict(row.preferences) if row else None

    async def set_preferences(
        self, tenant_id: str, user_id: str, prefs: dict[str, Any], now: datetime
    ) -> None:
        stmt = pg_insert(NotificationPreferenceRow).values(
            user_id=user_id, tenant_id=tenant_id, preferences=prefs, updated_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[NotificationPreferenceRow.user_id],
            set_={"preferences": stmt.excluded.preferences, "updated_at": now},
        )
        async with self._db.sessions.begin() as session:
            await session.execute(stmt)

    async def purge_closed_before(self, cutoff: datetime) -> int:
        async with self._db.sessions.begin() as session:
            r1 = await session.execute(
                delete(NotificationRow).where(
                    NotificationRow.created_at < cutoff, NotificationRow.status.not_in(DUE)
                )
            )
            r2 = await session.execute(
                delete(AlertRow).where(AlertRow.created_at < cutoff, AlertRow.status.not_in(OPEN))
            )
        return int(getattr(r1, "rowcount", 0) or 0) + int(getattr(r2, "rowcount", 0) or 0)

    async def stats(self, tenant_id: str, since: datetime) -> dict[str, Any]:
        async with self._db.sessions() as session:
            alerts = (
                (
                    await session.execute(
                        select(AlertRow)
                        .where(AlertRow.tenant_id == tenant_id, AlertRow.created_at >= since)
                        .limit(50_000)
                    )
                )
                .scalars()
                .all()
            )
            notes = (
                (
                    await session.execute(
                        select(NotificationRow)
                        .where(NotificationRow.tenant_id == tenant_id, NotificationRow.created_at >= since)
                        .limit(200_000)
                    )
                )
                .scalars()
                .all()
            )
        return _stats([_alert_from(a, []) for a in alerts], [_notif_from(n) for n in notes])


def _stats(alerts: list[Alert], notes: list[Notification]) -> dict[str, Any]:
    """Alert-fatigue indicators (counts, rates, acknowledgement/resolution times)."""
    by_device: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    for a in alerts:
        by_device[a.device_id] = by_device.get(a.device_id, 0) + 1
        by_sev[a.severity] = by_sev.get(a.severity, 0) + 1
    acked = [a for a in alerts if a.acknowledged_at]
    resolved = [a for a in alerts if a.resolved_at]
    per_user: dict[str, int] = {}
    for n in notes:
        if n.status.value in ("DELIVERED", "READ"):
            per_user[n.user_id] = per_user.get(n.user_id, 0) + 1
    delivered = sum(1 for n in notes if n.status.value in ("DELIVERED", "READ"))
    failed = sum(1 for n in notes if n.status is NotificationStatus.FAILED)
    total_occ = sum(a.occurrences for a in alerts)
    return {
        "alerts": len(alerts),
        "alerts_by_device": by_device,
        "alerts_by_severity": by_sev,
        "deduplicated_events": total_occ - len(alerts),
        "deduplication_rate": round((total_occ - len(alerts)) / total_occ, 3) if total_occ else None,
        "suppressed": sum(1 for a in alerts if a.status is AlertStatus.SUPPRESSED or a.suppressed_at),
        "acknowledgement_rate": round(len(acked) / len(alerts), 3) if alerts else None,
        "escalated": sum(1 for a in alerts if a.escalation_level > 0),
        "mean_ack_s": round(
            sum((a.acknowledged_at - a.created_at).total_seconds() for a in acked if a.acknowledged_at)
            / len(acked)
        )
        if acked
        else None,
        "mean_resolution_s": round(
            sum((a.resolved_at - a.created_at).total_seconds() for a in resolved if a.resolved_at)
            / len(resolved)
        )
        if resolved
        else None,
        "notifications": len(notes),
        "notifications_delivered": delivered,
        "notifications_failed": failed,
        "delivery_failure_rate": round(failed / (delivered + failed), 3) if delivered + failed else None,
        "notifications_per_user": per_user,
    }
