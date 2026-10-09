# ruff: noqa: E501  (inline test payloads)
"""Phase 10 reliability: supervised background loops, stuck-delivery recovery, concurrent duplicate batches,
bounded audit buffer, readiness that reflects background health."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import supervisor as sup
from app.domain.alerting.models import Notification, NotificationStatus
from app.repositories.alerting import MemoryAlertRepository
from app.services import audit as audit_mod
from tests.conftest import AGENT_KEY, batch, inventory_envelope, typical_samples


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sup, "BACKOFF_MIN_S", 0.01)
    monkeypatch.setattr(sup, "BACKOFF_MAX_S", 0.02)


# ------------------------------------------------------------------ supervisor
async def test_supervisor_restarts_a_failing_loop_and_flags_crash_loops() -> None:
    stop = asyncio.Event()
    s = sup.Supervisor(stop)
    calls = {"n": 0}

    async def flaky() -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("boom")
        await stop.wait()  # healthy after two failures

    async def always_failing() -> None:
        raise ValueError("broken")

    t1 = s.spawn("flaky", flaky, critical=True)
    t2 = s.spawn("broken", always_failing, critical=True)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if (
            s.tasks["broken"].state == "crash_looping"
            and s.tasks["flaky"].state == "running"
            and calls["n"] >= 3
        ):
            break
    assert calls["n"] >= 3 and s.tasks["flaky"].state == "running" and s.tasks["flaky"].restarts == 2
    st = s.status()
    assert st["status"] == "failing" and st["critical_crash_looping"] == ["broken"]
    assert "ValueError: broken" in st["not_running"]["broken"]["last_error"]
    stop.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), 2)
    assert s.tasks["flaky"].state == "stopped"


async def test_supervisor_treats_an_early_return_as_a_failure() -> None:
    stop = asyncio.Event()
    s = sup.Supervisor(stop)
    runs = {"n": 0}

    async def returns() -> None:
        runs["n"] += 1

    t = s.spawn("returns", returns)
    await asyncio.sleep(0.1)
    assert runs["n"] >= 2 and "returned before shutdown" in (s.tasks["returns"].last_error or "")
    stop.set()
    await asyncio.wait_for(t, 2)


async def test_watch_raises_when_a_subtask_dies() -> None:
    stop = asyncio.Event()

    async def dies() -> None:
        raise KeyError("x")

    async def lives() -> None:
        await stop.wait()

    tasks = [asyncio.create_task(dies(), name="dies"), asyncio.create_task(lives(), name="lives")]
    with pytest.raises(RuntimeError, match="subtask dies stopped"):
        await sup.watch(tasks, stop)
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    stop2 = asyncio.Event()
    t = asyncio.create_task(stop2.wait())
    stop2.set()
    await sup.watch([t], stop2)  # normal shutdown: no error


# ------------------------------------------------------------------ notifications
def _note(i: int, status: NotificationStatus, at: datetime) -> Notification:
    return Notification(
        f"n{i}",
        "default",
        "a1",
        "u",
        "d",
        "in_app",
        status,
        2,
        "HIGH",
        "anomaly",
        "t",
        "b",
        {},
        f"k{i}",
        at,
        at,
    )


async def test_deliveries_stuck_in_sending_are_requeued_and_backlog_is_measured() -> None:
    repo = MemoryAlertRepository()
    now = datetime.now(UTC)
    old = now - timedelta(minutes=10)
    for i in range(3):
        await repo.insert_notification(_note(i, NotificationStatus.PENDING, old))
    claimed = await repo.claim_due(old, 3)
    assert len(claimed) == 3 and all(n.status is NotificationStatus.SENDING for n in claimed)
    next(n for n in claimed if n.notification_id == "n2").updated_at = now  # within the provider timeout
    assert await repo.requeue_stuck(now - timedelta(minutes=5), now) == 2
    statuses = sorted(n.status.value for n in repo.notifications.values())
    assert statuses == ["RETRYING", "RETRYING", "SENDING"]
    count, oldest = await repo.backlog(now)
    assert count == 2 and oldest is not None and 590 < oldest < 610
    again = await repo.claim_due(now, 10)
    assert {n.notification_id for n in again} == {"n0", "n1"}  # delivered again (at-least-once)


# ------------------------------------------------------------------ ingest
def test_concurrent_copies_of_one_batch_are_applied_once(client: TestClient) -> None:
    h = {"X-Agent-Key": AGENT_KEY}
    assert client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=h).status_code == 202
    c = client.app.state.container  # type: ignore[attr-defined]
    from app.schemas.ingest import TelemetryBatchIn

    body = batch(typical_samples(), seq=7)
    body["batch_id"] = "batch-concurrent-1"
    b = TelemetryBatchIn.model_validate(body)
    calls = {"n": 0}
    original = c.telemetry.ingest

    async def slow_ingest(*args: Any, **kw: Any) -> int:
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return int(await original(*args, **kw))

    c.telemetry.ingest = slow_ingest

    async def both() -> list[tuple[str, int]]:
        now = datetime.now(UTC)
        return list(await asyncio.gather(c.ingest.apply(b, now), c.ingest.apply(b, now)))

    results = client.portal.call(both)  # type: ignore[union-attr]
    assert sorted(r[0] for r in results) == ["accepted", "duplicate"] and calls["n"] == 1
    assert client.portal.call(c.ingest.apply, b, datetime.now(UTC))[0] == "duplicate"  # type: ignore[union-attr]


# ------------------------------------------------------------------ audit
def test_audit_buffer_is_bounded_even_for_security_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_mod, "MAX_BUFFER", 10)
    monkeypatch.setattr(audit_mod, "HARD_MAX_BUFFER", 20)
    svc = audit_mod.AuditService(repo=None)
    for i in range(50):
        svc.record("acme", "u", "user", f"security.event_{i}", "security", severity="WARNING")
    assert len(svc._buffer) == 20 and svc.dropped == 30
    assert svc._buffer[-1].action == "security.event_49"  # newest kept, oldest dropped


# ------------------------------------------------------------------ readiness
def test_readiness_reports_background_loops_and_fails_on_a_crash_looping_critical_loop(
    client: TestClient,
) -> None:
    body = client.get("/health/ready").json()
    assert body["checks"]["background"]["status"] == "ok" and body["checks"]["background"]["tasks"] >= 8
    c = client.app.state.container  # type: ignore[attr-defined]
    st = c.supervisor.tasks["persister"]
    st.state = "crash_looping"
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["background"]["critical_crash_looping"] == [
        "persister"
    ]
    st.state = "running"


async def test_a_loop_started_during_shutdown_still_runs_its_exit_path() -> None:
    """Regression: start() immediately followed by stop() must still let loops flush their buffers."""
    stop = asyncio.Event()
    s = sup.Supervisor(stop)
    flushed = asyncio.Event()

    async def loop() -> None:
        await stop.wait()
        flushed.set()  # final flush on the way out

    t = s.spawn("flusher", loop)
    stop.set()  # before the task ever ran
    await asyncio.wait_for(t, 2)
    assert flushed.is_set() and s.tasks["flusher"].state == "stopped"


# ------------------------------------------------------------------ SLOs
def test_slo_endpoint_reports_proposed_targets_with_current_values(client: TestClient) -> None:
    h = {"X-Agent-Key": AGENT_KEY}
    assert client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=h).status_code == 202
    for i in range(3):
        client.post("/api/v1/ingest/telemetry", json=batch(typical_samples(), seq=i + 1), headers=h)
    body = client.get("/api/v1/ops/slo").json()
    assert body["window"] == "process_lifetime" and "not an achieved" in body["note"]
    by_id = {s["slo_id"]: s for s in body["slos"]}
    assert {s["target_status"] for s in body["slos"]} == {"PROPOSED"}
    # the metrics registry is process-wide (other tests shed load on purpose): check consistency, not 1.0
    ing = by_id["ingest-success"]
    assert ing["current"] is not None and 0 < ing["current"] <= 1.0
    assert ing["state"] == ("MEETING" if ing["current"] >= ing["target"] else "NOT_MEETING")
    assert by_id["background-health"]["state"] == "MEETING"
    assert by_id["tenant-isolation"]["state"] == "NO_DATA"  # never invented


def test_slo_endpoint_is_platform_scoped() -> None:
    from tests.unit.tenancy_helpers import accounts_app, build_world

    with accounts_app() as c:
        w = build_world(c)
        assert c.get("/api/v1/ops/slo", headers=w.root).status_code == 200
        assert c.get("/api/v1/ops/slo", headers=w.alice).status_code == 403  # an organization owner


# ------------------------------------------------------------------ leader lock semantics (no database)
class _Conn:
    def __init__(self, mode: str) -> None:
        self.mode = mode  # ok | slow (answers late) | hang (never answers in time) | dead

    async def execute(self, *_a: object, **_k: object) -> None:
        if self.mode == "slow":
            await asyncio.sleep(0.3)
        if self.mode == "hang":
            await asyncio.sleep(10)
        if self.mode == "dead":
            raise ConnectionResetError("connection closed")

    async def close(self) -> None:
        return None


def _lock(mode: str, max_unverified_s: float) -> Any:
    from app.infrastructure.database.leader import LeaderLock

    lock = LeaderLock(
        "postgresql+asyncpg://u:p@127.0.0.1:1/x", check_s=0.05, max_unverified_s=max_unverified_s
    )
    lock._conn, lock.is_leader = _Conn(mode), True  # type: ignore[assignment]
    return lock


async def test_a_slow_database_does_not_make_the_leader_step_down() -> None:
    lock = _lock("slow", max_unverified_s=5.0)
    stop, lost = asyncio.Event(), asyncio.Event()
    t = asyncio.create_task(lock.hold(stop, lost.set))
    await asyncio.sleep(1.0)  # probes answer late (0.3 s > check 0.05 s): unverified, never cancelled
    assert not lost.is_set() and lock.is_leader
    lock._conn.mode = "ok"  # database fast again
    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(t, 2)
    assert not lost.is_set()


async def test_a_dead_session_steps_down_at_once_and_a_long_outage_eventually() -> None:
    dead = _lock("dead", max_unverified_s=60.0)
    lost = asyncio.Event()
    await asyncio.wait_for(dead.hold(asyncio.Event(), lost.set), 2)
    assert lost.is_set() and not dead.is_leader
    slow = _lock("hang", max_unverified_s=0.2)
    lost2 = asyncio.Event()
    await asyncio.wait_for(slow.hold(asyncio.Event(), lost2.set), 3)
    assert lost2.is_set()  # bounded: a one-sided partition cannot keep two leaders for long


async def test_out_of_order_samples_are_persisted_not_downsampled_away() -> None:
    """Regression (Phase 10 chaos drill): a sample older than the newest kept one was skipped by the
    downsampler (negative gap < interval), silently losing replayed history."""
    from app.domain.telemetry.models import MetricReading, Quality
    from app.repositories.memory import MemoryTelemetryRepository
    from app.services.persistence import SamplePersister
    from tests.conftest import settings

    p = SamplePersister(MemoryTelemetryRepository(), settings(PERSIST_SAMPLE_INTERVAL_S=5))
    t0 = datetime.now(UTC)

    def r(dt: float) -> MetricReading:
        return MetricReading(
            "cpu.usage_percent",
            "cpu.usage_percent",
            "cpu",
            1.0,
            "percent",
            t0 + timedelta(seconds=dt),
            "s",
            Quality.GOOD,
            True,
            "measured",
        )

    assert p.enqueue("d", [r(100)]) == 1  # live
    assert p.enqueue("d", [r(102)]) == 0  # live, within the 5 s interval: downsampled
    assert p.enqueue("d", [r(10), r(20)]) == 2  # replayed older history: kept
    assert p.enqueue("d", [r(106)]) == 1  # live downsampling unaffected by the replay


# ------------------------------------------------------------------ durable confirmation (crash-safe ingest)
class _Receipts:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def write_receipts(self, receipts: list[Any]) -> int:
        self.rows += receipts
        return len(receipts)

    async def recent_receipt_ids(self, since: datetime, limit: int) -> list[str]:
        return []

    async def existing_receipt_ids(self, device_id: str, batch_ids: list[str]) -> list[str]:
        return [r.batch_id for r in self.rows if r.device_id == device_id and r.batch_id in batch_ids]

    async def purge_receipts_older_than(self, cutoff: datetime) -> int:
        return 0


def _durable_client(**kw: Any) -> TestClient:
    from app.main import create_app
    from tests.conftest import settings

    app = create_app(settings(**kw))
    app.state.container.ingest.receipts._repo = _Receipts()  # type: ignore[attr-defined]
    return TestClient(app)


def test_receipts_and_confirmation_only_after_samples_are_written() -> None:
    h = {"X-Agent-Key": AGENT_KEY}
    with _durable_client() as c:
        ct = c.app.state.container  # type: ignore[attr-defined]
        assert c.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=h).status_code == 202
        body = batch(typical_samples(), seq=1)
        body["batch_id"] = "dur-1"
        r = c.post("/api/v1/ingest/telemetry/bulk", json={"batches": [body]}, headers=h)
        assert r.status_code == 200 and r.json()["durable_confirmation"] is True
        hb = {"device_id": body["device_id"], "agent_version": "1.8.0", "sent_at": datetime.now(UTC).isoformat(),
              "unconfirmed_batch_ids": ["dur-1", "never-sent"]}  # fmt: skip
        out = c.post("/api/v1/agent/heartbeat", json=hb, headers=h).json()
        if out["durable_batch_ids"] == []:  # not flushed yet: pending, not durable
            assert out["pending_batch_ids"] == ["dur-1"]
        c.portal.call(ct.persister.flush)  # type: ignore[union-attr]
        out = c.post("/api/v1/agent/heartbeat", json=hb, headers=h).json()
        assert out["durable_batch_ids"] == ["dur-1"] and out["unknown_batch_ids"] == ["never-sent"]


def test_a_batch_whose_rows_were_evicted_is_never_confirmed_and_its_resend_is_applied() -> None:
    h = {"X-Agent-Key": AGENT_KEY}
    with _durable_client(PERSIST_QUEUE_MAX=1000, PERSIST_SAMPLE_INTERVAL_S=1) as c:
        ct = c.app.state.container  # type: ignore[attr-defined]
        assert c.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=h).status_code == 202
        repo = ct.telemetry_repo
        healthy_write = repo.write_samples

        async def db_down(*_a: Any, **_k: Any) -> int:
            raise ConnectionError("database down")

        repo.write_samples = db_down  # writes fail: rows stay queued, the bounded queue overflows
        first = batch(typical_samples(), seq=1)
        first["batch_id"] = "evicted-1"
        assert (
            c.post("/api/v1/ingest/telemetry/bulk", json={"batches": [first]}, headers=h).status_code == 200
        )
        # overflow the write queue (database "down"): the first batch's rows are evicted unwritten
        from tests.conftest import sample

        t0 = datetime.now(UTC) - timedelta(minutes=5)  # past timestamps (future ones are clamped to "now")
        for i in range(2, 30):
            b = batch(
                [
                    sample(
                        "cpu.core_usage_percent",
                        float(i),
                        labels={"core": str(k)},
                        ts=t0 + timedelta(seconds=2 * i),
                    )
                    for k in range(60)
                ],
                seq=i,
            )
            b["batch_id"] = f"filler-{i}"
            c.post("/api/v1/ingest/telemetry/bulk", json={"batches": [b]}, headers=h)
        assert ct.persister.max_dropped_seq > 0
        repo.write_samples = healthy_write  # database back
        c.portal.call(ct.persister.flush)  # type: ignore[union-attr]
        hb = {"device_id": first["device_id"], "agent_version": "1.8.0", "sent_at": datetime.now(UTC).isoformat(),
              "unconfirmed_batch_ids": ["evicted-1", "filler-29"]}  # fmt: skip
        out = c.post("/api/v1/agent/heartbeat", json=hb, headers=h).json()
        assert out["unknown_batch_ids"] == ["evicted-1"] and out["durable_batch_ids"] == ["filler-29"]
        again = c.post("/api/v1/ingest/telemetry/bulk", json={"batches": [first]}, headers=h).json()
        assert again["results"][0]["status"] == "accepted"  # not swallowed as a duplicate
        assert ct.ingest.lost_batches >= 1
