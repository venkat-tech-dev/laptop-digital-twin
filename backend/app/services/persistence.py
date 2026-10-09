"""Batched, bounded persistence of telemetry samples and twin events.

* Samples are down-sampled per series (at most one row per ``PERSIST_SAMPLE_INTERVAL_S``), buffered
  in a bounded queue and written in batches. If the database is down the queue keeps the most recent
  rows (oldest dropped and counted) and retries with backoff - ingest never blocks on the database.
* Event records (anomalies, health changes, system events) go through a separate bounded queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta

import structlog

from app.core.config import Settings
from app.core.metrics import PERSIST_DROPPED, PERSIST_LAG, PERSIST_QUEUE, RECORDER_DROPPED, RECORDER_QUEUE
from app.domain.telemetry.models import MetricReading, Quality
from app.repositories.base import MetricDef, SampleRow, TelemetryRepository

log = structlog.get_logger("persistence")


class SamplePersister:
    def __init__(self, repo: TelemetryRepository, settings: Settings) -> None:
        self._repo = repo
        self._interval = settings.persist_sample_interval_s
        self._flush_s = settings.persist_flush_interval_s
        self._exclude = tuple(settings.persist_exclude_prefixes)
        # (sequence, device, row): the sequence tells flush() exactly which rows it wrote, even when the
        # bounded queue dropped its oldest rows while the write was in progress
        self._queue: deque[tuple[int, str, SampleRow, float]] = deque(maxlen=settings.persist_queue_max)
        self._seq = 0
        self.on_flushed: Callable[[int], None] | None = None  # Phase 10: durable-confirmation hook
        self.max_dropped_seq = 0  # highest row sequence evicted unwritten (queue overflow)
        self._defs: dict[str, dict[str, MetricDef]] = {}
        self._last: dict[tuple[str, str], datetime] = {}
        self._backoff = 0.0
        self.written = 0
        self.last_error: str | None = None
        self.last_write_at: datetime | None = None

    @property
    def queued_seq(self) -> int:
        """Sequence number of the last row queued (rows up to it are written once flushed past it)."""
        return self._seq

    @property
    def depth(self) -> int:
        return len(self._queue)

    @property
    def capacity(self) -> int:
        return self._queue.maxlen or 1

    def oldest_age_s(self) -> float | None:
        """Age of the oldest queued sample (persistence lag); None when nothing waits."""
        if not self._queue:
            return None
        return round(time.monotonic() - self._queue[0][3], 1)  # waiting time here, not sample age

    def enqueue(self, device_id: str, readings: Iterable[MetricReading]) -> int:
        added = 0
        defs = self._defs.setdefault(device_id, {})
        for r in readings:
            if not r.available or r.quality not in (Quality.GOOD, Quality.DEGRADED):
                continue
            if r.metric.startswith(self._exclude):
                continue
            if isinstance(r.value, bool):
                value = 1.0 if r.value else 0.0
            elif isinstance(r.value, (int, float)):
                value = float(r.value)
            else:
                continue
            last = self._last.get((device_id, r.key))
            if last is not None and r.timestamp < last:
                pass  # older than the newest kept sample (replay / out of order): history, always keep it
            elif last is not None and (r.timestamp - last).total_seconds() < self._interval:
                continue  # downsample live data to one sample per series per interval
            else:
                self._last[(device_id, r.key)] = r.timestamp
            if r.key not in defs:
                defs[r.key] = MetricDef(r.key, r.metric, r.component_id, r.unit, r.source, r.kind, r.labels)
            if len(self._queue) == self._queue.maxlen:
                PERSIST_DROPPED.inc()
                self.max_dropped_seq = self._queue[0][0]  # evicted (oldest first): never confirm its batch
            self._seq += 1
            row = SampleRow(r.key, r.timestamp, value, 0 if r.quality is Quality.GOOD else 1)
            self._queue.append((self._seq, device_id, row, time.monotonic()))
            added += 1
        PERSIST_QUEUE.set(len(self._queue))
        return added

    async def flush(self) -> int:
        if not self._queue:
            return 0
        batch = list(self._queue)
        last_seq = batch[-1][0]
        by_device: dict[str, list[SampleRow]] = {}
        for _, device_id, row, _queued_at in batch:
            by_device.setdefault(device_id, []).append(row)
        written = 0
        try:
            bulk = getattr(self._repo, "write_samples_bulk", None)
            if bulk is not None:
                written = await bulk([(d, self._defs.get(d, {}), rows) for d, rows in by_device.items()])
            else:
                for device_id, rows in by_device.items():
                    written += await self._repo.write_samples(device_id, self._defs.get(device_id, {}), rows)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("persist_failed", error=self.last_error, queued=len(self._queue))
            raise
        # remove only what was written: rows evicted during the await are already gone, rows added after
        # the snapshot (sequence > last_seq) stay queued for the next flush
        while self._queue and self._queue[0][0] <= last_seq:
            self._queue.popleft()
        if self.on_flushed is not None:
            self.on_flushed(last_seq)  # rows up to last_seq are durable
        PERSIST_QUEUE.set(len(self._queue))
        PERSIST_LAG.set(self.oldest_age_s() or 0.0)
        self.written += written
        self.last_error = None
        self.last_write_at = datetime.now(UTC)
        return written

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            delay = self._flush_s
            try:
                await self.flush()
                self._backoff = 0.0
            except Exception:
                self._backoff = min(60.0, max(2.0, self._backoff * 2))
                delay = self._backoff
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
        with contextlib.suppress(Exception):
            await self.flush()  # best effort on shutdown


class RetentionTask:
    """Deletes samples older than RETENTION_DAYS (TimescaleDB uses its own retention policy instead)."""

    def __init__(self, repo: TelemetryRepository, retention_days: int, enabled: bool) -> None:
        self._repo = repo
        self._days = retention_days
        self._enabled = enabled

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set() and self._enabled:
            try:
                removed = await self._repo.purge_older_than(datetime.now(UTC) - timedelta(days=self._days))
                if removed:
                    log.info("retention_purged", rows=removed)
            except Exception as exc:
                log.warning("retention_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=3600)


Job = Callable[[], Awaitable[None]]


#: waits between attempts of one event-record job (3 attempts; the last value is never used)
RECORD_RETRY_DELAYS_S = (0.5, 2.0, 0.0)


class EventRecorder:
    """Sequential background writer for event records (keeps DB latency out of the ingest path)."""

    def __init__(self, max_queue: int = 5000) -> None:
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=max_queue)
        self.dropped = 0
        self.failures = 0

    def submit(self, job: Job) -> None:
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self.dropped += 1
            RECORDER_DROPPED.labels("queue_full").inc()
        RECORDER_QUEUE.set(self._queue.qsize())

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            getter = asyncio.ensure_future(self._queue.get())
            stopper = asyncio.ensure_future(stop.wait())
            done, _ = await asyncio.wait({getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            stopper.cancel()
            if getter not in done:  # shutdown: stop waiting at once (was up to 1 s per stop)
                getter.cancel()
                break
            job = getter.result()
            RECORDER_QUEUE.set(self._queue.qsize())
            # event records are idempotent (unique event ids): retry a few times, then drop and count
            for attempt, delay in enumerate(RECORD_RETRY_DELAYS_S, start=1):
                try:
                    await job()
                    break
                except Exception as exc:
                    if attempt == len(RECORD_RETRY_DELAYS_S) or stop.is_set():
                        self.failures += 1
                        RECORDER_DROPPED.labels("failed").inc()
                        log.warning("event_record_failed", error=str(exc)[:300], attempts=attempt)
                        break
                    await asyncio.sleep(delay)
