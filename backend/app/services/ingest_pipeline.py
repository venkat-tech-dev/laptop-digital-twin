"""The one authoritative telemetry ingestion path.

    authenticate (router) -> rate limit -> validate (schema version, schema, device identity,
    timestamps) -> dedupe -> apply (twin projection, persistence queue, events) -> track sequence /
    presence / latency -> receipt (async) -> acknowledge

Only cheap, bounded work happens in the request: the twin update is in-memory, database writes go
through bounded background queues (``SamplePersister``, ``EventRecorder``, ``ReceiptWriter``) and no
analytics beyond the incremental rule/statistical anomaly evaluation run here.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog
from pydantic import ValidationError

from app.core.metrics import (
    INGEST_BATCHES,
    INGEST_DUPLICATES,
    INGEST_INFLIGHT,
    INGEST_REJECTED,
    PIPELINE_LATENCY,
    RECEIPTS_DROPPED,
    RECEIPTS_PENDING,
    SEQUENCE_OBSERVED,
)
from app.schemas.ingest import (
    SUPPORTED_SCHEMA_VERSIONS,
    BatchResultOut,
    BulkAck,
    TelemetryBatchIn,
)
from app.services.devices import BatchDeduper
from app.services.presence import PresenceService
from app.services.sequences import SequenceTracker

log = structlog.get_logger("ingest")

STAGES = (
    "collection_to_server_ms",
    "server_processing_ms",
    "twin_projection_ms",
    "ws_queue_ms",
    "websocket_delivery_ms",
    "end_to_end_latency_ms",
)


class LatencyStats:
    """Rolling per-stage latency reservoir (last N observations) + Prometheus histograms."""

    def __init__(self, size: int = 4096) -> None:
        self._values: dict[str, deque[float]] = {s: deque(maxlen=size) for s in STAGES}

    def observe(self, stage: str, ms: float) -> None:
        if ms < 0 or ms != ms:  # negative (clock skew) or NaN: do not pollute the distribution
            return
        self._values.setdefault(stage, deque(maxlen=4096)).append(ms)
        PIPELINE_LATENCY.labels(stage.removesuffix("_ms")).observe(ms)

    def summary(self) -> dict[str, dict[str, float | int | None]]:
        out: dict[str, dict[str, float | int | None]] = {}
        for stage, values in self._values.items():
            v = sorted(values)
            n = len(v)
            out[stage] = {
                "count": n,
                "p50": round(v[n // 2], 2) if n else None,
                "p95": round(v[min(n - 1, int(n * 0.95))], 2) if n else None,
                "p99": round(v[min(n - 1, int(n * 0.99))], 2) if n else None,
                "max": round(v[-1], 2) if n else None,
            }
        return out


@dataclass(frozen=True, slots=True)
class Receipt:
    batch_id: str
    device_id: str
    sequence: int
    schema_version: str
    collected_at: datetime | None
    received_at: datetime
    samples: int
    events: int
    replay: bool


class ReceiptRepository(Protocol):
    async def write_receipts(self, receipts: list[Receipt]) -> int: ...

    async def recent_receipt_ids(self, since: datetime, limit: int) -> list[str]: ...

    async def existing_receipt_ids(self, device_id: str, batch_ids: list[str]) -> list[str]: ...

    async def purge_receipts_older_than(self, cutoff: datetime) -> int: ...


class ReceiptWriter:
    """Buffers receipts and writes them in batches; warms the deduper from them after a restart so a
    batch that was accepted before the restart is still acknowledged as a duplicate."""

    def __init__(
        self, repo: ReceiptRepository | None, retention_hours: int, max_buffer: int = 50_000
    ) -> None:
        self._repo = repo
        self._retention = timedelta(hours=retention_hours)
        self._buffer: deque[Receipt] = deque(maxlen=max_buffer)
        self.written = 0
        self.dropped = 0
        self.last_error: str | None = None

    @property
    def pending(self) -> int:
        return len(self._buffer)

    @property
    def enabled(self) -> bool:
        return self._repo is not None

    async def durable(self, device_id: str, batch_ids: list[str]) -> set[str]:
        """Batch ids whose receipt exists (written or about to be): their samples are durable."""
        wanted = set(batch_ids)
        found = {r.batch_id for r in self._buffer if r.batch_id in wanted and r.device_id == device_id}
        rest = [b for b in batch_ids if b not in found]
        if rest and self._repo is not None:
            found |= set(await self._repo.existing_receipt_ids(device_id, rest))
        return found

    def add(self, receipt: Receipt) -> None:
        if self._repo is None:
            return
        if len(self._buffer) == self._buffer.maxlen:
            self.dropped += 1
            RECEIPTS_DROPPED.inc()
        self._buffer.append(receipt)
        RECEIPTS_PENDING.set(len(self._buffer))

    async def warm(self, deduper: BatchDeduper, limit: int = 200_000) -> int:
        if self._repo is None:
            return 0
        ids = await self._repo.recent_receipt_ids(datetime.now(UTC) - self._retention, limit)
        for batch_id in reversed(ids):  # oldest first so the newest stay in the LRU
            deduper.remember(batch_id)
        return len(ids)

    async def flush(self) -> int:
        if self._repo is None or not self._buffer:
            return 0
        batch = list(self._buffer)
        written = await self._repo.write_receipts(batch)
        for _ in range(len(batch)):
            if self._buffer:
                self._buffer.popleft()
        RECEIPTS_PENDING.set(len(self._buffer))
        self.written += written
        return written

    async def run(self, stop: asyncio.Event, flush_s: float = 2.0) -> None:
        if self._repo is None:
            return
        backoff = flush_s
        last_purge = 0.0
        while not stop.is_set():
            delay = flush_s
            try:
                await self.flush()
                self.last_error = None
                backoff = flush_s
                if time.monotonic() - last_purge > 3600:
                    last_purge = time.monotonic()
                    await self._repo.purge_receipts_older_than(datetime.now(UTC) - self._retention)
            except Exception as exc:  # database down: keep buffering (bounded), retry with backoff
                self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                backoff = min(60.0, backoff * 2)
                delay = backoff
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
        with contextlib.suppress(Exception):
            await self.flush()


class OverloadedError(Exception):
    """The backend is at capacity; the agent should retry after ``retry_after_s``."""

    def __init__(self, reason: str, retry_after_s: float) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_s = retry_after_s


@dataclass
class IngestCounters:
    requests: int = 0
    overloaded: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    rate_limited: int = 0
    samples: int = 0
    events: int = 0


class IngestPipeline:
    def __init__(
        self,
        container: Any,
        sequences: SequenceTracker,
        presence: PresenceService,
        receipts: ReceiptWriter,
        latency: LatencyStats,
    ) -> None:
        self._c = container
        self.sequences = sequences
        self.presence = presence
        self.receipts = receipts
        self.latency = latency
        self.counters = IngestCounters()
        self._applying: set[str] = set()  # batch ids being applied right now (concurrent-duplicate guard)
        # Phase 10: a receipt is written only once the batch's samples are durable, so a backend crash can
        # never acknowledge (via receipts / dedupe) data that was lost; agents >= 1.8 resend what is unknown
        self._awaiting: deque[tuple[int, int, Receipt]] = deque()  # (first row seq, last row seq, receipt)
        self._awaiting_ids: set[str] = set()
        self.lost_batches = 0  # accepted batches whose rows were evicted before being written
        container.persister.on_flushed = self._on_flushed
        self.rejections: dict[str, int] = {}
        s = container.settings
        self._max_future = timedelta(seconds=s.ingest_max_future_skew_s)
        self._max_inflight = s.ingest_max_inflight
        self._shed_replay_above = s.ingest_shed_replay_above
        self.inflight = 0
        self.inflight_max_seen = 0

    # -------------------------------------------------------------- admission
    # Overload protection. Agents treat 503 as retryable and honour Retry-After: nothing is lost,
    # the data stays in the agent's outbox and arrives when capacity is available.

    def try_enter(self) -> OverloadedError | None:
        """Concurrency gate, called by ``IngestBodyMiddleware`` before the body is even read: more
        than INGEST_MAX_INFLIGHT telemetry requests in progress -> immediate 503 instead of an
        ever-growing server-side queue (bounded latency for the requests that are admitted)."""
        if self.inflight >= self._max_inflight:
            self.counters.overloaded += 1
            INGEST_REJECTED.labels("overloaded").inc()
            return OverloadedError("ingest_overloaded", 2.0 + random.uniform(0.0, 3.0))  # noqa: S311
        self.inflight += 1
        self.inflight_max_seen = max(self.inflight_max_seen, self.inflight)
        INGEST_INFLIGHT.set(self.inflight)
        return None

    def leave(self) -> None:
        self.inflight -= 1
        INGEST_INFLIGHT.set(self.inflight)

    def admit_replay(self, replay_only: bool) -> None:
        """Persistence queue above INGEST_SHED_REPLAY_ABOVE -> backlog (replay) uploads are deferred
        so the live view stays current while history catches up. Raises :class:`OverloadedError`."""
        persister = self._c.persister
        fill = persister.depth / max(1, persister.capacity)
        if replay_only and fill >= self._shed_replay_above:
            self.counters.overloaded += 1
            INGEST_REJECTED.labels("replay_deferred").inc()
            raise OverloadedError("replay_deferred_persistence_busy", 10.0 + random.uniform(0.0, 10.0))  # noqa: S311

    # ------------------------------------------------------------- validation
    def _reject(self, reason: str) -> None:
        self.counters.rejected += 1
        self.rejections[reason] = self.rejections.get(reason, 0) + 1
        INGEST_REJECTED.labels(reason).inc()
        INGEST_BATCHES.labels("rejected").inc()

    def parse(self, item: Any, received_at: datetime) -> tuple[TelemetryBatchIn | None, str | None]:
        """Validate one raw batch. Returns (batch, None) or (None, "<reason>: <detail>")."""
        if not isinstance(item, dict):
            return None, "malformed_payload: batch is not a JSON object"
        version = item.get("schema_version", "1.0")
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            supported = ",".join(sorted(SUPPORTED_SCHEMA_VERSIONS))
            return None, f"unsupported_schema_version: {str(version)[:16]} (supported: {supported})"
        try:
            batch = TelemetryBatchIn.model_validate(item)
        except ValidationError as exc:
            errors = [f"{'.'.join(str(p) for p in e['loc'][:4])}: {e['msg']}" for e in exc.errors()[:3]]
            return None, f"invalid_schema: {'; '.join(errors)}"[:400]
        error = self.timestamp_error(batch, received_at)
        return (None, error) if error else (batch, None)

    def timestamp_error(self, batch: TelemetryBatchIn, received_at: datetime) -> str | None:
        """Timestamps are device-clock UTC. Moderate drift is tracked (see ``SequenceTracker``), but data
        dated beyond INGEST_MAX_FUTURE_SKEW_S would corrupt ordering and retention, so it is refused."""
        horizon = received_at + self._max_future
        if batch.sent_at > horizon or (batch.samples and max(s.timestamp for s in batch.samples) > horizon):
            return "timestamp_in_future: device clock is too far ahead of the server"
        return None

    # ----------------------------------------------------------------- ingest
    async def apply(self, batch: TelemetryBatchIn, received_at: datetime) -> tuple[str, int]:
        """Dedupe + apply one validated, authorized batch. Returns (status, accepted samples)."""
        c = self._c
        concurrent = bool(batch.batch_id) and batch.batch_id in self._applying
        if concurrent or c.deduper.seen(batch.batch_id):
            # Phase 10: a copy of a batch still being applied (agent retry racing the first request) is a
            # duplicate too; before, both copies reached the twin, anomaly state and the event bus
            INGEST_DUPLICATES.labels("in_flight" if concurrent else "seen").inc()
            self.counters.duplicates += 1
            self.sequences.duplicate(batch.device_id)
            SEQUENCE_OBSERVED.labels("duplicate").inc()
            INGEST_BATCHES.labels("duplicate").inc()
            self._contact(batch.device_id, received_at)
            return "duplicate", 0
        if batch.batch_id:
            self._applying.add(batch.batch_id)
        try:
            return await self._apply(batch, received_at)
        finally:
            if batch.batch_id:
                self._applying.discard(batch.batch_id)  # failed batches are not remembered: the agent retries

    async def _apply(self, batch: TelemetryBatchIn, received_at: datetime) -> tuple[str, int]:
        c = self._c
        started = time.perf_counter()
        queued_before = c.persister.queued_seq
        accepted = int(await c.telemetry.ingest(batch, received_at))  # UnknownDeviceError -> 409
        queued_after = c.persister.queued_seq
        if batch.processes is not None and not batch.replay:
            c.process_history.add(batch.device_id, batch.processes)
        if batch.events:
            c.telemetry.record_device_events(batch.device_id, batch.events)
        c.sync.offer_batch(batch)
        c.deduper.remember(batch.batch_id)
        self.latency.observe("server_processing_ms", (time.perf_counter() - started) * 1000)
        if not batch.replay:
            collected = batch.collected_at or batch.sent_at
            self.latency.observe("collection_to_server_ms", (received_at - collected).total_seconds() * 1000)
        kind = self.sequences.observe(batch.device_id, batch.sequence, batch.sent_at, received_at)
        SEQUENCE_OBSERVED.labels(kind).inc()
        if kind == "reset":
            log.info("sequence_reset", device_id=batch.device_id, sequence=batch.sequence)
        self._contact(batch.device_id, received_at)
        if batch.batch_id:
            receipt = Receipt(
                batch.batch_id,
                batch.device_id,
                batch.sequence,
                batch.schema_version,
                batch.collected_at,
                received_at,
                len(batch.samples),
                len(batch.events),
                batch.replay,
            )
            if queued_after > queued_before:  # wait until its rows are written
                self._awaiting.append((queued_before + 1, queued_after, receipt))
                self._awaiting_ids.add(batch.batch_id)
            else:  # nothing to write (all downsampled / unavailable): durable now
                self.receipts.add(receipt)
        self.counters.accepted += 1
        self.counters.samples += accepted
        self.counters.events += len(batch.events)
        return "accepted", accepted

    def _on_flushed(self, last_seq: int) -> None:
        dropped_through = self._c.persister.max_dropped_seq
        while self._awaiting and self._awaiting[0][1] <= last_seq:
            first, _, receipt = self._awaiting.popleft()
            self._awaiting_ids.discard(receipt.batch_id)
            if first <= dropped_through:
                # some of its rows were evicted unwritten (queue overflow): never confirm it; forget it so
                # the agent's resend (it is now "unknown") is applied again instead of seen as a duplicate
                self._c.deduper.forget(receipt.batch_id)
                self.lost_batches += 1
                continue
            self.receipts.add(receipt)

    async def classify(self, device_id: str, batch_ids: list[str]) -> dict[str, list[str]] | None:
        """Durable / pending (accepted, still in memory) / unknown (lost or never received)."""
        if not self.receipts.enabled:
            return None  # no durable store: confirmation not offered (agents delete on 202)
        durable = await self.receipts.durable(device_id, batch_ids)
        pending = {
            b for b in batch_ids if b not in durable and (b in self._awaiting_ids or b in self._applying)
        }
        unknown = [b for b in batch_ids if b not in durable and b not in pending]
        return {"durable": sorted(durable), "pending": sorted(pending), "unknown": unknown}

    def _contact(self, device_id: str, now: datetime) -> None:
        change = self.presence.batch_received(device_id, now)
        if change is not None:
            self._c.publish_soon(change)

    async def ingest_bulk(self, agent: Any, items: list[Any], received_at: datetime) -> BulkAck:
        """Each batch is validated and acknowledged individually: one bad batch never blocks the rest."""
        self.counters.requests += 1
        ack = BulkAck(results=[], server_received_at=received_at, durable_confirmation=self.receipts.enabled)
        results = ack.results  # (pydantic copies lists passed to the constructor)
        device_ids: set[str] = set()
        for item in items:
            raw_id = str(item.get("batch_id") or "")[:64] if isinstance(item, dict) else ""
            batch, error = self.parse(item, received_at)
            if batch is None:
                reason = (error or "invalid").split(":", 1)[0]
                self._reject(reason)
                ack.rejected += 1
                results.append(BatchResultOut(batch_id=raw_id, status="rejected", detail=error))
                continue
            if not agent.allows(batch.device_id):
                raise PermissionError(batch.device_id)
            status, _ = await self.apply(batch, received_at)
            device_ids.add(batch.device_id)
            if status == "duplicate":
                ack.duplicates += 1
            else:
                ack.accepted += 1
            results.append(BatchResultOut(batch_id=batch.batch_id or raw_id, status=status))
        if len(device_ids) == 1:
            ack.last_sequence = self.sequences.last_sequence(next(iter(device_ids)))
        return ack

    def stats(self) -> dict[str, Any]:
        return {
            "inflight": self.inflight,
            "inflight_max_seen": self.inflight_max_seen,
            "max_inflight": self._max_inflight,
            "counters": vars(self.counters).copy(),
            "rejections": dict(self.rejections),
            "latency": self.latency.summary(),
            "receipts": {
                "pending": self.receipts.pending,
                "written": self.receipts.written,
                "dropped": self.receipts.dropped,
                "last_error": self.receipts.last_error,
            },
        }
