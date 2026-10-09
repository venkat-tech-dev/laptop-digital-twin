"""Integration tests against real PostgreSQL/TimescaleDB and Redis.

Run with the infrastructure up:  docker compose up -d postgres redis && alembic upgrade head
Uses DATABASE_URL / REDIS_URL from .env; skipped automatically if unreachable.
Each test uses a unique device id and deletes it afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.domain.anomalies.models import Anomaly, Detector, Severity
from app.domain.devices.models import Device
from app.infrastructure.database.engine import Database
from app.infrastructure.redis.client import RedisGateway
from app.repositories.base import HealthEventRecord, MetricDef, SampleRow, SystemEventRecord
from app.repositories.sql import SqlDeviceRepository, SqlEventRepository, SqlTelemetryRepository

pytestmark = pytest.mark.integration

ENV = Settings()


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    if not ENV.database_url:
        pytest.skip("DATABASE_URL not configured")
    database = Database(ENV.database_url)
    try:
        await database.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        await database.dispose()
        pytest.skip(f"PostgreSQL unreachable: {exc}")
    yield database
    await database.dispose()


@pytest.fixture
async def device(db: Database) -> AsyncIterator[str]:
    device_id = f"itest-{uuid.uuid4().hex[:10]}"
    now = datetime.now(UTC)
    await SqlDeviceRepository(db).upsert(
        Device(device_id, "LENOVO", "Test", "X", "Windows", {"k": 1}, "t", now, now, now)
    )
    yield device_id
    from sqlalchemy import text

    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM devices WHERE id = :d"), {"d": device_id})


async def test_samples_round_trip_and_bucketed_history(db: Database, device: str) -> None:
    repo = SqlTelemetryRepository(db)
    key = "cpu.usage_percent"
    defs = {key: MetricDef(key, key, "cpu", "percent", "test", "measured", {})}
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    rows = [SampleRow(key, t0 + timedelta(seconds=10 * i), float(i), 0) for i in range(30)]
    assert await repo.write_samples(device, defs, rows) == 30
    assert await repo.write_samples(device, defs, rows[:5]) == 5  # duplicate timestamps ignored, no error
    hist = await repo.history(device, [key], t0 - timedelta(seconds=1), t0 + timedelta(minutes=10), 60)
    points = hist[key]
    assert sum(p.count for p in points) == 30
    assert points[0].min == 0.0 and max(p.max for p in points) == 29.0
    raw = await repo.raw_values(device, key, t0, t0 + timedelta(minutes=10))
    assert len(raw) == 30 and raw[0][1] == 0.0
    assert [m.key for m in await repo.list_metrics(device)] == [key]


async def test_anomaly_health_and_system_events(db: Database, device: str) -> None:
    repo = SqlEventRepository(db)
    now = datetime.now(UTC)
    a = Anomaly(
        str(uuid.uuid4()),
        device,
        Detector.RULE,
        "r",
        "cpu",
        "cpu.usage_percent",
        Severity.WARNING,
        "T",
        "m",
        95.0,
        90.0,
        now,
        now,
        None,
        {"duration_s": 10},
    )
    await repo.upsert_anomaly(a)
    assert [x.anomaly_id for x in await repo.list_anomalies(device, "active", None, None, 10)] == [
        a.anomaly_id
    ]
    a.resolved_at = now + timedelta(seconds=30)
    await repo.upsert_anomaly(a)
    assert await repo.list_anomalies(device, "active", None, None, 10) == []
    resolved = await repo.list_anomalies(device, "resolved", "warning", None, 10)
    assert resolved[0].value == 95.0 and resolved[0].resolved_at is not None
    await repo.add_health_event(
        HealthEventRecord(device, "cpu", now, 100, 80, "healthy", "warning", [{"m": 1}])
    )
    assert (await repo.list_health_events(device, 5))[0].score == 80
    await repo.add_system_event(SystemEventRecord(device, now, "DeviceOnline", "info", "online", {}))
    assert (await repo.list_system_events(device, 5))[0].event_type == "DeviceOnline"


async def test_timescale_hypertable_present(db: Database) -> None:
    assert await db.detect_timescale() is True


async def test_redis_hot_state_and_recent_stream() -> None:
    if not ENV.redis_url:
        pytest.skip("REDIS_URL not configured")
    gw = RedisGateway(ENV.redis_url)
    try:
        await gw.ping()
    except Exception as exc:  # pragma: no cover
        await gw.close()
        pytest.skip(f"Redis unreachable: {exc}")
    dev = f"itest-{uuid.uuid4().hex[:8]}"
    await gw.save_twin(dev, {"device_id": dev}, ttl_s=30)
    assert (await gw.load_twin(dev)) == {"device_id": dev}
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    await gw.append_recent(dev, now_ms, {"cpu.usage_percent": 12.5})
    assert (await gw.read_recent(dev, now_ms - 1000))[0][1] == {"cpu.usage_percent": 12.5}
    await gw.close()


async def test_redis_fan_out_skips_own_events_and_reaches_other_replicas() -> None:
    import asyncio

    if not ENV.redis_url:
        pytest.skip("REDIS_URL not configured")
    url = ENV.redis_url.rsplit("/", 1)[0] + "/3"  # isolated logical db -> isolated channel
    a, b = RedisGateway(url), RedisGateway(url)
    try:
        await a.ping()
    except Exception as exc:  # pragma: no cover
        await a.close()
        await b.close()
        pytest.skip(f"Redis unreachable: {exc}")
    assert a.channel.endswith(":db3") and a.channel == b.channel
    got_a: list[dict[str, object]] = []
    got_b: list[dict[str, object]] = []

    async def on_a(m: dict[str, object]) -> None:
        got_a.append(m)

    async def on_b(m: dict[str, object]) -> None:
        got_b.append(m)

    a.start_listener(on_a)
    b.start_listener(on_b)
    await asyncio.sleep(0.5)  # subscriptions established
    await a.publish_event({"event": "x", "device_id": "d"})
    for _ in range(40):
        if got_b:
            break
        await asyncio.sleep(0.05)
    assert got_b == [{"event": "x", "device_id": "d"}]  # the other replica receives it
    assert got_a == []  # the publisher already delivered locally and skips its own echo
    await a.close()
    await b.close()
