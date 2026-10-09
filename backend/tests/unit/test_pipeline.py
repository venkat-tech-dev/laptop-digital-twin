"""Phase-2 telemetry pipeline: contract versioning, acknowledgements, sequences, limits, gzip,
presence, WebSocket subscriptions, multi-device isolation and durable idempotency."""

from __future__ import annotations

import gzip
import json
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.base import SystemEventRecord
from app.repositories.memory import MemoryEventRepository
from app.services.presence import PresenceService
from app.services.sequences import SequenceTracker
from tests.conftest import AGENT_KEY, INVENTORY, batch, settings, typical_samples

KEY = {"X-Agent-Key": AGENT_KEY}


def _client(**overrides: Any) -> Iterator[TestClient]:
    with TestClient(create_app(settings(**overrides))) as c:
        yield c


@pytest.fixture
def strict() -> Iterator[TestClient]:
    """Production-like: enrollment key may only register; ingest needs the device token."""
    yield from _client(ALLOW_ENROLLMENT_KEY_INGEST=False)


def register(client: TestClient, device: str) -> dict[str, str]:
    r = client.post("/api/v1/agent/register", json={"device_id": device, "agent_version": "t"}, headers=KEY)
    assert r.status_code == 200, r.text
    auth = {"Authorization": f"Bearer {r.json()['device_token']}", "X-Device-Id": device}
    env = {
        "schema_version": "1.1",
        "device_id": device,
        "agent_version": "t",
        "discovered_at": datetime.now(UTC).isoformat(),
        "inventory": INVENTORY,
    }
    assert client.post("/api/v1/ingest/inventory", json=env, headers=auth).status_code == 202
    return auth


def b(device: str, seq: int, ts: datetime | None = None, **extra: Any) -> dict[str, Any]:
    out = batch(typical_samples(ts or datetime.now(UTC)), seq=seq)
    out.update({"device_id": device, "batch_id": f"{device}-{seq}", "schema_version": "1.1", **extra})
    return out


def bulk(client: TestClient, auth: dict[str, str], batches: list[dict[str, Any]]) -> Any:
    return client.post("/api/v1/ingest/telemetry/bulk", json={"batches": batches}, headers=auth)


# ------------------------------------------------------------------ contract / auth


def test_enrollment_key_cannot_ingest_but_device_token_can(strict: TestClient) -> None:
    env = {
        "device_id": "dev-a",
        "agent_version": "t",
        "discovered_at": datetime.now(UTC).isoformat(),
        "inventory": INVENTORY,
    }
    r = strict.post("/api/v1/ingest/inventory", json=env, headers=KEY)
    assert r.status_code == 401 and "Per-device token" in r.json()["detail"]
    auth = register(strict, "dev-a")
    assert bulk(strict, auth, [b("dev-a", 1)]).json()["accepted"] == 1


def test_schema_versions_and_categories(strict: TestClient) -> None:
    auth = register(strict, "dev-a")
    legacy = b("dev-a", 1)
    del legacy["schema_version"]  # Phase-1 agent -> treated as 1.0
    v11 = b("dev-a", 2)
    v11["samples"][0]["category"] = "performance"
    v11["events"] = [
        {
            "event_id": "e1",
            "type": "internet_lost",
            "severity": "warning",
            "timestamp": datetime.now(UTC).isoformat(),
            "source": "t",
            "message": "Internet lost",
            "priority": "high",
            "category": "performance",
        }
    ]
    future = b("dev-a", 3, schema_version="9.0")
    bad_category = b("dev-a", 4)
    bad_category["samples"][0]["category"] = "keystrokes"
    ack = bulk(strict, auth, [legacy, v11, future, bad_category]).json()
    by_id = {r["batch_id"]: r for r in ack["results"]}
    assert by_id["dev-a-1"]["status"] == "accepted" and by_id["dev-a-2"]["status"] == "accepted"
    assert by_id["dev-a-3"]["status"] == "rejected"
    assert by_id["dev-a-3"]["detail"].startswith("unsupported_schema_version")
    assert by_id["dev-a-4"]["detail"].startswith("invalid_schema")
    assert (ack["accepted"], ack["duplicates"], ack["rejected"], ack["last_sequence"]) == (2, 0, 2, 2)
    assert ack["server_received_at"]


def test_ack_summary_duplicates_and_sequence_tracking(strict: TestClient) -> None:
    auth = register(strict, "dev-a")
    assert bulk(strict, auth, [b("dev-a", 1), b("dev-a", 2), b("dev-a", 5)]).json()["accepted"] == 3
    ack = bulk(strict, auth, [b("dev-a", 2), b("dev-a", 3, replay=True)]).json()  # lost-ack retry + replay
    assert (ack["accepted"], ack["duplicates"], ack["last_sequence"]) == (1, 1, 5)
    seq = strict.get("/api/v1/pipeline/stats").json()["sequences"]["dev-a"]
    assert seq["gaps_detected"] == 1 and seq["out_of_order"] == 1 and seq["duplicates"] == 1
    assert seq["missing"] == 1  # 4 never arrived, 3 filled the gap


def test_future_timestamps_rejected_but_moderate_drift_tracked(strict: TestClient) -> None:
    auth = register(strict, "dev-a")
    ahead = datetime.now(UTC) + timedelta(days=3)
    skewed = datetime.now(UTC) - timedelta(minutes=10)  # device clock 10 min behind
    ack = bulk(
        strict,
        auth,
        [b("dev-a", 1, ts=ahead, sent_at=ahead.isoformat()), b("dev-a", 2, sent_at=skewed.isoformat())],
    ).json()
    assert ack["results"][0]["detail"].startswith("timestamp_in_future") and ack["accepted"] == 1
    stats = strict.get("/api/v1/pipeline/stats").json()
    assert stats["sequences"]["dev-a"]["clock_drift_s"] > 500 and "dev-a" in stats["clock_drift_warnings"]


# --------------------------------------------------------------- transport limits


def test_gzip_bodies_and_size_limits() -> None:
    for c in _client(
        ALLOW_ENROLLMENT_KEY_INGEST=False, INGEST_MAX_BODY_BYTES=50_000, INGEST_MAX_DECOMPRESSED_BYTES=200_000
    ):
        auth = register(c, "dev-a")
        body = json.dumps({"batches": [b("dev-a", 1)]}).encode()
        r = c.post(
            "/api/v1/ingest/telemetry/bulk",
            content=gzip.compress(body),
            headers={**auth, "Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
        assert r.status_code == 200 and r.json()["accepted"] == 1
        bad = c.post(
            "/api/v1/ingest/telemetry/bulk",
            content=b"\x1f\x8bnot-gzip",
            headers={**auth, "Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
        assert bad.status_code == 400
        bomb = gzip.compress(b'{"batches": [' + b" " * 1_000_000 + b"]}")
        assert len(bomb) < 50_000
        r = c.post(
            "/api/v1/ingest/telemetry/bulk",
            content=bomb,
            headers={**auth, "Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
        assert r.status_code == 413
        big = c.post(
            "/api/v1/ingest/telemetry/bulk",
            content=b"x" * 60_000,
            headers={**auth, "Content-Type": "application/json"},
        )
        assert big.status_code == 413
        r = c.post(
            "/api/v1/ingest/telemetry/bulk",
            content=zlib.compress(body),
            headers={**auth, "Content-Encoding": "br", "Content-Type": "application/json"},
        )
        assert r.status_code == 415


def test_per_device_rate_limit_returns_retry_after() -> None:
    for c in _client(
        ALLOW_ENROLLMENT_KEY_INGEST=False, INGEST_RATE_PER_DEVICE_PER_MIN=6, INGEST_RATE_BURST=3
    ):
        a = register(c, "dev-a")  # inventory consumed one token
        other = register(c, "dev-b")
        codes = [bulk(c, a, [b("dev-a", i)]).status_code for i in range(1, 5)]
        assert codes[:2] == [200, 200] and codes[-1] == 429
        r = bulk(c, a, [b("dev-a", 9)])
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
        assert bulk(c, other, [b("dev-b", 1)]).status_code == 200  # other devices unaffected


# ------------------------------------------------------------- multi-device / presence


def test_device_isolation_and_fleet_apis(strict: TestClient) -> None:
    a, bb = register(strict, "dev-a"), register(strict, "dev-b")
    assert bulk(strict, a, [b("dev-b", 1)]).status_code == 403  # a's token cannot write b
    bulk(strict, a, [b("dev-a", 1, ts=datetime.now(UTC))])
    bulk(strict, bb, [b("dev-b", 1, samples=[])])
    devices = {d["device_id"]: d for d in strict.get("/api/v1/devices").json()["items"]}
    assert set(devices) == {"dev-a", "dev-b"} and devices["dev-a"]["presence"] == "ONLINE"
    assert devices["dev-a"]["primary"] and not devices["dev-b"]["primary"]  # primary stays stable
    state = strict.get("/api/v1/devices/dev-a/state").json()
    assert state["device_id"] == "dev-a" and state["sequence"]["last_sequence"] == 1
    assert strict.get("/api/v1/devices/nope/state").status_code == 404


def test_heartbeat_drives_presence(strict: TestClient) -> None:
    auth = register(strict, "dev-a")
    sent = datetime.now(UTC) - timedelta(seconds=2)
    r = strict.post(
        "/api/v1/agent/heartbeat",
        json={"device_id": "dev-a", "agent_version": "1.3.0", "sent_at": sent.isoformat(), "queue_depth": 4},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["presence"] == "ONLINE" and r.json()["clock_offset_s"] >= 2
    hb = {"device_id": "dev-b", "agent_version": "1", "sent_at": sent.isoformat()}
    assert strict.post("/api/v1/agent/heartbeat", json=hb, headers=auth).status_code == 403
    endpoint = strict.get("/api/v1/endpoint", params={"device_id": "dev-a"}).json()
    assert endpoint["presence"]["heartbeat"]["queue_depth"] == 4


def test_presence_thresholds() -> None:
    p = PresenceService(stale_after_s=30, offline_after_s=120)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    p.known("never")
    _, change = p.heartbeat("dev", {}, t0)
    assert change is not None and change.presence == "ONLINE"
    assert [c.presence for c in p.evaluate(t0 + timedelta(seconds=31))] == ["STALE"]
    assert [c.presence for c in p.evaluate(t0 + timedelta(seconds=121))] == ["OFFLINE"]
    assert p.presence_of("never") == "UNKNOWN"
    assert p.batch_received("dev", t0 + timedelta(seconds=200)) is not None
    assert p.presence_of("dev") == "ONLINE"


def test_sequence_reset_detection() -> None:
    t = SequenceTracker()
    now = datetime.now(UTC)
    for s in (50_000, 50_001):
        t.observe("d", s, now, now)
    assert t.observe("d", 1, now, now) == "reset"  # reinstall: sequence restarted
    assert t.observe("d", 2, now, now) == "in_order"


# ------------------------------------------------------------------ WebSocket routing


def _drain_until(ws: Any, event: str, limit: int = 30) -> dict[str, Any]:
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["event"] == event:
            return msg
    raise AssertionError(f"no {event}")


def test_websocket_subscriptions_route_per_device(strict: TestClient) -> None:
    a, bb = register(strict, "dev-a"), register(strict, "dev-b")
    with strict.websocket_connect("/ws/twin") as ws:
        assert _drain_until(ws, "connection_status")["primary_device_id"] == "dev-a"
        ws.send_json({"type": "subscribe", "topics": ["device:dev-b", "device:ghost", "fleet"]})
        sub = _drain_until(ws, "subscribed")
        assert sub["accepted"] == ["device:dev-b", "fleet"]
        assert sub["rejected"] == [{"topic": "device:ghost", "reason": "unknown_device"}]
        snap = _drain_until(ws, "twin_snapshot")
        while snap["device_id"] != "dev-b":
            snap = _drain_until(ws, "twin_snapshot")
        _drain_until(ws, "fleet_snapshot")
        bulk(strict, a, [b("dev-a", 1)])  # not subscribed to a: only low-volume fleet events
        bulk(strict, bb, [b("dev-b", 1)])
        upd = _drain_until(ws, "telemetry_update")
        assert upd["device_id"] == "dev-b"
        assert set(upd["timing"]) >= {"collected_at", "server_received_at", "published_at"}
        ws.send_json(
            {
                "type": "ping",
                "latency": {"websocket_delivery_ms": [3.5, -1, "x"], "end_to_end_latency_ms": [120.0]},
            }
        )
        _drain_until(ws, "pong")
    lat = strict.get("/api/v1/pipeline/stats").json()["ingest"]["latency"]
    assert lat["websocket_delivery_ms"]["count"] == 1 and lat["end_to_end_latency_ms"]["p50"] == 120.0
    assert lat["server_processing_ms"]["count"] >= 2 and lat["ws_queue_ms"]["count"] >= 1


def test_legacy_websocket_client_gets_primary_device_only(strict: TestClient) -> None:
    a, bb = register(strict, "dev-a"), register(strict, "dev-b")
    bulk(strict, a, [b("dev-a", 1)])  # first live device becomes primary and stays primary
    with strict.websocket_connect("/ws/twin") as ws:
        _drain_until(ws, "connection_status")
        bulk(strict, bb, [b("dev-b", 1)])
        bulk(strict, a, [b("dev-a", 2)])
        bulk(strict, bb, [b("dev-b", 2)])
        bulk(strict, a, [b("dev-a", 3)])
        first, second = _drain_until(ws, "telemetry_update"), _drain_until(ws, "telemetry_update")
        assert (first["device_id"], first["sequence"]) == ("dev-a", 2)
        assert (second["device_id"], second["sequence"]) == ("dev-a", 3)


# ------------------------------------------------------------------ idempotency


async def test_device_events_persist_once_per_event_id() -> None:
    repo = MemoryEventRepository()
    rec = SystemEventRecord("d", datetime.now(UTC), "device.app_crash", "error", "x", {}, event_uid="e-1")
    await repo.add_system_event(rec)
    await repo.add_system_event(rec)  # replay after a backend restart
    assert len(await repo.list_system_events("d", 10)) == 1


# ------------------------------------------------------------------ overload protection


def test_overload_returns_503_with_retry_after_and_defers_backlog_first() -> None:
    for c in _client(ALLOW_ENROLLMENT_KEY_INGEST=False, INGEST_MAX_INFLIGHT=1):
        auth = register(c, "dev-a")
        container = c.app.state.container  # type: ignore[attr-defined]
        container.ingest.inflight = 1  # simulate a request already being processed
        r = bulk(c, auth, [b("dev-a", 1)])
        assert r.status_code == 503 and int(r.headers["Retry-After"]) >= 2
        container.ingest.inflight = 0
        assert bulk(c, auth, [b("dev-a", 1)]).status_code == 200  # nothing lost: retried later
        # persistence saturated: backlog (replay) is deferred, live data still flows
        container.persister._queue.extend([("dev-a", None)] * int(container.persister.capacity * 0.9))
        assert bulk(c, auth, [b("dev-a", 2, replay=True)]).status_code == 503
        assert bulk(c, auth, [b("dev-a", 3)]).status_code == 200
        container.persister._queue.clear()
        stats = c.get("/api/v1/pipeline/stats").json()["ingest"]
        assert stats["counters"]["overloaded"] == 2 and stats["max_inflight"] == 1


def test_ingest_survives_credential_store_outage(strict: TestClient) -> None:
    """Database down: a verified (cached) token keeps ingesting; an unknown one gets 503, not 500."""
    a = register(strict, "dev-a")
    container = strict.app.state.container  # type: ignore[attr-defined]

    async def down(*_: object) -> None:
        raise ConnectionRefusedError("database unreachable")

    container.device_auth._ttl = 0.0  # force a lookup: exercise the stale-while-error path
    container.admin_repo.get_device_credential = down
    container.admin_repo.save_device_credential = down
    assert bulk(strict, a, [b("dev-a", 1)]).status_code == 200
    stranger = {"Authorization": "Bearer not-cached", "X-Device-Id": "dev-x"}
    r = bulk(strict, stranger, [b("dev-x", 1)])
    assert r.status_code == 503 and r.headers["Retry-After"] == "10"
    reg = strict.post(
        "/api/v1/agent/register", json={"device_id": "dev-y", "agent_version": "t"}, headers=KEY
    )
    assert reg.status_code == 503


async def test_graceful_stop_flushes_receipts() -> None:
    from app.core.container import build_container
    from app.services.ingest_pipeline import Receipt

    class Repo:
        def __init__(self) -> None:
            self.rows: list[Receipt] = []

        async def write_receipts(self, receipts: list[Receipt]) -> int:
            self.rows += receipts
            return len(receipts)

        async def recent_receipt_ids(self, since: datetime, limit: int) -> list[str]:
            return []

        async def purge_receipts_older_than(self, cutoff: datetime) -> int:
            return 0

    container = build_container(settings())
    repo = Repo()
    container.ingest.receipts._repo = repo
    await container.start()
    now = datetime.now(UTC)
    container.ingest.receipts.add(Receipt("b-1", "d", 1, "1.1", now, now, 1, 0, False))
    await container.stop()  # SIGTERM path: must not lose the buffered receipt
    assert [r.batch_id for r in repo.rows] == ["b-1"]
