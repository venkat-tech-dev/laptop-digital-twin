"""Phase 5 - integration (telemetry -> forecast -> prediction -> twin / WebSocket / storage) and
security of the prediction APIs."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
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


def _post_memory(c: TestClient, value: float, device: str = DEVICE, seq: int = 1) -> None:
    b = batch([sample("memory.usage_percent", value, component="memory", ts=datetime.now(UTC))], seq=seq)
    b.update({"batch_id": f"{device}-{seq}", "device_id": device, "schema_version": "1.2"})
    assert c.post("/api/v1/ingest/telemetry/bulk", json={"batches": [b]}, headers=KEY).status_code == 200


def _seed_rising_memory(c: TestClient, device: str = DEVICE) -> Any:
    """25 minutes of rising RAM (60 -> 84 %) in the forecaster's bounded series (test seam: the live
    path fills it from the twin window and a one-time TimescaleDB bootstrap)."""
    svc = c.app.state.container.forecasts  # type: ignore[attr-defined]
    now = datetime.now(UTC)
    c.portal.call(svc.evaluate_device, device, now, True)  # type: ignore[union-attr]
    st = svc._devices[device].targets["memory"]
    ts = now.timestamp()
    st.series.load_history([(ts - (25 - i) * 60, 60.0 + i) for i in range(25)])
    return svc


def _until(ws: Any, event: str, limit: int = 60) -> dict[str, Any]:
    for _ in range(limit):
        m = ws.receive_json()
        if m["event"] == event:
            return m
    raise AssertionError(event)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(settings())) as c:
        _inventory(c)
        _post_memory(c, 84.0)
        yield c


def test_telemetry_to_prediction_twin_websocket_and_history(client: TestClient) -> None:
    c = client
    svc = _seed_rising_memory(c)
    with c.websocket_connect("/ws/twin") as ws:
        ws.send_json({"type": "subscribe", "topics": [f"device:{DEVICE}"]})
        _until(ws, "twin.snapshot")
        trs = c.portal.call(svc.evaluate_device, DEVICE, datetime.now(UTC), True)  # type: ignore[union-attr]
        assert "created" in [t.kind for t in trs]
        ev = _until(ws, "prediction.created")["prediction"]
        assert ev["target_id"] == "memory" and ev["status"] == "ACTIVE" and ev["threshold"] == 90.0
        assert 0 < ev["time_to_threshold_s"] < 20 * 60 and ev["crossing_earliest"] <= ev["crossing_at"]
        assert ev["model_type"] == "trend" and ev["model_version"] == "theilsen-v1"
        assert ev["feature_version"] and ev["evidence"]["confidence"]["factors"]
        assert "curve" not in ev["evidence"]  # the forecast path is served by the detail endpoint
    cur = c.get(f"/api/v1/devices/{DEVICE}/predictions").json()
    mem = next(t for t in cur["targets"] if t["target_id"] == "memory")
    assert mem["status"] == "AVAILABLE" and mem["prediction"]["prediction_id"] == ev["prediction_id"]
    battery = next(t for t in cur["targets"] if t["target_id"] == "battery")
    assert battery["status"] in ("NOT_APPLICABLE", "INSUFFICIENT_HISTORY", "STALE_DATA")
    flat = c.get(f"/api/v1/devices/{DEVICE}/twin", params={"format": "flat"}).json()["state"]
    assert flat["predictions.memory"]["prediction_id"] == ev["prediction_id"]
    assert flat["predictions.active_count"] == 1
    assert flat["performance.memory.usage_percent"]["value"] == 84.0  # observed value untouched
    one = c.get(f"/api/v1/predictions/{ev['prediction_id']}").json()
    assert one["statement"].startswith("Memory utilization") and "guarantee" in one["evidence"]["wording"]
    curve = c.get(f"/api/v1/devices/{DEVICE}/predictions/memory/forecast").json()
    assert len(curve["history"]) >= 20 and len(curve["forecast"]) == 25
    assert all(p["lower"] <= p["mean"] <= p["upper"] for p in curve["forecast"])
    hist = c.get(f"/api/v1/devices/{DEVICE}/predictions/history", params={"active": "true"}).json()
    assert [h["prediction_id"] for h in hist["items"]] == [ev["prediction_id"]]
    acc = c.get("/api/v1/prediction-accuracy").json()
    assert acc["overall"]["closed"] == 0 and "by_confidence_band" in acc


def test_offline_device_forecasts_are_stale_not_invented(client: TestClient) -> None:
    c = client
    svc = c.app.state.container.forecasts  # type: ignore[attr-defined]
    later = datetime.now(UTC) + timedelta(minutes=30)
    c.portal.call(svc.evaluate_device, DEVICE, later, True)  # type: ignore[union-attr]
    mem = next(t for t in svc.current(DEVICE)["targets"] if t["target_id"] == "memory")
    assert mem["status"] == "STALE_DATA" and mem["time_to_threshold_s"] is None


def test_config_validation_and_no_training_endpoints(client: TestClient) -> None:
    c = client
    cfg = c.get("/api/v1/prediction-config").json()
    assert (
        cfg["targets"]["disk"]["thresholds"] == [90.0, 95.0] and cfg["policy"]["create_min_confidence"] == 0.5
    )
    ok = c.put(
        "/api/v1/prediction-config",
        json={"targets": {"disk": {"thresholds": [85, 95]}}, "policy": {"create_min_confidence": 0.6}},
    )
    assert ok.status_code == 200 and ok.json()["targets"]["disk"]["thresholds"] == [85.0, 95.0]
    assert ok.json()["version"] == 1
    bad = [
        {"targets": {"disk": {"thresholds": [150]}}},
        {"targets": {"disk": {"bucket_s": 1}}},
        {"targets": {"gpu": {"enabled": True}}},
        {"policy": {"keep_min_confidence": 0.9}},
        {"policy": {"__import__": 1}},
        {"code": "print(1)"},
    ]
    for body in bad:
        assert c.put("/api/v1/prediction-config", json=body).status_code == 422, body
    paths = c.get("/openapi.json").json()["paths"]
    assert not [
        p for p in paths if "predict" in p and any(w in p for w in ("train", "execute", "upload", "run"))
    ]
    assert c.get("/api/v1/predictions/' OR 1=1 --").status_code == 404
    assert c.get(f"/api/v1/devices/{DEVICE}/predictions/history", params={"limit": 10_000}).status_code == 422
    assert c.get(f"/api/v1/devices/{DEVICE}/predictions/gpu/forecast").status_code == 422


@pytest.fixture
def accounts() -> Iterator[TestClient]:
    with TestClient(create_app(settings(AUTH_MODE="accounts", JWT_SECRET="x" * 40))) as c:
        yield c


def test_device_isolation_and_roles(accounts: TestClient) -> None:
    c = accounts
    admin_tok = c.post("/api/v1/auth/setup", json={"username": "admin", "password": PASSWORD}).json()
    admin = {"Authorization": f"Bearer {admin_tok['access_token']}"}
    for i, device in enumerate(("dev-mine", "dev-other")):
        _inventory(c, device)
        _post_memory(c, 84.0, device, seq=1 + i)
    for user, role in (("ana", "employee"), ("vic", "viewer")):
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

    emp, viewer = login("ana"), login("vic")
    svc = _seed_rising_memory(c, "dev-other")
    trs = c.portal.call(svc.evaluate_device, "dev-other", datetime.now(UTC), True)  # type: ignore[union-attr]
    pid = next(t.prediction.prediction_id for t in trs if t.kind == "created")
    for path in ("predictions", "predictions/history", "predictions/memory/forecast"):
        assert c.get(f"/api/v1/devices/dev-other/{path}", headers=emp).status_code == 404
    assert c.get("/api/v1/devices/dev-mine/predictions", headers=emp).status_code == 200
    assert c.get(f"/api/v1/predictions/{pid}", headers=emp).status_code == 404  # no id probing
    assert c.get(f"/api/v1/predictions/{pid}", headers=viewer).status_code == 200
    assert c.get("/api/v1/prediction-accuracy", headers=emp).status_code == 403
    assert c.get("/api/v1/prediction-config", headers=viewer).status_code == 403
    assert c.put("/api/v1/prediction-config", json={}, headers=viewer).status_code == 403
    assert c.get("/api/v1/prediction-config").status_code == 401


def test_metric_not_seen_yet_is_retried_on_the_next_tick(client: TestClient) -> None:
    c = client
    svc = c.app.state.container.forecasts  # type: ignore[attr-defined]
    now = datetime.now(UTC)
    c.portal.call(svc.evaluate_device, DEVICE, now)  # type: ignore[union-attr]
    disk = svc._devices[DEVICE].targets["disk"]
    assert disk.assessment is not None and disk.assessment.status == "NOT_APPLICABLE"
    assert now.timestamp() - disk.last_eval >= disk.assessment.target.update_interval_s - 20  # due again soon
