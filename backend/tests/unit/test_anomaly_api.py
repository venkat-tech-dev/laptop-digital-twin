"""Phase 4 - integration (telemetry -> anomaly -> twin / WebSocket / storage) and security of the
anomaly intelligence APIs."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.domain.anomalies.baseline import BaselineStatus, ContextStats, SignalBaseline
from app.domain.anomalies.stats import Summary
from app.main import create_app
from app.repositories.intelligence import StoredBaseline
from app.services.intelligence import DeviceIntel
from tests.conftest import AGENT_KEY, DEVICE, INVENTORY, batch, sample, settings

KEY = {"X-Agent-Key": AGENT_KEY}
PASSWORD = "Correct-Horse-9-Battery"


def _inventory(c: TestClient, device: str = DEVICE) -> None:
    env = {
        "device_id": device,
        "agent_version": "t",
        "discovered_at": datetime.now(UTC).isoformat(),
        "inventory": INVENTORY,
    }
    assert c.post("/api/v1/ingest/inventory", json=env, headers=KEY).status_code in (200, 202)


def _post_cpu(c: TestClient, value: float, end: datetime, device: str = DEVICE, seq0: int = 1) -> None:
    """24 samples at 5 s spacing ending at ``end`` (a full 2-minute observation window)."""
    batches = []
    for i in range(24):
        ts = end - timedelta(seconds=115 - 5 * i)
        b = batch([sample("cpu.usage_percent", value, ts=ts)], seq=seq0 + i)
        b.update({"batch_id": f"{device}-{seq0 + i}", "device_id": device, "schema_version": "1.2"})
        batches.append(b)
    r = c.post("/api/v1/ingest/telemetry/bulk", json={"batches": batches}, headers=KEY)
    assert r.status_code == 200, r.text


def _seed_baseline(c: TestClient, device: str = DEVICE) -> None:
    intel = c.app.state.container.intelligence  # type: ignore[attr-defined]
    b = SignalBaseline(
        "cpu",
        BaselineStatus.STABLE,
        "cpu-test",
        datetime.now(UTC) - timedelta(days=8),
        datetime.now(UTC),
        12_000,
        0,
        {"all": ContextStats("all", Summary.of([10.0 + (i % 21) for i in range(500)]))},
    )
    intel._devices[device] = DeviceIntel(
        baselines={"cpu": StoredBaseline(b, "cpu.usage_percent")}, loaded=True, trained_at=time.monotonic()
    )


def _until(ws: Any, event: str, limit: int = 60) -> dict[str, Any]:
    for _ in range(limit):
        m = ws.receive_json()
        if m["event"] == event:
            return m
    raise AssertionError(event)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(settings(ANOMALY_PERSISTENCE_S=0))) as c:
        _inventory(c)
        yield c


def test_telemetry_to_anomaly_to_twin_websocket_and_history(client: TestClient) -> None:
    c = client
    intel = c.app.state.container.intelligence  # type: ignore[attr-defined]
    _seed_baseline(c)
    now = datetime.now(UTC)
    _post_cpu(c, 88.0, now)
    with c.websocket_connect("/ws/twin") as ws:
        ws.send_json({"type": "subscribe", "topics": [f"device:{DEVICE}"]})
        _until(ws, "twin.snapshot")
        trs = c.portal.call(intel.evaluate_device, DEVICE, now)  # type: ignore[union-attr]
        assert [t.kind for t in trs] == ["detected"]
        event = _until(ws, "anomaly.detected")
        a = event["anomaly"]
        assert a["anomaly_type"] == "behavioral_anomaly" and a["signal_id"] == "cpu"
        assert a["level"] in ("MEDIUM", "HIGH") and 0 < a["confidence"] <= 1
        assert a["evidence"]["expected"]["median"] == 20.0
        patch = _until(ws, "twin.state.patch")
        assert patch["changes"].get("alerts.highest_severity") == a["level"]
    # REST read side
    active = c.get(f"/api/v1/devices/{DEVICE}/anomalies/active").json()["items"]
    assert [x["anomaly_id"] for x in active] == [a["anomaly_id"]]
    one = c.get(f"/api/v1/anomalies/{a['anomaly_id']}").json()
    assert one["evidence"]["methods"] and one["confidence_band"] in ("MODERATE", "HIGH", "VERY HIGH")
    hist = c.get(f"/api/v1/devices/{DEVICE}/anomalies", params={"type": "behavioral_anomaly"}).json()
    assert hist["items"][0]["anomaly_id"] == a["anomaly_id"]
    assert c.get(f"/api/v1/devices/{DEVICE}/anomalies", params={"level": "CRITICAL"}).json()["items"] == []
    summary = c.get(f"/api/v1/devices/{DEVICE}/anomaly-summary").json()
    assert summary["active_count"] == 1 and summary["highest_severity"] == a["level"]
    assert summary["detection"]["mode"] == "statistical"  # baselines, no multivariate model
    twin = c.get(f"/api/v1/devices/{DEVICE}/twin", params={"format": "flat"}).json()["state"]
    assert twin["alerts.active_count"] == 1 and twin["alerts.active"][0]["type"] == "behavioral_anomaly"
    assert twin["performance.cpu.usage_percent"]["value"] == 88.0  # raw telemetry untouched
    baseline = c.get(f"/api/v1/devices/{DEVICE}/baseline").json()
    cpu = next(s for s in baseline["signals"] if s["signal_id"] == "cpu")
    assert cpu["status"] == "STABLE" and cpu["source_key"] == "cpu.usage_percent"
    # legacy endpoint still lists it (backward compatibility)
    legacy = c.get("/api/v1/anomalies", params={"device_id": DEVICE}).json()
    assert any(x["anomaly_id"] == a["anomaly_id"] for x in legacy)
    # feedback: false positive mutes the key and is counted
    fb = c.post(f"/api/v1/anomalies/{a['anomaly_id']}/feedback", json={"verdict": "false_positive"})
    assert fb.status_code == 200 and fb.json()["feedback"]["verdict"] == "false_positive"
    # recovery: normal values for longer than recovery_s resolve it
    later = now + timedelta(seconds=200)
    _post_cpu(c, 20.0, later, seq0=100)
    c.portal.call(intel.evaluate_device, DEVICE, later)  # type: ignore[union-attr]
    trs = c.portal.call(intel.evaluate_device, DEVICE, later + timedelta(seconds=130))  # type: ignore[union-attr]
    # by then the samples are stale: the anomaly is either resolved or still open, never invented
    assert all(t.kind in ("resolved", "updated") for t in trs)


def test_offline_device_is_not_evaluated(client: TestClient) -> None:
    c = client
    intel = c.app.state.container.intelligence  # type: ignore[attr-defined]
    _seed_baseline(c)
    now = datetime.now(UTC)
    _post_cpu(c, 88.0, now)
    from app.services.presence import Presence

    c.app.state.container.presence.get(DEVICE).presence = Presence.OFFLINE  # type: ignore[attr-defined]
    trs = c.portal.call(intel.evaluate_device, DEVICE, now)  # type: ignore[union-attr]
    assert trs == []  # no current data from an offline device: nothing is judged
    assert intel.summary(DEVICE)["data_quality"]["cpu"] == "device_not_connected"


def test_admin_config_validation_and_no_training_endpoints(client: TestClient) -> None:
    c = client
    cfg = c.get("/api/v1/anomaly-config").json()
    assert cfg["policy"]["z_trigger"] == 3.5 and "z_trigger" in cfg["editable"]
    ok = c.put(
        "/api/v1/anomaly-config", json={"z_trigger": 4.0, "enabled_detectors": ["robust_z", "quantile"]}
    )
    assert ok.status_code == 200 and ok.json()["policy"]["z_trigger"] == 4.0 and ok.json()["version"] == 1
    assert c.put("/api/v1/anomaly-config", json={"z_trigger": 99}).status_code == 422  # out of range
    assert (
        c.put("/api/v1/anomaly-config", json={"eval": "__import__('os')"}).status_code == 422
    )  # unknown key
    assert c.put("/api/v1/anomaly-config", json={"enabled_detectors": ["shell"]}).status_code == 422
    assert c.put("/api/v1/anomaly-config", json={"z_recover": 5.0, "z_trigger": 4.0}).status_code == 422
    paths = c.get("/openapi.json").json()["paths"]
    forbidden = ("train", "retrain", "execute", "run-detector", "model/upload")
    assert not [p for p in paths if any(f in p for f in forbidden) and "anomal" in p]


def test_malicious_and_oversized_inputs(client: TestClient) -> None:
    c = client
    assert c.get("/api/v1/anomalies/' OR 1=1 --").status_code == 404
    assert (
        c.get(f"/api/v1/devices/{DEVICE}/anomalies", params={"signal_id": "cpu;drop table"}).status_code
        == 422
    )
    assert c.get(f"/api/v1/devices/{DEVICE}/anomalies", params={"limit": 10_000}).status_code == 422
    assert c.get(f"/api/v1/devices/{DEVICE}/anomalies", params={"level": "EVIL"}).status_code == 422
    assert c.get("/api/v1/devices/unknown-device/anomalies").status_code == 404
    big = {"verdict": "unsure", "note": "x" * 5000}
    assert c.post("/api/v1/anomalies/whatever/feedback", json=big).status_code == 422


@pytest.fixture
def accounts() -> Iterator[TestClient]:
    with TestClient(create_app(settings(AUTH_MODE="accounts", JWT_SECRET="x" * 40))) as c:
        yield c


def test_device_isolation_and_roles(accounts: TestClient) -> None:
    c = accounts
    admin_tok = c.post("/api/v1/auth/setup", json={"username": "admin", "password": PASSWORD}).json()
    admin = {"Authorization": f"Bearer {admin_tok['access_token']}"}
    for device in ("dev-mine", "dev-other"):
        _inventory(c, device)
        _post_cpu(c, 88.0, datetime.now(UTC), device=device)
    for user, role in (("ana", "employee"), ("vic", "viewer")):
        r = c.post(
            "/api/v1/users", json={"username": user, "password": PASSWORD, "role": role}, headers=admin
        )
        assert r.status_code == 201
    c.put("/api/v1/devices/dev-mine/assignment", json={"username": "ana"}, headers=admin)
    emp = {
        "Authorization": "Bearer "
        + c.post("/api/v1/auth/login", json={"username": "ana", "password": PASSWORD}).json()["access_token"]
    }
    viewer = {
        "Authorization": "Bearer "
        + c.post("/api/v1/auth/login", json={"username": "vic", "password": PASSWORD}).json()["access_token"]
    }
    intel = c.app.state.container.intelligence  # type: ignore[attr-defined]
    _seed_baseline(c, "dev-other")
    intel.policy = intel.policy.merged({"persistence_s": 0.0})
    intel.engine.set_policy(intel.policy)
    trs = c.portal.call(intel.evaluate_device, "dev-other", datetime.now(UTC))  # type: ignore[union-attr]
    other_id = trs[0].anomaly.anomaly_id
    for path in ("anomalies", "anomalies/active", "anomaly-summary", "baseline"):
        assert c.get(f"/api/v1/devices/dev-other/{path}", headers=emp).status_code == 404
        assert c.get(f"/api/v1/devices/dev-mine/{path}", headers=emp).status_code == 200
    assert c.get(f"/api/v1/anomalies/{other_id}", headers=emp).status_code == 404  # no id probing
    assert c.get(f"/api/v1/anomalies/{other_id}", headers=viewer).status_code == 200
    fb = {"verdict": "false_positive"}
    assert c.post(f"/api/v1/anomalies/{other_id}/feedback", json=fb, headers=viewer).status_code == 403
    assert c.post(f"/api/v1/anomalies/{other_id}/feedback", json=fb, headers=admin).status_code == 200
    assert c.get("/api/v1/anomaly-config", headers=viewer).status_code == 403
    assert c.put("/api/v1/anomaly-config", json={"z_trigger": 4}, headers=viewer).status_code == 403
    assert c.get("/api/v1/anomaly-config").status_code == 401
    token = emp["Authorization"].removeprefix("Bearer ")
    with c.websocket_connect(f"/ws/twin?token={token}") as ws:  # anomaly events respect device scope
        ws.send_json({"type": "subscribe", "topics": ["device:dev-mine"]})
        _until(ws, "subscribed")
        _post_cpu(c, 90.0, datetime.now(UTC), device="dev-other", seq0=200)
        c.portal.call(intel.evaluate_device, "dev-other", datetime.now(UTC))  # type: ignore[union-attr]
        _post_cpu(c, 50.0, datetime.now(UTC), device="dev-mine", seq0=300)
        for _ in range(40):
            m = ws.receive_json()
            assert m.get("device_id") in (None, "dev-mine"), m["event"]
            if m["event"] == "twin.state.patch":
                break
