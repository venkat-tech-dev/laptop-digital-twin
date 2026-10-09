"""Phase-1 agent ingest: registration, device tokens, bulk upload, de-duplication, replay safety, events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AGENT_KEY, DEVICE, batch, inventory_envelope, typical_samples

H = {"X-Agent-Key": AGENT_KEY}


def _register(client: TestClient) -> dict[str, str]:
    r = client.post("/api/v1/agent/register", json={"device_id": DEVICE, "agent_version": "t"}, headers=H)
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['device_token']}", "X-Device-Id": DEVICE}


def _batch(seq: int, ts: datetime, **extra: Any) -> dict[str, Any]:
    b = batch(typical_samples(ts), seq=seq)
    b.update({"batch_id": f"batch-{seq}", **extra})
    return b


def test_registration_requires_enrollment_key_and_issues_working_token(client: TestClient) -> None:
    assert (
        client.post("/api/v1/agent/register", json={"device_id": DEVICE, "agent_version": "t"}).status_code
        == 401
    )
    auth = _register(client)
    assert client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=auth).status_code == 202
    bad = {**auth, "Authorization": "Bearer wrong"}
    assert client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=bad).status_code == 401
    # a device token cannot submit data for another device
    other = {**auth, "X-Device-Id": DEVICE}
    env = inventory_envelope()
    env["device_id"] = "ldt-someone-else"
    assert client.post("/api/v1/ingest/inventory", json=env, headers=other).status_code == 403


def test_re_registration_rotates_the_token(client: TestClient) -> None:
    first = _register(client)
    second = _register(client)
    assert (
        client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=first).status_code == 401
    )
    assert (
        client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=second).status_code == 202
    )


def test_bulk_upload_acknowledges_each_batch_and_deduplicates(client: TestClient) -> None:
    auth = _register(client)
    client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=auth)
    now = datetime.now(UTC)
    good = [_batch(i, now - timedelta(seconds=10 - i)) for i in range(3)]
    invalid = {"batch_id": "broken", "device_id": DEVICE}  # missing required fields
    r = client.post("/api/v1/ingest/telemetry/bulk", json={"batches": [*good, invalid]}, headers=auth)
    assert r.status_code == 200
    statuses = {x["batch_id"]: x["status"] for x in r.json()["results"]}
    assert statuses == {
        "batch-0": "accepted",
        "batch-1": "accepted",
        "batch-2": "accepted",
        "broken": "rejected",
    }
    # a retry after a lost response is acknowledged, not applied twice
    again = client.post("/api/v1/ingest/telemetry/bulk", json={"batches": good[:1]}, headers=auth).json()
    assert again["results"][0]["status"] == "duplicate"
    assert client.post("/api/v1/ingest/telemetry/bulk", json={"batches": []}, headers=auth).status_code == 422


def test_bulk_for_unknown_device_returns_409(client: TestClient) -> None:
    r = client.post(
        "/api/v1/ingest/telemetry/bulk", json={"batches": [_batch(1, datetime.now(UTC))]}, headers=H
    )
    assert r.status_code == 409


def test_replayed_backlog_never_overwrites_live_state(client: TestClient) -> None:
    client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=H)
    now = datetime.now(UTC)
    live = batch(typical_samples(now, cpu=80.0), seq=10)
    live["batch_id"] = "live"
    live["processes"] = {
        "timestamp": now.isoformat(),
        "source": "t",
        "total_processes": 1,
        "processes": [
            {
                "pid": 1,
                "name": "live.exe",
                "status": "running",
                "cpu_percent": 5.0,
                "memory_rss_bytes": 1,
                "memory_percent": 0.1,
                "num_threads": 1,
                "io_read_bytes_per_sec": 0.0,
                "io_write_bytes_per_sec": 0.0,
            }
        ],
    }
    assert client.post("/api/v1/ingest/telemetry", json=live, headers=H).status_code == 202
    old = batch(typical_samples(now - timedelta(hours=2), cpu=5.0), seq=3)
    old.update({"batch_id": "old", "replay": True})
    old["processes"] = {
        **live["processes"],
        "processes": [{**live["processes"]["processes"][0], "name": "old.exe"}],
    }
    assert client.post("/api/v1/ingest/telemetry/bulk", json={"batches": [old]}, headers=H).status_code == 200
    latest = client.get("/api/v1/telemetry/latest").json()
    assert latest["cpu.usage_percent"]["value"] == 80.0  # live value kept
    procs = client.get("/api/v1/system/processes").json()["processes"]
    assert procs[0]["name"] == "live.exe"  # replayed process list ignored


def test_events_and_health_are_exposed(client: TestClient) -> None:
    client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=H)
    now = datetime.now(UTC)
    b = _batch(1, now)
    b["events"] = [
        {
            "event_id": "evt-app-1",
            "type": "app_crash",
            "severity": "error",
            "timestamp": now.isoformat(),
            "source": "Windows event log",
            "message": "app.exe crashed",
            "data": {"application": "app.exe"},
        }
    ]
    b["device_health"] = {
        "state": "WARNING",
        "reasons": ["Secure Boot is disabled"],
        "checks": {"secure_boot": "WARNING"},
        "evaluated_at": now.isoformat(),
    }
    b["agent_health"] = {
        "agent_version": "1.2.0",
        "run_mode": "service",
        "started_at": now.isoformat(),
        "uptime_s": 12.0,
        "queue_depth": 3,
        "collectors": [{"name": "cpu", "lane": "fast", "interval_ms": 5000}],
    }
    assert client.post("/api/v1/ingest/telemetry", json=b, headers=H).status_code == 202
    state = client.get("/api/v1/endpoint").json()
    assert state["device_health"]["state"] == "WARNING"
    assert state["agent_health"]["run_mode"] == "service" and state["agent_health"]["queue_depth"] == 3
    assert state["events"][0]["type"] == "app_crash" and state["events"][0]["message"] == "app.exe crashed"
    # an older health evaluation replayed later does not roll the state back
    old = _batch(2, now - timedelta(hours=1), replay=True)
    old["device_health"] = {
        **b["device_health"],
        "state": "HEALTHY",
        "evaluated_at": (now - timedelta(hours=1)).isoformat(),
    }
    client.post("/api/v1/ingest/telemetry/bulk", json={"batches": [old]}, headers=H)
    assert client.get("/api/v1/endpoint").json()["device_health"]["state"] == "WARNING"


def test_revoked_device_cannot_ingest(client: TestClient) -> None:
    auth = _register(client)
    client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=auth)
    assert client.delete(f"/api/v1/endpoint/credentials/{DEVICE}").status_code == 200
    assert client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=auth).status_code == 401
