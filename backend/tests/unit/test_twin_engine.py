"""Phase-3 digital twin state engine: projection, partial updates, ordering, duplicates, freshness,
connectivity, health rules, hysteresis, timeline, patches/snapshots, restore and authorization."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.schemas.ingest import InventoryEnvelopeIn, TelemetryBatchIn
from app.services.digital_twin import DigitalTwinService
from app.services.twin_engine import TwinEngine, TwinPolicy
from tests.conftest import AGENT_KEY, DEVICE, INVENTORY, batch, sample, settings

KEY = {"X-Agent-Key": AGENT_KEY}
T0 = datetime(2026, 10, 7, 10, 0, 0, tzinfo=UTC)


# ------------------------------------------------------------------ helpers (pure engine)


def _twins(s: Settings | None = None) -> DigitalTwinService:
    svc = DigitalTwinService(s or settings())
    svc.apply_inventory(
        InventoryEnvelopeIn(device_id=DEVICE, agent_version="t", discovered_at=T0, inventory=INVENTORY), T0
    )
    return svc


def _batch(seq: int, ts: datetime, samples: list[dict[str, Any]], **extra: Any) -> TelemetryBatchIn:
    b = batch(samples, seq=seq)
    b.update({"batch_id": f"b-{seq}", "sent_at": ts.isoformat(), "schema_version": "1.2", **extra})
    return TelemetryBatchIn.model_validate(b)


def _cpu(v: float, ts: datetime) -> dict[str, Any]:
    s = sample("cpu.usage_percent", v, ts=ts)
    s["interval_ms"] = 5000
    return s


def _mem(v: float, ts: datetime) -> dict[str, Any]:
    s = sample("memory.usage_percent", v, component="memory", ts=ts)
    s["interval_ms"] = 5000
    return s


def _apply(
    svc: DigitalTwinService, eng: TwinEngine, b: TelemetryBatchIn, now: datetime, presence: str = "ONLINE"
):
    svc.update(b, now)
    return eng.project(svc.get(DEVICE), now, presence)


def _field(eng: TwinEngine, path: str) -> dict[str, Any]:
    return eng.docs[DEVICE].state[path]


# ------------------------------------------------------------------ projection


def test_projection_values_units_provenance_and_visual_states() -> None:
    svc, eng = _twins(), TwinEngine(epoch="e1")
    ch = _apply(svc, eng, _batch(1, T0, [_cpu(85.0, T0), _mem(40.0, T0)]), T0 + timedelta(seconds=1))
    cpu = _field(eng, "performance.cpu.usage_percent")
    assert cpu["value"] == 85.0 and cpu["unit"] == "%" and cpu["status"] == "elevated"
    assert cpu["freshness"] == "LIVE" and cpu["interval_s"] == 5.0
    assert cpu["source"]["batch_id"] == "b-1" and cpu["source"]["sequence"] == 1
    assert cpu["source"]["metric_key"] == "cpu.usage_percent"
    assert ch is not None and ch.version == 1 and ch.base_version == 0
    _apply(
        svc,
        eng,
        _batch(2, T0 + timedelta(seconds=5), [_cpu(97.0, T0 + timedelta(seconds=5))]),
        T0 + timedelta(seconds=6),
    )
    assert _field(eng, "performance.cpu.usage_percent")["status"] == "critical"
    assert eng.docs[DEVICE].state["sections.cpu"]["visual"] == "critical"
    # an instantaneous CPU peak is not a health problem (only sustained load is)
    assert eng.docs[DEVICE].state["health"]["state"] == "HEALTHY"


def test_partial_updates_keep_latest_known_values() -> None:
    svc, eng = _twins(), TwinEngine(epoch="e1")
    _apply(svc, eng, _batch(1, T0, [_cpu(30.0, T0), _mem(50.0, T0)]), T0)
    net = sample(
        "network.internet_connected", True, "bool", component="network", ts=T0 + timedelta(seconds=5)
    )
    ch = _apply(svc, eng, _batch(2, T0 + timedelta(seconds=5), [net]), T0 + timedelta(seconds=5))
    assert _field(eng, "performance.cpu.usage_percent")["value"] == 30.0  # not reset to 0 / None
    assert _field(eng, "performance.memory.usage_percent")["value"] == 50.0
    assert _field(eng, "network.internet_connected")["value"] is True
    assert ch is not None and "performance.cpu.usage_percent" not in ch.changes  # patch = only what changed


def test_old_telemetry_never_overwrites_newer_state_and_duplicates_do_not_mutate() -> None:
    svc, eng = _twins(), TwinEngine(epoch="e1")
    _apply(
        svc,
        eng,
        _batch(5, T0 + timedelta(seconds=20), [_cpu(70.0, T0 + timedelta(seconds=20))]),
        T0 + timedelta(seconds=20),
    )
    v = eng.docs[DEVICE].version
    late = _apply(svc, eng, _batch(4, T0, [_cpu(10.0, T0)], replay=True), T0 + timedelta(seconds=21))
    assert _field(eng, "performance.cpu.usage_percent")["value"] == 70.0
    assert late is None or "performance.cpu.usage_percent" not in late.changes
    v2 = eng.docs[DEVICE].version
    same = _apply(
        svc,
        eng,
        _batch(5, T0 + timedelta(seconds=20), [_cpu(70.0, T0 + timedelta(seconds=20))]),
        T0 + timedelta(seconds=21),
    )
    assert same is None and eng.docs[DEVICE].version == v2 >= v  # identical input: no new version


def test_projection_is_deterministic() -> None:
    def run() -> tuple[int, dict[str, Any]]:
        svc, eng = _twins(), TwinEngine(epoch="same")
        for i, v in enumerate((20.0, 92.0, 99.0, 40.0)):
            ts = T0 + timedelta(seconds=5 * i)
            _apply(svc, eng, _batch(i + 1, ts, [_cpu(v, ts), _mem(60 + i, ts)]), ts)
        snap = eng.snapshot(DEVICE)
        assert snap is not None
        return snap["twin_version"], snap["state"]

    assert run() == run()


def test_freshness_follows_each_metrics_own_interval() -> None:
    svc, eng = _twins(), TwinEngine(TwinPolicy(publish_wait_s=5, grace_s=5), epoch="e1")
    fw = sample(
        "security.firewall_enabled",
        True,
        "bool",
        component="motherboard",
        labels={"profile": "domain"},
        ts=T0,
    )
    fw["interval_ms"] = 300_000
    _apply(svc, eng, _batch(1, T0, [_cpu(20.0, T0), fw]), T0)
    twins = {DEVICE: svc.get(DEVICE)}
    eng.tick(twins, lambda _d: "ONLINE", T0 + timedelta(seconds=30))
    assert _field(eng, "performance.cpu.usage_percent")["freshness"] == "RECENT"  # 5 s metric, 30 s old
    assert _field(eng, "security.firewall_enabled")["freshness"] == "LIVE"  # 5 min metric, 30 s old
    eng.tick(twins, lambda _d: "ONLINE", T0 + timedelta(seconds=120))
    assert _field(eng, "performance.cpu.usage_percent")["freshness"] == "STALE"
    assert _field(eng, "security.firewall_enabled")["freshness"] == "LIVE"


def test_unsupported_metric_is_never_zero() -> None:
    svc, eng = _twins(), TwinEngine(epoch="e1")
    temp = sample(
        "cpu.temperature_c", None, "celsius", ts=T0, available=False, reason="Needs LibreHardwareMonitor"
    )
    _apply(svc, eng, _batch(1, T0, [temp]), T0)
    f = _field(eng, "performance.cpu.temperature_c")
    assert f["value"] is None and f["freshness"] == "UNSUPPORTED" and "LibreHardwareMonitor" in f["reason"]
    assert _field(eng, "battery.charge_percent")["freshness"] == "UNKNOWN"  # never reported


def test_offline_connectivity_health_unknown_and_events() -> None:
    svc, eng = _twins(), TwinEngine(epoch="e1")
    _apply(svc, eng, _batch(1, T0, [_cpu(20.0, T0), _mem(93.0, T0)]), T0)
    doc = eng.docs[DEVICE]
    assert doc.state["connectivity.status"] == "ONLINE"
    assert doc.state["health"]["state"] == "WARNING"
    assert doc.state["health"]["reasons"][0]["rule"] == "memory"
    changes = eng.tick({DEVICE: svc.get(DEVICE)}, lambda _d: "OFFLINE", T0 + timedelta(minutes=10))
    assert changes and changes[0].connectivity == ("ONLINE", "OFFLINE")
    assert doc.state["health"]["state"] == "UNKNOWN" and doc.state["health"]["last_known"] == "WARNING"
    assert doc.state["sections.memory"]["visual"] == "offline"
    assert doc.state["performance.memory.usage_percent"]["freshness"] == "OFFLINE"
    assert doc.state["performance.memory.usage_percent"]["value"] == 93.0  # last known value kept
    assert any(
        e.type == "twin.connectivity" and e.message == "Device went offline" for e in changes[0].events
    )


def test_hysteresis_and_threshold_events() -> None:
    svc, eng = _twins(), TwinEngine(epoch="e1")
    seq = iter(range(1, 100))

    def mem(v: float, i: int) -> Any:
        ts = T0 + timedelta(seconds=5 * i)
        return _apply(svc, eng, _batch(next(seq), ts, [_mem(v, ts)]), ts)

    mem(50, 0)
    ch = mem(91, 1)
    assert _field(eng, "performance.memory.usage_percent")["status"] == "warning"
    assert ch is not None and ch.events and "Memory usage reached 91%" in ch.events[0].message
    ch = mem(88, 2)  # below 90 but within the 3-point hysteresis: still warning, no event
    assert _field(eng, "performance.memory.usage_percent")["status"] == "warning"
    assert ch is not None and not [e for e in ch.events if e.type == "twin.threshold"]
    ch = mem(80, 3)
    assert _field(eng, "performance.memory.usage_percent")["status"] == "elevated"
    assert ch is not None and "returned to 80%" in ch.events[0].message


def test_battery_low_only_matters_on_battery() -> None:
    svc, eng = _twins(), TwinEngine(epoch="e1")
    on_ac = [
        sample("battery.charge_percent", 4.0, component="battery", ts=T0),
        sample("power.source", "ac", "state", component="battery", ts=T0),
    ]
    _apply(svc, eng, _batch(1, T0, on_ac), T0)
    assert _field(eng, "battery.charge_percent")["status"] == "normal"
    ts = T0 + timedelta(seconds=5)
    on_batt = [
        sample("battery.charge_percent", 4.0, component="battery", ts=ts),
        sample("power.source", "battery", "state", component="battery", ts=ts),
        sample("battery.charging_state", "discharging", "state", component="battery", ts=ts),
    ]
    _apply(svc, eng, _batch(2, ts, on_batt), ts)
    assert _field(eng, "battery.charge_percent")["status"] == "critical"
    assert eng.docs[DEVICE].state["health"]["state"] == "CRITICAL"


def test_restore_keeps_last_known_state_with_honest_freshness() -> None:
    svc, eng = _twins(), TwinEngine(epoch="e1")
    _apply(svc, eng, _batch(1, T0, [_cpu(42.0, T0)]), T0)
    raw = eng.dump(DEVICE)
    assert raw is not None
    fresh_svc, fresh = _twins(), TwinEngine(epoch="e2")  # backend restarted: no readings in memory
    fresh.restore(DEVICE, raw)
    fresh.tick({DEVICE: fresh_svc.get(DEVICE)}, lambda _d: "STALE", T0 + timedelta(minutes=3))
    snap = fresh.snapshot(DEVICE)
    assert snap is not None and snap["restored_from_cache"] and snap["epoch"] == "e2"
    cpu = snap["state"]["performance"]["cpu"]["usage_percent"]
    assert cpu["value"] == 42.0 and cpu["freshness"] == "STALE"
    # new telemetry without CPU does not wipe the restored CPU value
    m = _mem(50.0, T0 + timedelta(minutes=3))
    fresh_svc.update(_batch(2, T0 + timedelta(minutes=3), [m]), T0 + timedelta(minutes=3))
    fresh.project(fresh_svc.get(DEVICE), T0 + timedelta(minutes=3), "ONLINE")
    assert fresh.docs[DEVICE].state["performance.cpu.usage_percent"]["value"] == 42.0


# ------------------------------------------------------------------ API + WebSocket


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(settings())) as c:
        c.post(
            "/api/v1/ingest/inventory",
            json={
                "device_id": DEVICE,
                "agent_version": "t",
                "discovered_at": T0.isoformat(),
                "inventory": INVENTORY,
            },
            headers=KEY,
        )
        yield c


def _post(c: TestClient, seq: int, samples: list[dict[str, Any]], device: str = DEVICE) -> None:
    b = batch(samples, seq=seq)
    b.update({"batch_id": f"{device}-{seq}", "device_id": device, "schema_version": "1.2"})
    assert c.post("/api/v1/ingest/telemetry/bulk", json={"batches": [b]}, headers=KEY).status_code == 200


def test_twin_api_timeline_explain_and_history(client: TestClient) -> None:
    now = datetime.now(UTC)
    _post(client, 1, [_cpu(20.0, now), _mem(50.0, now)])
    _post(client, 2, [_cpu(22.0, now + timedelta(seconds=1)), _mem(96.0, now + timedelta(seconds=1))])
    snap = client.get(f"/api/v1/devices/{DEVICE}/twin").json()
    assert snap["twin_version"] >= 2 and snap["state"]["identity"]["device_id"] == DEVICE
    assert snap["state"]["performance"]["memory"]["usage_percent"]["status"] == "critical"
    assert snap["state"]["health"]["state"] == "CRITICAL"
    explain = client.get(
        f"/api/v1/devices/{DEVICE}/twin/explain", params={"path": "performance.memory.usage_percent"}
    ).json()
    assert explain["source_reading"]["value"] == 96.0 and explain["severity_rule"]["critical"] == 95
    timeline = client.get(f"/api/v1/devices/{DEVICE}/timeline").json()["items"]
    assert any("Memory usage reached 96%" in e["message"] for e in timeline)
    hist = client.get(
        f"/api/v1/devices/{DEVICE}/history",
        params={"fields": ["performance.cpu.usage_percent"], "range": "15m"},
    )
    assert hist.status_code == 200 and "performance.cpu.usage_percent" in hist.json()["series"]
    listing = client.get(
        "/api/v1/devices", params={"q": "thinkpad", "sort": "memory", "order": "desc"}
    ).json()
    assert (
        listing["total"] == 1
        and listing["items"][0]["memory"] == 96.0
        and listing["items"][0]["health"] == "CRITICAL"
    )
    assert client.get("/api/v1/devices", params={"health": "HEALTHY"}).json()["total"] == 0
    fleet = client.get("/api/v1/fleet/summary").json()
    assert fleet["total"] == 1 and fleet["by_health"] == {"CRITICAL": 1}


def test_websocket_snapshot_then_versioned_patches(client: TestClient) -> None:
    now = datetime.now(UTC)
    _post(client, 1, [_cpu(20.0, now)])
    with client.websocket_connect("/ws/twin") as ws:
        ws.send_json({"type": "subscribe", "topics": [f"device:{DEVICE}"]})
        snap = _until(ws, "twin.snapshot")
        v = snap["twin"]["twin_version"]
        _post(client, 2, [_cpu(93.0, now + timedelta(seconds=1))])
        patch = _until(ws, "twin.state.patch")
        assert (
            patch["base_version"] == v
            and patch["twin_version"] == v + 1
            and patch["epoch"] == snap["twin"]["epoch"]
        )
        cpu = patch["merge"]["performance.cpu.usage_percent"]  # partial field update
        assert cpu["value"] == 93.0 and cpu["status"] == "warning"
        assert "unit" not in cpu and "interval_s" not in cpu  # unchanged sub-keys are not resent
        assert set(cpu["source"]) <= {"batch_id", "sequence", "received_at"}
        assert "performance.memory.usage_percent" not in patch["merge"]  # only what changed
        assert "performance.memory.usage_percent" not in patch["changes"]
        ws.send_json({"type": "twin.sync", "device_id": DEVICE})  # recovery after a gap
        again = _until(ws, "twin.snapshot")
        assert again["twin"]["twin_version"] == v + 1


def _until(ws: Any, event: str, limit: int = 40) -> dict[str, Any]:
    for _ in range(limit):
        m = ws.receive_json()
        if m["event"] == event:
            return m
    raise AssertionError(event)


# ------------------------------------------------------------------ authorization


PASSWORD = "Correct-Horse-9-Battery"


@pytest.fixture
def accounts() -> Iterator[TestClient]:
    s = settings(AUTH_MODE="accounts", JWT_SECRET="x" * 40)
    with TestClient(create_app(s)) as c:
        yield c


def _login(c: TestClient, user: str) -> dict[str, str]:
    r = c.post("/api/v1/auth/login", json={"username": user, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_employee_sees_only_assigned_device(accounts: TestClient) -> None:
    c = accounts
    admin = {
        "Authorization": "Bearer "
        + c.post("/api/v1/auth/setup", json={"username": "admin", "password": PASSWORD}).json()[
            "access_token"
        ]
    }
    for device in ("dev-mine", "dev-other"):
        env = {
            "device_id": device,
            "agent_version": "t",
            "discovered_at": T0.isoformat(),
            "inventory": INVENTORY,
        }
        assert c.post("/api/v1/ingest/inventory", json=env, headers=KEY).status_code == 202
        _post(c, 1, [_cpu(10.0, datetime.now(UTC))], device=device)
    assert (
        c.post(
            "/api/v1/users", json={"username": "ana", "password": PASSWORD, "role": "employee"}, headers=admin
        ).status_code
        == 201
    )
    r = c.put(
        "/api/v1/devices/dev-mine/assignment",
        json={"username": "ana", "employee_name": "Ana Silva"},
        headers=admin,
    )
    assert r.status_code == 200
    emp = _login(c, "ana")
    assert [d["device_id"] for d in c.get("/api/v1/devices", headers=emp).json()["items"]] == ["dev-mine"]
    assert (
        c.get("/api/v1/devices/dev-mine/twin", headers=emp).json()["state"]["identity"]["owner"]
        == "Ana Silva"
    )
    for path in (
        "/api/v1/devices/dev-other/twin",
        "/api/v1/devices/dev-other/timeline",
        "/api/v1/devices/dev-other",
    ):
        assert c.get(path, headers=emp).status_code == 404  # indistinguishable from an unknown device
    assert c.get("/api/v1/twin", params={"device_id": "dev-other"}, headers=emp).status_code == 404
    assert (
        c.get("/api/v1/telemetry/latest", params={"device_id": "dev-other"}, headers=emp).status_code == 404
    )
    assert c.get("/api/v1/twin", headers=emp).json()["device_id"] == "dev-mine"  # default = own device
    assert c.get("/api/v1/pipeline/stats", headers=emp).status_code == 403
    assert c.get("/api/v1/workspaces", headers=emp).status_code == 403
    assert c.put("/api/v1/devices/dev-mine/assignment", json={}, headers=emp).status_code == 403
    assert c.get("/api/v1/fleet/summary", headers=emp).json()["total"] == 1
    assert len(c.get("/api/v1/devices", headers=admin).json()["items"]) == 2  # staff see everything
    token = emp["Authorization"].removeprefix("Bearer ")
    with c.websocket_connect(f"/ws/twin?token={token}") as ws:
        ws.send_json({"type": "subscribe", "topics": ["device:dev-other", "device:dev-mine", "fleet"]})
        sub = _until(ws, "subscribed")
        assert {"topic": "device:dev-other", "reason": "unknown_device"} in sub["rejected"]
        fleet = _until(ws, "fleet_snapshot")
        assert [d["device_id"] for d in fleet["devices"]] == ["dev-mine"]
        _post(c, 2, [_cpu(99.0, datetime.now(UTC))], device="dev-other")
        _post(c, 2, [_cpu(50.0, datetime.now(UTC))], device="dev-mine")
        for _ in range(30):
            m = ws.receive_json()
            assert m.get("device_id") in (None, "dev-mine"), m["event"]
            if m["event"] == "twin.state.patch":
                break


def test_unwatched_devices_do_not_serialise_high_volume_deltas(client: TestClient) -> None:
    now = datetime.now(UTC)
    _post(client, 1, [_cpu(20.0, now)])  # nobody connected: patches/telemetry deltas are skipped
    skipped = client.get("/api/v1/pipeline/stats").json()["websocket"]["fanout_skipped_no_viewer"]
    assert skipped >= 2  # telemetry_update + twin.state.patch
    with client.websocket_connect("/ws/twin") as ws:
        ws.send_json({"type": "subscribe", "topics": [f"device:{DEVICE}"]})
        _until(ws, "twin.snapshot")
        _post(client, 2, [_cpu(30.0, now + timedelta(seconds=1))])
        assert _until(ws, "twin.state.patch")["merge"]["performance.cpu.usage_percent"]["value"] == 30.0
