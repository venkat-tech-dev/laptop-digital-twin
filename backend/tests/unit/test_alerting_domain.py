"""Phase 6 - alerting domain: policy, dedupe, persistence, cooldown, hysteresis, correlation, quiet hours,
escalation, preferences, routing, retry schedule and state machines (deterministic: injected clock)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.alerting.delivery import MAX_ATTEMPTS, DeliveryResult, next_retry
from app.domain.alerting.engine import AlertEngine, dedupe_key
from app.domain.alerting.models import (
    AlertEvent,
    AlertStatus,
    InvalidTransitionError,
    Notification,
    NotificationStatus,
)
from app.domain.alerting.policy import AlertPolicy, Preferences, QuietHours
from app.domain.alerting.routing import Recipient, next_digest, quiet_until, recipients, route

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)


def ev(
    kind: str = "opened",
    severity: str = "HIGH",
    confidence: float | None = 0.9,
    source: str = "anomaly",
    condition: str = "behavior.cpu",
    device: str = "dev-1",
    at: datetime = T0,
    **kw,
) -> AlertEvent:
    return AlertEvent(
        source,
        f"src-{condition}",
        device,
        kind,
        kw.pop("alert_type", "behavioral_anomaly"),
        kw.pop("category", "anomaly" if source == "anomaly" else source),
        severity,
        confidence,
        kw.pop("title", "Unusually high CPU usage"),
        kw.pop("summary", "CPU 88 % vs usual 10-30 %"),
        condition,
        at,
        **kw,
    )


def engine(**policy) -> AlertEngine:
    ids = iter(range(1, 10_000))
    return AlertEngine(AlertPolicy(**policy), id_factory=lambda: f"al-{next(ids)}")


# ---------------------------------------------------------------- policy + dedupe + persistence
def test_policy_routes_by_severity_and_confidence() -> None:
    e = engine()
    assert e.ingest(ev(severity="INFO"), T0)[0].kind == "ignored"
    assert e.ingest(ev(severity="HIGH", confidence=0.3), T0)[0].kind == "ignored"  # below min confidence
    ch = e.ingest(
        ev(severity="CRITICAL", confidence=0.1, condition="c2"), T0
    )  # CRITICAL: no confidence floor
    assert ch[0].kind == "created" and ch[0].notify and ch[0].alert and ch[0].alert.priority == 1


def test_same_condition_updates_instead_of_duplicating() -> None:
    e = engine()
    first = e.ingest(ev(), T0)[0]
    assert first.kind == "created"
    for k in range(1, 4):
        ch = e.ingest(ev(kind="updated", at=T0 + timedelta(minutes=5 * k)), T0 + timedelta(minutes=5 * k))[0]
        assert ch.kind == "updated" and not ch.notify and ch.alert is first.alert
    assert len(e.open_alerts()) == 1 and first.alert and first.alert.occurrences == 4
    assert first.alert.status is AlertStatus.ONGOING and e.stats["deduplicated"] == 3
    closed = e.ingest(ev(kind="closed"), T0 + timedelta(minutes=30))[0]
    assert closed.kind == "resolved" and closed.alert and closed.alert.status is AlertStatus.RESOLVED
    assert e.open_alerts() == []


def test_severity_escalation_notifies_but_oscillation_does_not() -> None:
    e = engine()
    e.ingest(ev(severity="HIGH"), T0)
    up = e.ingest(ev(kind="updated", severity="CRITICAL"), T0 + timedelta(minutes=1))[0]
    assert up.notify and up.alert and up.alert.severity == "CRITICAL"
    # hysteresis: a value hovering around the limit (HIGH/CRITICAL/HIGH ...) is one alert, not a stream
    notes = [
        e.ingest(ev(kind="updated", severity=s), T0 + timedelta(minutes=2 + i))[0].notify
        for i, s in enumerate(["HIGH", "CRITICAL", "HIGH", "CRITICAL"])
    ]
    assert notes == [False, False, False, False] and len(e.open_alerts()) == 1


def test_medium_needs_persistence() -> None:
    e = engine()
    assert e.ingest(ev(severity="MEDIUM"), T0) == []  # pending
    assert e.ingest(ev(kind="updated", severity="MEDIUM"), T0 + timedelta(seconds=120)) == []
    ch = e.ingest(ev(kind="updated", severity="MEDIUM"), T0 + timedelta(seconds=301))
    assert ch[0].kind == "created" and ch[0].alert and ch[0].alert.first_detected_at == T0
    # a medium condition that ends before the persistence window never becomes an alert
    e2 = engine()
    e2.ingest(ev(severity="MEDIUM"), T0)
    assert e2.ingest(ev(kind="closed"), T0 + timedelta(seconds=60)) == []
    assert e2.ingest(ev(kind="updated", severity="MEDIUM"), T0 + timedelta(seconds=400)) == []


def test_cooldown_recurrence_is_visible_but_not_renotified() -> None:
    e = engine()
    e.ingest(ev(), T0)
    e.ingest(ev(kind="closed"), T0 + timedelta(minutes=5))
    again = e.ingest(ev(), T0 + timedelta(minutes=10))[0]
    assert again.kind == "created" and not again.notify and again.alert and again.alert.metadata["recurrence"]
    worse = engine()
    worse.ingest(ev(severity="HIGH"), T0)
    worse.ingest(ev(kind="closed"), T0 + timedelta(minutes=5))
    assert worse.ingest(ev(severity="CRITICAL"), T0 + timedelta(minutes=6))[0].notify  # more severe: notify
    later = engine()
    later.ingest(ev(), T0)
    later.ingest(ev(kind="closed"), T0 + timedelta(minutes=5))
    assert later.ingest(ev(), T0 + timedelta(minutes=40))[0].notify  # after the cooldown


def test_dedupe_key_is_deterministic_and_condition_specific() -> None:
    assert dedupe_key("t", ev()) == dedupe_key("t", ev(kind="updated", severity="LOW"))
    assert dedupe_key("t", ev()) != dedupe_key("t", ev(condition="behavior.memory"))
    assert dedupe_key("t", ev()) != dedupe_key("t", ev(device="dev-2"))
    assert dedupe_key("t", ev(threshold=90.0)) != dedupe_key("t", ev(threshold=95.0))
    assert dedupe_key("a", ev()) != dedupe_key("b", ev())  # tenant isolation


def test_related_alerts_share_a_correlation_key() -> None:
    e = engine()
    a = e.ingest(ev(condition="behavior.cpu"), T0)[0].alert
    b = e.ingest(ev(condition="behavior.temperature"), T0 + timedelta(minutes=3))[0].alert
    c = e.ingest(
        ev(condition="presence", source="connectivity", severity="MEDIUM", alert_type="agent_offline"),
        T0 + timedelta(minutes=4),
    )[0].alert
    assert a and b and c and a.correlation_key == b.correlation_key is not None
    assert c.correlation_key is None  # a different family is not grouped with performance


def test_prediction_bands_raise_but_never_lower_and_respect_confidence() -> None:
    p = AlertPolicy()
    assert p.prediction_severity("LOW", 600, "HIGH") == "HIGH"  # < 15 min
    assert p.prediction_severity("LOW", 1800, "MEDIUM") == "MEDIUM"  # < 1 h
    assert p.prediction_severity("INFO", 20 * 3600, "HIGH") == "LOW"  # < 24 h
    assert p.prediction_severity("MEDIUM", 9 * 86400, "HIGH") == "MEDIUM"  # source severity kept
    assert p.prediction_severity("HIGH", 600, "HIGH") == "HIGH"
    assert p.prediction_severity("LOW", 600, "LOW") == "LOW"  # low-confidence forecasts are not raised


# ---------------------------------------------------------------- escalation + expiry + user actions
def test_escalation_levels_stop_on_acknowledgement() -> None:
    e = engine()
    a = e.ingest(ev(severity="HIGH"), T0)[0].alert
    assert a is not None
    assert e.tick(T0 + timedelta(minutes=29)) == []
    lvl1 = e.tick(T0 + timedelta(minutes=31))
    assert [c.kind for c in lvl1] == ["escalated"] and lvl1[0].escalation_roles == ("operator",)
    assert e.tick(T0 + timedelta(minutes=32)) == []  # idempotent: the same level is not repeated
    lvl2 = e.tick(T0 + timedelta(minutes=61))
    assert lvl2[0].escalation_roles == ("admin",) and a.escalation_level == 2
    assert e.tick(T0 + timedelta(hours=5)) == []  # no more levels
    b = e.ingest(ev(severity="HIGH", condition="c2"), T0)[0].alert
    assert b is not None
    e.acknowledge(b, "ops", T0 + timedelta(minutes=10))
    assert [c for c in e.tick(T0 + timedelta(minutes=40)) if c.alert is b] == []
    assert b.status is AlertStatus.ACKNOWLEDGED and any(x.action == "acknowledged" for x in b.audit)


def test_expiry_suppression_and_invalid_transitions_are_audited() -> None:
    e = engine(expire_after_s=3600)
    a = e.ingest(ev(), T0)[0].alert
    assert a is not None
    s = e.suppress(a, "ops", T0 + timedelta(minutes=1), T0 + timedelta(hours=2), "maintenance")
    assert s.alert and s.alert.status is AlertStatus.SUPPRESSED and a.next_escalation_at is None
    with pytest.raises(InvalidTransitionError):
        e.acknowledge(a, "ops", T0 + timedelta(minutes=2))  # SUPPRESSED -> ACKNOWLEDGED is not allowed
    exp = e.tick(T0 + timedelta(hours=2))
    assert [c.kind for c in exp] == ["expired"] and a.status is AlertStatus.EXPIRED
    with pytest.raises(InvalidTransitionError):
        e.resolve(a, "ops", T0 + timedelta(hours=3))  # terminal
    assert [x.action for x in a.audit] == ["created", "suppressed", "expired"]


def test_notification_state_machine() -> None:
    n = Notification(
        "n1",
        "default",
        "a1",
        "u",
        "d",
        "in_app",
        NotificationStatus.PENDING,
        2,
        "HIGH",
        "anomaly",
        "t",
        "b",
        {},
        "k",
        T0,
        T0,
    )
    n.transition(NotificationStatus.SENDING, T0)
    n.transition(NotificationStatus.RETRYING, T0)
    n.transition(NotificationStatus.SENDING, T0)
    n.transition(NotificationStatus.DELIVERED, T0)
    n.transition(NotificationStatus.READ, T0)
    with pytest.raises(InvalidTransitionError):
        n.transition(NotificationStatus.PENDING, T0)
    assert [h.to_status for h in n.history] == ["SENDING", "RETRYING", "SENDING", "DELIVERED", "READ"]


# ---------------------------------------------------------------- routing, preferences, quiet hours
def test_quiet_hours_cross_midnight_in_the_users_time_zone() -> None:
    qh = QuietHours("22:00", "07:00")
    # 18:00 UTC = 23:30 in Kolkata (inside), ends 07:00 local = 01:30 UTC next day
    end = quiet_until(qh, "Asia/Kolkata", datetime(2026, 10, 8, 18, 0, tzinfo=UTC))
    assert end == datetime(2026, 10, 9, 1, 30, tzinfo=UTC)
    assert quiet_until(qh, "Asia/Kolkata", datetime(2026, 10, 8, 6, 0, tzinfo=UTC)) is None  # 11:30 local
    # 05:00 UTC = 01:00 New York (EDT) -> inside, ends 07:00 EDT = 11:00 UTC
    assert quiet_until(qh, "America/New_York", datetime(2026, 10, 8, 5, 0, tzinfo=UTC)) == datetime(
        2026, 10, 8, 11, 0, tzinfo=UTC
    )
    # DST end in New York (1 Nov 2026): 07:00 EST = 12:00 UTC
    assert quiet_until(qh, "America/New_York", datetime(2026, 11, 1, 8, 0, tzinfo=UTC)) == datetime(
        2026, 11, 1, 12, 0, tzinfo=UTC
    )
    assert (
        quiet_until(qh, "Not/A_Zone", datetime(2026, 10, 8, 23, 0, tzinfo=UTC)) is not None
    )  # falls back to UTC
    assert next_digest(8, "Asia/Kolkata", datetime(2026, 10, 8, 3, 0, tzinfo=UTC)) == datetime(
        2026, 10, 9, 2, 30, tzinfo=UTC
    )


def _alert(severity: str = "HIGH", category: str = "anomaly"):
    e = engine()
    e_ = ev(severity=severity, category=category, confidence=0.95, condition=f"{severity}{category}")
    out = e.ingest(e_, T0) or e.ingest(e_, T0 + timedelta(seconds=301))  # MEDIUM waits out its persistence
    a = out[0].alert
    assert a is not None
    return a


def test_routing_respects_preferences_quiet_hours_and_critical_safeguard() -> None:
    pol = AlertPolicy()
    u = Recipient("ana", "operator")
    night = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)  # 23:30 Kolkata
    prefs = Preferences(
        channels=("browser", "email"),
        timezone="Asia/Kolkata",
        email=None,
        quiet_hours=QuietHours("22:00", "07:00", high="defer", medium="defer"),
    )
    high = route(_alert("HIGH"), u, prefs, pol, night, group=False)
    assert [d.channel for d in high] == ["browser"]  # e-mail dropped: no address
    assert high[0].deliver_after == datetime(2026, 10, 9, 1, 30, tzinfo=UTC) and "quiet" in high[0].reason
    crit = route(_alert("CRITICAL"), u, prefs, pol, night, group=False)
    assert {d.channel for d in crit} == {"browser", "in_app"} and all(d.deliver_after is None for d in crit)
    muted = Preferences(severities=("CRITICAL",), categories=("security",))
    assert route(_alert("HIGH"), u, muted, pol, T0, group=False) == []
    grouped = route(_alert("LOW"), u, Preferences(), pol, T0, group=True)
    assert grouped[0].grouped and grouped[0].deliver_after == T0 + timedelta(seconds=pol.group_window_s)
    resolution = route(_alert("HIGH"), u, Preferences(channels=("browser", "in_app")), pol, T0, False, True)
    assert [d.channel for d in resolution] == ["in_app"]


def test_recipients_are_authorization_aware() -> None:
    users = [
        Recipient("own", "employee", owner=True),
        Recipient("other", "employee"),
        Recipient("v", "viewer"),
        Recipient("op", "operator"),
        Recipient("ad", "admin"),
    ]
    assert {u.user_id for u in recipients(_alert("MEDIUM"), users, None)} == {"own", "op", "ad"}
    assert {u.user_id for u in recipients(_alert("HIGH"), users, None)} == {"own", "v", "op", "ad"}
    assert {u.user_id for u in recipients(_alert("HIGH"), users, ("admin",))} == {"ad"}


# ---------------------------------------------------------------- retry
def test_retry_schedule_classification_and_dead_letter() -> None:
    rng = random.Random(1)
    t1 = next_retry(1, DeliveryResult(False, "transient"), T0, rng)
    t2 = next_retry(2, DeliveryResult(False, "transient"), T0, rng)
    t3 = next_retry(3, DeliveryResult(False, "transient"), T0, rng)
    assert t1 and t2 and t3
    assert 4 <= (t1 - T0).total_seconds() <= 6 and 24 <= (t2 - T0).total_seconds() <= 36
    assert 96 <= (t3 - T0).total_seconds() <= 144
    assert next_retry(MAX_ATTEMPTS, DeliveryResult(False, "transient"), T0) is None  # dead letter
    assert next_retry(1, DeliveryResult(False, "permanent"), T0) is None  # never retried
    rl = next_retry(1, DeliveryResult(False, "rate_limited", retry_after_s=90), T0, rng)
    assert rl and (rl - T0).total_seconds() >= 90


def test_waiting_for_a_session_is_deferred_not_a_failure() -> None:
    d = DeliveryResult(False, "deferred", "no open session", retry_after_s=60)
    for attempt in (1, 4, 50):
        at = next_retry(attempt, d, T0)
        assert at == T0 + timedelta(seconds=60)  # never a dead letter; the channel lifetime expires it
