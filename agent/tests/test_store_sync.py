"""Local SQLite outbox and synchronisation (no network: the API client is faked)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.contracts import InventoryEnvelope
from app.publisher.sync import SyncManager, mark_replay
from app.storage.queue import TelemetryStore
from app.transport.client import ApiError, BatchResult


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def payload(batch_id: str, **extra: Any) -> bytes:
    return json.dumps({"batch_id": batch_id, "device_id": "dev", "samples": [], **extra}).encode()


def store(tmp_path: Path, clock: Clock, **kw: Any) -> TelemetryStore:
    args = {"max_batches": 100, "max_bytes": 10_000_000, "max_age_s": 3600.0}
    args.update(kw)
    return TelemetryStore(tmp_path / "q.db", clock=clock, **args)


# ------------------------------------------------------------------------------------- store
def test_put_is_idempotent_and_persists_across_restart(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock)
    assert s.put("a", payload("a")) is True
    assert s.put("a", payload("a")) is False  # duplicate batch_id ignored
    s.set_state("sequence", "41")
    s.close()
    s2 = store(tmp_path, clock)  # agent / Windows restart
    assert s2.stats().depth == 1 and s2.get_state("sequence") == "41"
    (row,) = s2.due(10)
    assert json.loads(row.payload)["batch_id"] == "a"


def test_count_limit_thins_old_normal_batches_first(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock, max_batches=3)
    for i in range(5):
        s.put(f"b{i}", payload(f"b{i}"))
    ids = [r.batch_id for r in s.due(10)]
    assert ids == ["b0", "b3", "b4"]  # every other old batch removed; the trend survives
    assert s.stats().thinned_total == 2 and s.stats().dropped_total == 0


def test_backpressure_keeps_critical_high_and_latest(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock, max_batches=4)
    s.put("crit", payload("crit"), priority=3)
    s.put("high", payload("high"), priority=2)
    for i in range(10):
        s.put(f"n{i}", payload(f"n{i}"), priority=1)
    ids = {r.batch_id for r in s.due(20)}
    assert {"crit", "high", "n9"} <= ids and len(ids) == 4
    stats = s.stats()
    assert stats.by_priority == {"low": 0, "normal": 2, "high": 1, "critical": 1}
    assert stats.thinned_total + stats.dropped_total == 8


def test_high_priority_outlives_normal_age_limit(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock, max_age_s=100.0)
    s.put("normal", payload("normal"))
    s.put("high", payload("high"), priority=2)
    clock.t += 150
    s.put("new", payload("new"))
    assert [r.batch_id for r in s.due(10)] == ["high", "new"]  # backlog: priority first
    clock.t += 100  # beyond 2x max age
    s.put("newer", payload("newer"))
    assert "high" not in [r.batch_id for r in s.due(10)]


def test_v1_database_is_migrated_in_place(tmp_path: Path) -> None:
    import sqlite3
    import zlib

    db = sqlite3.connect(tmp_path / "q.db")
    db.executescript(
        "CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL UNIQUE, "
        "created_at REAL NOT NULL, size INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
        "next_attempt_at REAL NOT NULL DEFAULT 0, last_error TEXT, payload BLOB NOT NULL);"
        "CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO state VALUES ('schema_version', '1'), ('sequence', '239');"
    )
    blob = zlib.compress(payload("old"))
    db.execute(
        "INSERT INTO outbox(batch_id, created_at, size, payload) VALUES ('old', 0, ?, ?)", (len(blob), blob)
    )
    db.commit()
    db.close()
    s = TelemetryStore(tmp_path / "q.db", 100, 10**8, 10**9, clock=Clock())
    assert [r.batch_id for r in s.due(10)] == ["old"] and s.get_state("sequence") == "239"
    assert s.get_state("schema_version") == "3" and s.stats().by_priority["normal"] == 1


def test_age_limit(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock, max_age_s=100.0)
    s.put("old", payload("old"))
    clock.t += 200  # older than max_age
    s.put("new", payload("new"))
    assert [r.batch_id for r in s.due(10)] == ["new"]
    assert s.stats().dropped_total == 1


def test_byte_limit_keeps_newest(tmp_path: Path) -> None:
    import zlib

    one = len(zlib.compress(payload("b0"), 6))
    s = store(tmp_path, Clock(), max_bytes=int(one * 2.5))
    for i in range(4):
        s.put(f"b{i}", payload(f"b{i}"))
    assert [r.batch_id for r in s.due(10)] == ["b0", "b3"]  # thinned: newest kept, trend anchor kept
    assert s.stats().bytes <= int(one * 2.5)


def test_corrupt_database_is_quarantined_and_recreated(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    db.write_bytes(b"this is not a sqlite database" * 100)
    s = store(tmp_path, Clock())
    assert s.stats().depth == 0 and s.stats().recovered_from_corruption == 1
    assert any(p.name.startswith("q.db.corrupt-") for p in tmp_path.iterdir())
    assert s.put("x", payload("x"))


def test_undecodable_row_is_dead_lettered(tmp_path: Path) -> None:
    s = store(tmp_path, Clock())
    s.put("ok", payload("ok"))
    raw = sqlite3.connect(tmp_path / "q.db")
    raw.execute("INSERT INTO outbox(batch_id, created_at, size, payload) VALUES ('bad', 1e6, 3, x'000102')")
    raw.commit()
    raw.close()
    assert [r.batch_id for r in s.due(10)] == ["ok"]
    assert s.stats().dead_lettered_total == 1


# ------------------------------------------------------------------------------------- sync
class FakeClient:
    def __init__(self) -> None:
        self.up = True
        self.calls: list[list[dict[str, Any]]] = []
        self.inventories = 0
        self.reject: set[str] = set()
        self.conflict_once = False
        self.last_latency_ms = None
        self.detail = "bad"
        self.error: ApiError | None = None

    async def publish_inventory(self, envelope: InventoryEnvelope) -> None:
        if not self.up:
            raise ApiError("ConnectError", retryable=True)
        self.inventories += 1

    async def publish_batches(self, payloads: list[bytes]) -> list[BatchResult]:
        if not self.up:
            raise ApiError("ConnectError: backend down", retryable=True)
        if self.error is not None:
            raise self.error
        if self.conflict_once:
            self.conflict_once = False
            raise ApiError("Unknown device", 409, retryable=True)
        items = [json.loads(p) for p in payloads]
        self.calls.append(items)
        return [
            BatchResult(
                i["batch_id"],
                "rejected" if i["batch_id"] in self.reject else "accepted",
                self.detail if i["batch_id"] in self.reject else None,
            )
            for i in items
        ]


def envelope() -> InventoryEnvelope:
    return InventoryEnvelope(
        device_id="dev", agent_version="t", discovered_at=datetime.now(UTC), inventory={}
    )


def manager(s: TelemetryStore, c: FakeClient, clock: Clock, **kw: Any) -> SyncManager:
    args: dict[str, Any] = {"batch_size": 2, "max_attempts": 3, "backoff_base_s": 1.0, "backoff_max_s": 60.0}
    args.update(kw)
    return SyncManager(s, c, envelope, clock=clock, rng=lambda: 1.0, **args)  # type: ignore[arg-type]


async def test_offline_then_reconnect_uploads_newest_first_then_backlog(tmp_path: Path) -> None:
    clock = Clock()
    s, c = store(tmp_path, clock), FakeClient()
    sync = manager(s, c, clock, replay_after_s=30)
    c.up = False
    for i in range(5):
        s.put(f"b{i}", payload(f"b{i}"))
        clock.t += 10
        await sync.sync_once()
    assert s.stats().depth == 5 and not sync.online and sync.failures_consecutive >= 1  # nothing lost
    c.up = True
    clock.t += 1000  # past the backoff
    s.put("live", payload("live"))
    delivered = await sync.sync_once()
    assert delivered == 6 and s.stats().depth == 0 and sync.online
    first, *rest = c.calls
    assert [i["batch_id"] for i in first] == ["live"] and "replay" not in first[0]
    backlog = [i for call in rest for i in call]
    assert [i["batch_id"] for i in backlog] == ["b0", "b1", "b2", "b3", "b4"]
    assert all(i["replay"] is True for i in backlog)
    assert c.inventories == 1


async def test_backoff_grows_and_is_bounded(tmp_path: Path) -> None:
    clock = Clock()
    s, c = store(tmp_path, clock), FakeClient()
    sync = manager(s, c, clock, backoff_max_s=8.0)
    c.up = False
    s.put("a", payload("a"))
    delays = []
    for _ in range(6):
        await sync.sync_once()
        delays.append(round(sync._next_attempt - clock.t, 1))
        clock.t = sync._next_attempt
    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
    sync.retry_now()
    assert sync._next_attempt == 0.0


async def test_rejected_batch_is_dead_lettered_without_blocking_the_queue(tmp_path: Path) -> None:
    clock = Clock()
    s, c = store(tmp_path, clock), FakeClient()
    sync = manager(s, c, clock)
    c.reject = {"bad"}
    s.put("bad", payload("bad"))
    s.put("good", payload("good"))
    await sync.sync_once()  # a deterministic validation failure is never retried
    assert s.stats().depth == 0 and s.stats().dead_lettered_total == 1 and sync.rejected_total == 1
    assert sum(1 for call in c.calls for i in call if i["batch_id"] == "good") == 1


async def test_unsupported_schema_pauses_and_keeps_the_queue(tmp_path: Path) -> None:
    clock = Clock()
    s, c = store(tmp_path, clock), FakeClient()
    sync = manager(s, c, clock)
    c.detail = "unsupported_schema_version: 1.1 (supported: 1.0)"
    c.reject = {"a", "b"}
    s.put("a", payload("a"))
    s.put("b", payload("b"))
    await sync.sync_once()
    assert s.stats().depth == 2 and s.stats().dead_lettered_total == 0 and sync.paused_reason
    calls = len(c.calls)
    clock.t += 60
    sync.retry_now()  # connectivity signals do not override a schema pause
    await sync.sync_once()
    assert len(c.calls) == calls
    c.reject = set()  # backend upgraded
    clock.t += 3600
    await sync.sync_once()
    assert s.stats().depth == 0 and sync.paused_reason is None


async def test_retry_after_is_respected(tmp_path: Path) -> None:
    clock = Clock()
    s, c = store(tmp_path, clock), FakeClient()
    sync = manager(s, c, clock)
    c.error = ApiError("HTTP 429", 429, retryable=True, retry_after_s=120.0)
    s.put("a", payload("a"))
    await sync.sync_once()
    assert sync._next_attempt - clock.t >= 120.0 and s.stats().depth == 1
    c.error = None
    clock.t += 121
    await sync.sync_once()
    assert s.stats().depth == 0


async def test_chunks_are_capped_by_bytes(tmp_path: Path) -> None:
    clock = Clock()
    s, c = store(tmp_path, clock), FakeClient()
    one = len(payload("x0"))
    sync = manager(s, c, clock, batch_size=50, max_batch_bytes=int(one * 2.5), replay_after_s=-1)
    for i in range(5):
        s.put(f"x{i}", payload(f"x{i}"))
    await sync.sync_once()
    assert [len(call) for call in c.calls] == [2, 2, 1]


async def test_unknown_device_resends_inventory(tmp_path: Path) -> None:
    clock = Clock()
    s, c = store(tmp_path, clock), FakeClient()
    sync = manager(s, c, clock)
    s.put("a", payload("a"))
    c.conflict_once = True
    await sync.sync_once()
    assert s.stats().depth == 1 and c.inventories == 1
    clock.t += 1000
    await sync.sync_once()
    assert s.stats().depth == 0 and c.inventories == 2  # re-announced before retrying


def test_mark_replay_sets_flag() -> None:
    assert json.loads(mark_replay(payload("z")))["replay"] is True
