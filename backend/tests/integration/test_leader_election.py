"""Phase 10: active/standby election against real PostgreSQL.

Needs a throwaway database (never the live one, whose backend holds the lock):
    set TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host:15432/ldt_test
    pytest tests/integration -m integration
Skipped when TEST_DATABASE_URL is unset or unreachable.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.infrastructure.database.engine import normalize_url
from app.infrastructure.database.leader import LEADER_LOCK_KEY, LeaderLock

pytestmark = pytest.mark.integration

URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
async def url() -> str:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not configured (throwaway database required)")
    u = normalize_url(URL)
    eng = create_async_engine(u)
    try:
        async with eng.connect() as c:
            await c.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"test database unreachable: {exc}")
    finally:
        await eng.dispose()
    return u


async def test_only_one_instance_leads_and_the_standby_takes_over(url: str) -> None:
    a, b = LeaderLock(url, check_s=0.2, retry_s=0.2), LeaderLock(url, check_s=0.2, retry_s=0.2)
    try:
        assert await a.try_acquire() is True
        assert await b.try_acquire() is False  # split brain prevented
        stop = asyncio.Event()
        waiter = asyncio.create_task(b.wait_until_leader(stop))
        await asyncio.sleep(0.5)
        assert not waiter.done()  # standby keeps waiting
        await a.release()  # active instance stops
        assert await asyncio.wait_for(waiter, 5) is True and b.is_leader
    finally:
        await a.release()
        await b.release()


async def test_leader_detects_a_lost_session_and_steps_down(url: str) -> None:
    a = LeaderLock(url, check_s=0.2)
    lost = asyncio.Event()
    try:
        assert await a.try_acquire()
        stop = asyncio.Event()
        holder = asyncio.create_task(a.hold(stop, lost.set))
        # simulate a network partition / DB restart: the server ends the leader's session
        eng = create_async_engine(url)
        async with eng.begin() as c:
            killed = (
                await c.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_locks "
                        "WHERE locktype = 'advisory' AND objid = :k AND granted"
                    ),
                    {"k": LEADER_LOCK_KEY & 0xFFFFFFFF},
                )
            ).all()
        await eng.dispose()
        assert killed, "leader session not found"
        await asyncio.wait_for(lost.wait(), 5)  # the leader notices and steps down
        await asyncio.wait_for(holder, 5)
        assert a.is_leader is False
        b = LeaderLock(url)
        try:
            assert await b.try_acquire()  # the lock is free again for a standby
        finally:
            await b.release()
    finally:
        await a.release()


async def test_the_lock_session_never_holds_a_transaction_open(url: str) -> None:
    """An 'idle in transaction' session held for the life of the process would block VACUUM cleanup
    for the whole database. The lock session must stay plain 'idle' between probes."""
    a = LeaderLock(url, check_s=0.1)
    try:
        assert await a.try_acquire()
        stop = asyncio.Event()
        holder = asyncio.create_task(a.hold(stop, lambda: None))
        await asyncio.sleep(0.5)  # several probes
        eng = create_async_engine(url)
        async with eng.connect() as c:
            states = (
                (
                    await c.execute(
                        text(
                            "SELECT a.state FROM pg_stat_activity a JOIN pg_locks l ON l.pid = a.pid "
                            "WHERE l.locktype = 'advisory' AND l.objid = :k AND l.granted"
                        ),
                        {"k": LEADER_LOCK_KEY & 0xFFFFFFFF},
                    )
                )
                .scalars()
                .all()
            )
        await eng.dispose()
        assert states and all(s in ("idle", "active") for s in states), states
        stop.set()
        await asyncio.wait_for(holder, 2)
    finally:
        await a.release()
