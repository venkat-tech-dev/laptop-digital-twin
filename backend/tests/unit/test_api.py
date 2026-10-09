from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from tests.conftest import AGENT_KEY, batch, inventory_envelope, sample, settings, typical_samples

H = {"X-Agent-Key": AGENT_KEY}


def test_ingest_requires_agent_key(client: TestClient) -> None:
    assert client.post("/api/v1/ingest/inventory", json=inventory_envelope()).status_code == 401
    assert (
        client.post(
            "/api/v1/ingest/inventory", json=inventory_envelope(), headers={"X-Agent-Key": "wrong"}
        ).status_code
        == 401
    )


def test_unknown_device_gets_409(client: TestClient) -> None:
    r = client.post("/api/v1/ingest/telemetry", json=batch(typical_samples()), headers=H)
    assert r.status_code == 409


def test_validation_errors_do_not_echo_payload(client: TestClient) -> None:
    bad = batch([sample("cpu.usage_percent", 1.0)])
    bad["samples"][0]["quality"] = "SECRET-VALUE"
    r = client.post("/api/v1/ingest/telemetry", json=bad, headers=H)
    assert r.status_code == 422 and "SECRET-VALUE" not in r.text


def test_no_device_yet_404(client: TestClient) -> None:
    assert client.get("/api/v1/twin").status_code == 404
    assert client.get("/api/v1/device").status_code == 404


def test_device_twin_and_geometry_honesty(live_client: TestClient) -> None:
    d = live_client.get("/api/v1/device").json()
    assert d["manufacturer"] == "LENOVO" and d["status"] == "LIVE"
    assert d["data_source"] == "LOCAL WINDOWS HARDWARE" and d["telemetry"] == "REAL"
    # Known model without a mesh file: profile-based parametric twin, never claimed as exact.
    assert d["geometry"]["kind"] == "profile" and d["geometry"]["label"] == "MODEL PROFILE"
    assert d["geometry"]["url"] is None
    twin = live_client.get("/api/v1/twin").json()
    assert twin["mode"] == "live"
    assert twin["thermal"]["sensor"] == "ACPI thermal zone _TZ.THM0"
    assert twin["thermal"]["is_cpu_package_sensor"] is False
    comps = live_client.get("/api/v1/twin/components").json()
    assert any(c["component_id"] == "cpu" for c in comps)
    cpu = live_client.get("/api/v1/twin/components/cpu").json()
    assert cpu["telemetry"]["cpu.usage_percent"]["value"] == 35.0
    assert live_client.get("/api/v1/twin/components/nope").status_code == 404


def test_hardware_hides_serial_by_default(live_client: TestClient) -> None:
    inv = live_client.get("/api/v1/hardware").json()["inventory"]
    assert "serial_number" not in inv
    assert "SECRET-SERIAL" not in live_client.get("/api/v1/hardware").text


def test_telemetry_latest_recent_history(live_client: TestClient) -> None:
    latest = live_client.get("/api/v1/telemetry/latest").json()
    assert latest["cpu.temperature_c"]["availability"] == "unavailable"
    assert latest["cpu.temperature_c"]["reason"] == "needs LHM"
    recent = live_client.get("/api/v1/telemetry/recent?seconds=60").json()
    assert recent["points"] and "cpu.usage_percent" in recent["points"][-1][1]
    hist = live_client.get("/api/v1/telemetry/history?keys=cpu.usage_percent&minutes=5")
    assert hist.status_code == 200 and "cpu.usage_percent" in hist.json()["series"]


def test_health_anomalies_analytics(live_client: TestClient) -> None:
    health = live_client.get("/api/v1/health").json()
    assert health["overall"]["score"] is not None
    live_client.post(
        "/api/v1/ingest/telemetry",
        headers=H,
        json=batch(
            [sample("disk.usage_percent", 97.0, component="storage", labels={"volume": "C:"})], seq=99
        ),
    )
    anomalies = live_client.get("/api/v1/anomalies?status=active").json()
    assert {a["rule_id"] for a in anomalies} >= {"disk_space_low", "disk_space_critical"}
    assert live_client.get("/api/v1/anomalies/rules").json()
    preds = live_client.get("/api/v1/analytics/predictions").json()
    assert {p["prediction_id"] for p in preds["predictions"]} == {
        "thermal_trend",
        "memory_pressure",
        "storage_capacity",
        "battery_degradation",
    }
    assert all("confidence" in p for p in preds["predictions"])
    assert live_client.get("/api/v1/analytics/thermal?minutes=5").json()["sensors"]
    assert "cpu.usage_percent" in live_client.get("/api/v1/analytics/performance").json()["metrics"]


def test_simulation_is_labelled_and_never_touches_live_state(live_client: TestClient) -> None:
    before = live_client.get("/api/v1/twin/components/cpu").json()["telemetry"]["cpu.usage_percent"]["value"]
    r = live_client.post("/api/v1/simulation/run", json={"scenario": "ai_ml", "duration_minutes": 5}).json()
    assert r["mode"] == "SIMULATION" and r["label"] == "SIMULATION — GENERATED DATA"
    assert r["trajectory"] and r["assumptions"]
    after = live_client.get("/api/v1/twin/components/cpu").json()["telemetry"]["cpu.usage_percent"]["value"]
    assert before == after
    assert live_client.post("/api/v1/simulation/run", json={"scenario": "bogus"}).status_code == 422


def test_processes_endpoint_read_only(live_client: TestClient) -> None:
    procs = {
        "timestamp": datetime.now(UTC).isoformat(),
        "source": "test",
        "total_processes": 2,
        "unavailable_fields": {},
        "processes": [
            {
                "pid": 1,
                "name": "a.exe",
                "status": "running",
                "cpu_percent": 5.0,
                "memory_rss_bytes": 10,
                "memory_percent": 0.1,
                "num_threads": 2,
                "gpu_percent": None,
                "io_read_bytes_per_sec": 0.0,
                "io_write_bytes_per_sec": 0.0,
            },
            {
                "pid": 2,
                "name": "b.exe",
                "status": "running",
                "cpu_percent": 1.0,
                "memory_rss_bytes": 99,
                "memory_percent": 0.5,
                "num_threads": 2,
                "gpu_percent": 3.0,
                "io_read_bytes_per_sec": None,
                "io_write_bytes_per_sec": None,
            },
        ],
    }
    live_client.post(
        "/api/v1/ingest/telemetry", headers=H, json=batch([sample("cpu.usage_percent", 1.0)], 50, procs)
    )
    by_mem = live_client.get("/api/v1/system/processes?sort_by=memory").json()
    assert [p["name"] for p in by_mem["processes"]] == ["b.exe", "a.exe"]
    assert live_client.delete("/api/v1/system/processes").status_code == 405


def test_probes_and_metrics(live_client: TestClient) -> None:
    assert live_client.get("/health/live").json()["status"] == "alive"
    ready = live_client.get("/health/ready").json()
    assert ready["status"] == "ready" and ready["checks"]["database"]["status"] == "disabled"
    assert "ldt_ingest_batches_total" in live_client.get("/metrics").text
    assert live_client.get("/api/v1/system/info").json()["persistence"] == "memory"
    assert "X-Request-ID" in live_client.get("/health/live").headers


def test_api_key_auth_mode() -> None:
    app = create_app(settings(AUTH_MODE="api_key", API_KEYS="reader-key-123456", JWT_SECRET="j" * 40))
    with TestClient(app) as c:
        assert c.get("/api/v1/auth/config").json()["mode"] == "api_key"
        assert c.get("/api/v1/device").status_code == 401
        assert c.get("/api/v1/device", headers={"X-API-Key": "reader-key-123456"}).status_code == 404
        tok = c.post("/api/v1/auth/token", json={"api_key": "reader-key-123456"}).json()["access_token"]
        assert c.get("/api/v1/device", headers={"Authorization": f"Bearer {tok}"}).status_code == 404
        assert c.post("/api/v1/auth/token", json={"api_key": "wrong-key-xyz"}).status_code == 401
        assert c.get("/api/v1/device", headers={"Authorization": "Bearer a.b.c"}).status_code == 401
        with pytest.raises(WebSocketDisconnect), c.websocket_connect("/ws/twin") as ws:
            ws.receive_json()
        with c.websocket_connect("/ws/twin?token=reader-key-123456") as ws:
            assert ws.receive_json()["event"] == "connection_status"


def test_rate_limit() -> None:
    app = create_app(settings(RATE_LIMIT_PER_MINUTE=10))
    with TestClient(app) as c:
        codes = [c.get("/api/v1/auth/config").status_code for _ in range(12)]
        assert codes.count(429) == 2


def test_websocket_stream_snapshot_updates_ping(live_client: TestClient) -> None:
    with live_client.websocket_connect("/ws/twin") as ws:
        assert ws.receive_json()["event"] == "connection_status"
        snap = ws.receive_json()
        assert snap["event"] == "twin_snapshot" and snap["twin"]["device_status"] == "LIVE"
        live_client.post(
            "/api/v1/ingest/telemetry", headers=H, json=batch([sample("cpu.usage_percent", 77.0)], seq=10)
        )
        events = []
        while True:
            msg = ws.receive_json()
            events.append(msg["event"])
            if msg["event"] == "telemetry_update":
                assert msg["components"]["cpu"]["telemetry"]["cpu.usage_percent"]["value"] == 77.0
                assert msg["mode"] == "live"
                break
        ws.send_json({"type": "ping"})
        while ws.receive_json()["event"] != "pong":
            pass


def test_websocket_rejects_foreign_origin(live_client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        live_client.websocket_connect("/ws/twin", headers={"origin": "https://evil.example"}) as ws,
    ):
        ws.receive_json()


def test_device_profile_matching() -> None:
    from app.services.device_profiles import find_profile

    assert find_profile("LENOVO", "ThinkPad L14 Gen 4", "21H1S0PM00") is not None
    assert find_profile("LENOVO", None, "21H5CTO1WW") is not None  # AMD machine type
    assert find_profile("LENOVO", "ThinkPad L14 Gen 3", "21C1") is None
    assert find_profile("Dell Inc.", "Latitude 5440", None) is None


def test_geometry_uses_profile_for_known_model(tmp_path: Path) -> None:
    from app.services.geometry import GeometryResolver

    g = GeometryResolver(tmp_path).resolve("LENOVO", "ThinkPad L14 Gen 4", {"model_number": "21H1S0PM00"})
    assert g["kind"] == "profile" and g["label"] == "MODEL PROFILE" and g["url"] is None
    assert g["profile"]["chassis_mm"]["width"] > 300
    assert "match" not in g["profile"]
