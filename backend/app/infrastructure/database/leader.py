"""Active/standby election with a PostgreSQL session advisory lock (Phase 10).

The backend keeps control state in process memory (tenancy, policies, alert engine, remediation
dispatch, quotas). Two active replicas against one database would diverge: a device revoked on one
replica keeps ingesting on the other, and every background loop runs twice. So exactly one instance is
**active**. Others wait as **standby** (API and readiness answer 503) and take over when the active
instance's database session ends (process exit, crash, network partition).

The lock lives on a dedicated connection (not from the request pool). PostgreSQL releases it when that
session ends, so a crashed leader cannot hold it. The leader probes its lock connection every
``check_s`` seconds. If the connection is gone, it has lost leadership (another instance may already be
active) and must stop acting: the caller shuts the process down and the supervisor (Docker
``restart: unless-stopped``) starts it again as a candidate.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

log = structlog.get_logger("leader")

#: "LDTL" — distinct from the audit (0x4C445441) and remediation (0x4C445452) transaction locks
LEADER_LOCK_KEY = 0x4C44544C


class LeaderLock:
    def __init__(
        self, url: str, check_s: float = 5.0, retry_s: float = 5.0, max_unverified_s: float = 60.0
    ) -> None:
        # AUTOCOMMIT: the lock session must never sit "idle in transaction" (that would hold back VACUUM
        # for the whole database for the life of the process); session advisory locks need no transaction
        self._engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        self._conn: AsyncConnection | None = None
        self.check_s = check_s
        self.retry_s = retry_s
        self.max_unverified_s = max_unverified_s
        self.is_leader = False

    async def try_acquire(self) -> bool:
        conn = await self._engine.connect()
        try:
            got = bool(
                (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LEADER_LOCK_KEY})).scalar()
            )
        except Exception:
            await conn.close()
            raise
        if not got:
            await conn.close()
            return False
        self._conn, self.is_leader = conn, True
        return True

    async def wait_until_leader(self, stop: asyncio.Event) -> bool:
        """Block until this instance holds the lock (True) or ``stop`` is set (False)."""
        announced = False
        while not stop.is_set():
            try:
                if await self.try_acquire():
                    log.info("leadership_acquired")
                    return True
                if not announced:
                    log.warning("standby_another_instance_is_active", retry_s=self.retry_s)
                    announced = True
            except Exception as exc:  # database unreachable: keep trying, bounded pace
                log.warning("leadership_check_failed", error=str(exc)[:200])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.retry_s)
        return False

    async def hold(self, stop: asyncio.Event, on_lost: Callable[[], Awaitable[None] | None]) -> None:
        """While leader: verify the lock session; on loss call ``on_lost`` once and return.

        A *closed* session means PostgreSQL released the lock (another instance may take it): step down at
        once. A probe that only *times out* (database paused or overloaded) does not release the lock, and
        no other instance can acquire it meanwhile, so keep running and keep probing; step down only if the
        session stays unverifiable for ``max_unverified_s`` (bounds a split brain under a one-sided
        network partition).
        """
        unverified_since: float | None = None
        probe: asyncio.Task[Any] | None = None
        loop = asyncio.get_running_loop()
        reason = ""
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.check_s)
            if stop.is_set():
                break
            if probe is None:
                assert self._conn is not None
                # never cancel a probe: cancelling a query mid-flight leaves the connection unusable, which
                # would look like a lost session; a slow probe stays pending and is awaited again
                probe = asyncio.create_task(self._conn.execute(text("SELECT 1")))
            await asyncio.wait({probe}, timeout=self.check_s)
            if probe.done():
                exc = probe.exception()
                probe = None
                if exc is None:
                    if unverified_since is not None:
                        log.info(
                            "leadership_verified_again", after_s=round(loop.time() - unverified_since, 1)
                        )
                    unverified_since = None
                    continue
                reason = str(exc)[:200]
            else:
                if unverified_since is None:
                    unverified_since = loop.time()
                    log.warning("leadership_unverified_database_slow", max_s=self.max_unverified_s)
                if loop.time() - unverified_since < self.max_unverified_s:
                    continue
                reason = f"lock session unverifiable for {self.max_unverified_s:.0f} s"
                probe.cancel()
                probe = None
            self.is_leader = False
            log.critical("leadership_lost_stepping_down", reason=reason)
            with contextlib.suppress(Exception):
                await self.release()
            result = on_lost()
            if asyncio.iscoroutine(result):
                await result
            return
        if probe is not None:  # normal shutdown while a probe is pending
            probe.cancel()

    async def release(self) -> None:
        conn, self._conn = self._conn, None
        self.is_leader = False
        if conn is not None:
            # bounded: on step-down the database may be unresponsive; closing the session releases the lock
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LEADER_LOCK_KEY}), self.check_s
                )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(conn.close(), self.check_s)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._engine.dispose(), self.check_s)
