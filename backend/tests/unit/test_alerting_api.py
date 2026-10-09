"""Phase 6 - integration (event -> alert -> notification -> provider -> inbox / WebSocket / timeline),
failure scenarios and security of the alerting APIs."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.domain.alerting.delivery import DeliveryResult, NotificationProvider
from app.domain.alerting.models import Notification
from app.domain.events.events import AnomalyChanged, PredictionChanged, PresenceChanged
from app.main import create_app
from app.services.alerting import AlertService, from_anomaly
from tests.conftest import AGENT_KEY, DEVICE, INVENTORY, settings

KEY = {"X-Agent-Key": AGENT_KEY}
PASSWORD = "Correct-Horse-9-Battery"
SECRET = "test-signing-secret-0123456789"


def _inventory(c: TestClient, device: str = DEVICE) -> None:
    env = {
        "device_id": device,
        "agent_version": "t",
        "discovered_at": datetime.now(UTC).isoformat(),
        "inventory": INVENTORY,
    }
    assert c.post("/api/v1/ingest/inventory", json=env, headers=KEY).status_code in (200, 202)


def anomaly(
    kind: str = "detected",
    level: str = "HIGH",
    device: str = DEVICE,
    aid: str = "an-1",
    rule: str = "behavior.cpu",
    confidence: float = 0.9,
) -> AnomalyChanged:
    return AnomalyChanged(
        device_id=device,
        kind=kind,
        anomaly={
            "anomaly_id": aid,
            "anomaly_type": "behavioral_anomaly",
            "level": level,
            "confidence": confidence,
            "title": "Unusually high cpu usage",
            "message": "CPU usage is 88% (2-minute average) for 4 min; usually 10-30%.",
            "rule_id": rule,
            "metric_key": "performance.cpu.usage_percent",
            "value": 88.0,
            "threshold": 46.0,
            "expected_value": 20.0,
            "expected_min": 11.0,
            "expected_max": 29.0,
            "lifecycle": "ONGOING",
            "started_at": datetime.now(UTC).isoformat(),
            "evidence": {"summary": "CPU 88% vs usual 11-29%"},
        },
    )


def svc_of(c: TestClient) -> AlertService:
    svc = c.app.state.container.alerts  # type: ignore[attr-defined]
    assert svc is not None
    return svc


def publish(c: TestClient, *events: Any) -> None:
    c.portal.call(c.app.state.container.bus.publish_all, list(events))  # type: ignore[attr-defined,union-attr]
    c.portal.call(svc_of(c).drain)  # type: ignore[union-attr]  # events are processed off the bus


def deliver(c: TestClient, at: datetime | None = None) -> int:
    return c.portal.call(svc_of(c).deliver_due, at or datetime.now(UTC))  # type: ignore[union-attr]


def _until(ws: Any, event: str, limit: int = 60) -> dict[str, Any]:
    for _ in range(limit):
        m = ws.receive_json()
        if m["event"] == event:
            return m
    raise AssertionError(event)


@pytest.fixture
def client() -> Iterator[TestClient]:
    s = settings(WEBHOOK_SIGNING_SECRET=SECRET, WEBHOOK_ALLOW_HTTP=True, WEBHOOK_ALLOW_PRIVATE=True)
    with TestClient(create_app(s)) as c:
        _inventory(c)
        yield c


def test_anomaly_to_alert_to_inbox_websocket_and_timeline(client: TestClient) -> None:
    c = client
    with c.websocket_connect("/ws/twin") as ws:
        ws.send_json({"type": "subscribe", "topics": [f"device:{DEVICE}"]})
        _until(ws, "subscribed")
        publish(c, anomaly())
        alert = _until(ws, "alert.created")["alert"]
        assert alert["severity"] == "HIGH" and alert["status"] == "OPEN" and alert["source_type"] == "anomaly"
        deliver(c)  # (the background worker may already have delivered it)
        note = _until(ws, "notification.created")
        assert note["recipient"] == "local" and note["notification"]["alert_id"] == alert["alert_id"]
    inbox = c.get("/api/v1/notifications").json()
    assert inbox["unread"] == 1 and inbox["items"][0]["status"] == "DELIVERED"
    n = inbox["items"][0]
    assert n["title"] == "Unusually high cpu usage" and "Confidence 90%" in n["body"]
    assert c.post(f"/api/v1/notifications/{n['notification_id']}/read").json()["status"] == "READ"
    assert c.get("/api/v1/notifications/unread-count").json()["unread"] == 0
    detail = c.get(f"/api/v1/alerts/{alert['alert_id']}").json()
    assert detail["metadata"]["observed"] == 88.0 and detail["metadata"]["expected"] == 20.0
    assert [e["action"] for e in detail["audit"]][:1] == ["created"]
    assert {d["channel"] for d in detail["deliveries"]} == {"in_app", "browser"}
    timeline = c.get(f"/api/v1/devices/{DEVICE}/timeline").json()["items"]
    assert any(e["type"] == "alert.created" for e in timeline)


def test_duplicate_events_create_one_alert_and_resolution_closes_it(client: TestClient) -> None:
    c = client
    publish(c, anomaly(), anomaly(), anomaly(kind="updated"))
    deliver(c)
    alerts = c.get("/api/v1/alerts", params={"status": ["OPEN", "ONGOING"]}).json()["items"]
    assert len(alerts) == 1 and alerts[0]["occurrences"] == 3
    assert len(c.get("/api/v1/notifications").json()["items"]) == 1  # no repeated "CPU high"
    publish(c, anomaly(kind="resolved"))
    deliver(c)
    a = c.get(f"/api/v1/alerts/{alerts[0]['alert_id']}").json()
    assert a["status"] == "RESOLVED" and a["resolved_by"] == "system"
    titles = [x["title"] for x in c.get("/api/v1/notifications").json()["items"]]
    assert titles[0].startswith("Resolved:")  # one in-app resolution note, nothing more
    assert c.post(f"/api/v1/alerts/{a['alert_id']}/acknowledge", json={}).status_code == 409  # terminal


def test_prediction_and_connectivity_sources(client: TestClient) -> None:
    c = client
    soon = (datetime.now(UTC) + timedelta(minutes=12)).isoformat()
    publish(
        c,
        PredictionChanged(
            device_id=DEVICE,
            kind="created",
            prediction={
                "prediction_id": "p-1",
                "target_id": "memory",
                "prediction_type": "resource_exhaustion",
                "severity": "MEDIUM",
                "time_to_threshold_s": 720,
                "confidence": 0.8,
                "confidence_band": "HIGH",
                "statement": "RAM may exceed 90%",
                "threshold": 90.0,
                "current_value": 84.0,
                "metric": "performance.memory.usage_percent",
                "crossing_at": soon,
                "model_type": "trend",
                "model_version": "theilsen-v1",
            },
        ),
    )
    publish(
        c,
        PresenceChanged(
            device_id=DEVICE,
            previous_presence="ONLINE",
            presence="OFFLINE",
            last_contact_at=datetime.now(UTC),
        ),
    )
    items = {a["category"]: a for a in c.get("/api/v1/alerts").json()["items"]}
    assert items["prediction"]["severity"] == "HIGH"  # < 15 min to the threshold raises MEDIUM -> HIGH
    assert items["prediction"]["title"] == "Memory exhaustion risk"
    assert items["connectivity"]["alert_type"] == "agent_offline"
    publish(
        c,
        PresenceChanged(
            device_id=DEVICE,
            previous_presence="OFFLINE",
            presence="ONLINE",
            last_contact_at=datetime.now(UTC),
        ),
    )
    assert c.get(f"/api/v1/alerts/{items['connectivity']['alert_id']}").json()["status"] == "RESOLVED"


class Flaky(NotificationProvider):
    channel, name = "email", "flaky"

    def __init__(self, failure: str) -> None:
        self.failure, self.calls = failure, 0

    async def send(self, notification: Notification) -> DeliveryResult:
        self.calls += 1
        return DeliveryResult(False, self.failure, "smtp down")


def test_retry_then_dead_letter_and_permanent_failure(client: TestClient) -> None:
    c = client
    svc = svc_of(c)
    flaky = Flaky("transient")
    svc.providers["email"] = flaky
    c.put(
        "/api/v1/notification-preferences", json={"channels": ["in_app", "email"], "email": "ops@example.com"}
    )
    publish(c, anomaly())
    now = datetime.now(UTC)
    for k in range(6):
        deliver(c, now + timedelta(minutes=5 * k))
    alert_id = c.get("/api/v1/alerts").json()["items"][0]["alert_id"]
    notes = c.portal.call(svc.repo.notifications_for_alert, alert_id)  # type: ignore[union-attr]
    email = next(n for n in notes if n.channel == "email")
    assert email.status.value == "FAILED" and email.attempt_count == 4 and flaky.calls == 4
    assert [h.to_status for h in email.history].count("RETRYING") == 3 and email.failure_reason
    perm = Flaky("permanent")
    svc.providers["email"] = perm
    publish(c, anomaly(aid="an-2", rule="behavior.memory"))
    deliver(c)
    assert perm.calls == 1  # never retried


def test_signed_webhook_and_ssrf_guard(client: TestClient) -> None:
    c = client
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"X-Request-Id": "r-1"})

    svc = svc_of(c)
    svc.providers["webhook"]._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    r = c.put("/api/v1/notification-webhooks", json=[{"name": "itsm", "url": "http://127.0.0.1:9/hook"}])
    assert r.status_code == 200 and "secret" not in json.dumps(r.json()).lower().replace("signing", "")
    publish(c, anomaly(level="CRITICAL"))
    deliver(c)
    assert len(seen) == 1
    req = seen[0]
    body = req.content
    ts = req.headers["X-LDT-Timestamp"]
    expected = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(req.headers["X-LDT-Signature"], expected)
    payload = json.loads(body)
    assert payload["severity"] == "CRITICAL" and req.headers["Idempotency-Key"]
    assert "email" not in json.dumps(payload) and SECRET not in json.dumps(payload)


def test_ssrf_is_blocked_by_default() -> None:
    with TestClient(create_app(settings(WEBHOOK_SIGNING_SECRET=SECRET))) as c:
        for url in (
            "http://example.com/h",
            "https://127.0.0.1/h",
            "https://10.0.0.5/h",
            "https://user:pw@example.com/h",
            "ftp://example.com/h",
        ):
            assert (
                c.put("/api/v1/notification-webhooks", json=[{"name": "x", "url": url}]).status_code == 422
            ), url


def test_escalation_and_restart_recovery(client: TestClient) -> None:
    c = client
    svc = svc_of(c)
    publish(c, anomaly())
    alert_id = c.get("/api/v1/alerts").json()["items"][0]["alert_id"]
    now = datetime.now(UTC)
    c.portal.call(svc.tick, now + timedelta(minutes=31))  # level 1: operators (none in this deployment)
    c.portal.call(svc.tick, now + timedelta(minutes=61))  # level 2: admins
    a = c.get(f"/api/v1/alerts/{alert_id}").json()
    assert a["escalation_level"] == 2 and [e["action"] for e in a["audit"]].count("escalated") == 2
    deliver(c, now + timedelta(minutes=62))
    titles = [n["title"] for n in c.get("/api/v1/notifications").json()["items"]]
    assert any(t.startswith("Not yet acknowledged:") for t in titles)
    # application restart: a new service instance resumes the open alert from storage (no duplicate)
    fresh = AlertService(
        c.app.state.container.settings,
        svc.repo,
        svc._store,
        svc._admin_repo,
        None,
        None,  # type: ignore[attr-defined]
        svc._publish,
        lambda _r: None,
        svc.providers,
        "local",
    )
    c.portal.call(fresh.load)
    assert [x.alert_id for x in fresh.engine.open_alerts()] == [alert_id]
    updated = from_anomaly(anomaly(kind="updated"))
    assert updated is not None
    assert c.portal.call(fresh.ingest, updated)[0].kind == "updated"  # type: ignore[union-attr]


def test_preferences_validation(client: TestClient) -> None:
    c = client
    ok = c.put(
        "/api/v1/notification-preferences",
        json={
            "channels": ["in_app"],
            "severities": ["HIGH", "CRITICAL"],
            "timezone": "Asia/Kolkata",
            "frequency": "digest",
            "quiet_hours": {"start": "22:00", "end": "07:00"},
        },
    )
    assert ok.status_code == 200 and ok.json()["preferences"]["timezone"] == "Asia/Kolkata"
    assert ok.json()["channels"]["email"]["available"] is False  # SMTP not configured: shown, not hidden
    for bad in (
        {"timezone": "Mars/Base"},
        {"quiet_hours": {"start": "25:00", "end": "07:00"}},
        {"channels": ["email"]},
        {"email": "not-an-email"},
        {"channels": ["sms"]},
        {"user_id": "someone"},
    ):
        assert c.put("/api/v1/notification-preferences", json=bad).status_code == 422, bad


@pytest.fixture
def accounts() -> Iterator[TestClient]:
    with TestClient(create_app(settings(AUTH_MODE="accounts", JWT_SECRET="x" * 40))) as c:
        yield c


def test_authorization_and_isolation(accounts: TestClient) -> None:
    c = accounts
    admin = {
        "Authorization": "Bearer "
        + c.post("/api/v1/auth/setup", json={"username": "admin", "password": PASSWORD}).json()[
            "access_token"
        ]
    }
    for d in ("dev-mine", "dev-other"):
        _inventory(c, d)
    for user, role in (("ana", "employee"), ("vic", "viewer"), ("ops", "operator")):
        assert (
            c.post(
                "/api/v1/users", json={"username": user, "password": PASSWORD, "role": role}, headers=admin
            ).status_code
            == 201
        )
    c.put("/api/v1/devices/dev-mine/assignment", json={"username": "ana"}, headers=admin)

    def login(u: str) -> dict[str, str]:
        tok = c.post("/api/v1/auth/login", json={"username": u, "password": PASSWORD}).json()["access_token"]
        return {"Authorization": f"Bearer {tok}"}

    ana, vic, ops = login("ana"), login("vic"), login("ops")
    publish(c, anomaly(device="dev-mine", aid="m1"), anomaly(device="dev-other", aid="o1"))
    deliver(c)
    mine = c.get("/api/v1/alerts", headers=ana).json()["items"]
    assert [a["device_id"] for a in mine] == ["dev-mine"]  # never the other device
    other_id = next(
        a["alert_id"]
        for a in c.get("/api/v1/alerts", headers=admin).json()["items"]
        if a["device_id"] == "dev-other"
    )
    assert c.get(f"/api/v1/alerts/{other_id}", headers=ana).status_code == 404  # no id probing
    assert c.get("/api/v1/alerts", params={"device_id": "dev-other"}, headers=ana).status_code == 404
    ana_notes = c.get("/api/v1/notifications", headers=ana).json()["items"]
    assert {n["device_id"] for n in ana_notes} == {"dev-mine"}  # the owner is notified of her device only
    ops_note = c.get("/api/v1/notifications", headers=ops).json()["items"][0]["notification_id"]
    assert c.get(f"/api/v1/notifications/{ops_note}", headers=ana).status_code == 404  # another user's
    assert c.post(f"/api/v1/notifications/{ops_note}/read", headers=ana).status_code == 404
    assert (
        c.post(f"/api/v1/alerts/{mine[0]['alert_id']}/acknowledge", json={}, headers=ana).status_code == 200
    )
    assert c.post(f"/api/v1/alerts/{mine[0]['alert_id']}/resolve", json={}, headers=ana).status_code == 403
    assert c.post(f"/api/v1/alerts/{other_id}/resolve", json={}, headers=vic).status_code == 403
    assert c.post(f"/api/v1/alerts/{other_id}/suppress", json={"hours": 2}, headers=ops).status_code == 200
    for path in ("/api/v1/alert-policy", "/api/v1/notification-webhooks"):
        assert c.get(path, headers=ops).status_code == 403
    assert c.put("/api/v1/alert-policy", json={"cooldown_s": 60}, headers=ops).status_code == 403
    assert c.put("/api/v1/alert-policy", json={"cooldown_s": 60}, headers=admin).status_code == 200
    assert (
        c.put(
            "/api/v1/alert-policy",
            json={"rules": [{"rule_id": "x", "sources": ["shell"], "min_severity": "LOW"}]},
            headers=admin,
        ).status_code
        == 422
    )
    assert c.get("/api/v1/alerting/stats", headers=ana).status_code == 403
    assert c.get("/api/v1/notifications").status_code == 401
    leaked = json.dumps(c.get("/api/v1/notifications", headers=ops).json())
    assert PASSWORD not in leaked and "token" not in leaked.lower()


def test_security_alert_quotes_the_security_finding_not_the_first_reason() -> None:
    from app.domain.events.events import TwinMessage
    from app.services.alerting import from_twin_health

    msg = TwinMessage(
        device_id=DEVICE,
        kind="twin.event.created",
        body={
            "timeline_event": {
                "event_id": "e1",
                "type": "twin.health",
                "timestamp": datetime.now(UTC).isoformat(),
                "message": "Health UNKNOWN -> WARNING: Memory usage: 93%",
                "data": {
                    "from": "UNKNOWN",
                    "to": "WARNING",
                    "reasons": ["memory", "security"],
                    "details": [
                        {"rule": "memory", "state": "WARNING", "message": "Memory usage: 93%"},
                        {"rule": "security", "state": "WARNING", "message": "Secure Boot: off"},
                    ],
                },
            }
        },
    )
    sec, agent = from_twin_health(msg)
    assert sec.kind == "opened" and sec.summary == "Secure Boot: off" and sec.severity == "MEDIUM"
    assert agent.kind == "closed"  # no agent finding: nothing to alert (and closes an open one)
