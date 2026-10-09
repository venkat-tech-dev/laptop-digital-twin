from __future__ import annotations

import asyncio
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def normalize_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


class Database:
    def __init__(
        self,
        url: str,
        pool_size: int = 5,
        max_overflow: int = 5,
        pool_timeout_s: float = 10.0,
        statement_timeout_ms: int = 60_000,
    ) -> None:
        url = normalize_url(url)
        connect_args: dict[str, object] = {}
        if statement_timeout_ms > 0 and url.startswith("postgresql+asyncpg"):
            # a runaway query cannot hold a pool connection forever (0 disables)
            connect_args["server_settings"] = {"statement_timeout": str(statement_timeout_ms)}
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout_s,  # waiting for a free connection fails fast instead of piling up
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args=connect_args,
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.timescale = False
        self._ping_task: asyncio.Future[float] | None = None
        self.aggregate_5m = False  # telemetry_samples_5m continuous aggregate present (migration 0005)

    async def ping_bounded(self, timeout_s: float = 2.0) -> float:
        """Readiness ping that answers within ``timeout_s`` even when the database hangs (paused, network
        stall). The ping is never cancelled (that would poison its pooled connection); while one is still
        pending, later calls wait on the same one instead of piling up more."""
        task = self._ping_task
        if task is None or task.done():
            task = self._ping_task = asyncio.ensure_future(self.ping())
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
        if not done:
            raise TimeoutError(f"database did not answer within {timeout_s} s")
        return task.result()

    async def ping(self) -> float:
        started = time.perf_counter()
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return (time.perf_counter() - started) * 1000.0

    async def detect_timescale(self) -> bool:
        async with self.engine.connect() as conn:
            row = await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"))
            self.timescale = row.first() is not None
            if self.timescale:
                view = await conn.execute(
                    text(
                        "SELECT 1 FROM timescaledb_information.continuous_aggregates "
                        "WHERE view_name = 'telemetry_samples_5m'"
                    )
                )
                self.aggregate_5m = view.first() is not None
        return self.timescale

    async def apply_retention(self, raw_days: int, aggregate_days: int) -> list[str]:
        """(Re)apply TimescaleDB retention policies from settings (idempotent). Returns what was set."""
        applied: list[str] = []
        if not self.timescale:
            return applied
        targets = [("telemetry_samples", int(raw_days))]
        if self.aggregate_5m:
            targets.append(("telemetry_samples_5m", int(aggregate_days)))
        async with self.engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            for table, days in targets:
                await conn.execute(text(f"SELECT remove_retention_policy('{table}', if_exists => true)"))
                await conn.execute(text(f"SELECT add_retention_policy('{table}', INTERVAL '{days} days')"))
                applied.append(f"{table}={days}d")
        return applied

    async def dispose(self) -> None:
        await self.engine.dispose()
