"""Synchronises the local SQLite outbox with the backend.

Order: the newest batch first (so the live twin is current immediately after reconnecting), then the
backlog by priority (CRITICAL/HIGH first) and age in bulk chunks flagged ``replay`` (the backend
persists those as history without overwriting newer live state). A chunk holds at most
``batch_size`` batches and ``max_batch_bytes`` of JSON (gzip-compressed on the wire).

Failure policy:

* Retryable (timeout, connection refused/reset, 5xx, 408/425, 401/403 credential problems): nothing is
  dropped; the next attempt waits ``min(max, base * 2^n)`` seconds with 50-100 % jitter.
* 429 / 503 with ``Retry-After``: the next attempt waits at least that long.
* Per-batch rejection (``invalid_schema``, ``malformed_payload``, ``timestamp_in_future``): the batch
  can never succeed, so it is dead-lettered at once (deleted, counted, logged) - one bad batch never
  blocks the queue and never takes the rest of the queue with it.
* ``unsupported_schema_version``: the backend is older/newer than this agent. Uploads pause for
  ``SCHEMA_PAUSE_S`` and the queue is kept (collection continues; queue limits bound disk use) so the
  data is delivered once the backend is upgraded.
* A whole request rejected as invalid (4xx): attempt counter, dead-letter after ``max_attempts``.
* 409 unknown device: the hardware inventory is re-announced before retrying.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime

import structlog

from app.contracts import InventoryEnvelope
from app.storage.queue import QueuedBatch, TelemetryStore
from app.transport.client import AgentApiClient, ApiError

#: an accepted batch never confirmed durable is sent again after this (the backend deduplicates)
RESEND_UNCONFIRMED_AFTER_S = 600.0

log = structlog.get_logger("agent.sync")

MAX_CHUNKS_PER_CYCLE = 10
SCHEMA_PAUSE_S = 3600.0


def mark_replay(payload: bytes) -> bytes:
    data = json.loads(payload)
    data["replay"] = True
    return json.dumps(data, separators=(",", ":")).encode()


class SyncManager:
    def __init__(
        self,
        store: TelemetryStore,
        client: AgentApiClient,
        inventory: Callable[[], InventoryEnvelope],
        *,
        batch_size: int,
        max_attempts: int,
        backoff_base_s: float,
        backoff_max_s: float,
        replay_after_s: float = 30.0,
        max_batch_bytes: int = 1_000_000,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._store = store
        self._client = client
        self._inventory = inventory
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._base = backoff_base_s
        self._max = backoff_max_s
        self._replay_after = replay_after_s
        self._max_bytes = max_batch_bytes
        self._clock = clock
        self._rng = rng
        self._next_attempt = 0.0
        self._inventory_sent = False
        self.online = False
        self.last_sync_at: datetime | None = None
        self.failures_consecutive = 0
        self.failures_total = 0
        self.delivered_total = 0
        self.duplicates_total = 0
        self.rejected_total = 0
        self.last_error: str | None = None
        self.paused_reason: str | None = None
        self.server_last_sequence: int | None = None

    def mark_inventory_stale(self) -> None:
        self._inventory_sent = False

    def retry_now(self) -> None:
        """Connectivity came back (internet restored / heartbeat answered): skip remaining backoff."""
        if self.paused_reason is None:
            self._next_attempt = 0.0

    def backoff_s(self) -> float:
        raw = min(self._max, self._base * (2 ** max(0, self.failures_consecutive - 1)))
        return float(raw * (0.5 + 0.5 * self._rng()))

    async def sync_once(self) -> int:
        """One synchronisation cycle. Never raises for backend/network problems."""
        if self._clock() < self._next_attempt:
            return 0
        if self.paused_reason is not None:
            self.paused_reason = None  # the pause expired: probe again
        delivered = 0
        try:
            if not self._inventory_sent:
                await self._client.publish_inventory(self._inventory())
                self._inventory_sent = True
            newest = self._store.due(1, newest_first=True)
            if newest and self._clock() - newest[0].created_at <= self._replay_after:
                delivered += await self._send(newest, replay=False)
            for _ in range(MAX_CHUNKS_PER_CYCLE):
                rows = self._cap_bytes(self._store.due(self._batch_size))
                if not rows or self.paused_reason is not None:
                    break
                delivered += await self._send(rows, replay=True)
        except ApiError as exc:
            self._on_failure(exc)
            return delivered
        if self.paused_reason is not None:
            return delivered
        if not self.online:
            log.info("backend_connected", delivered_total=self.delivered_total + delivered)
        self.online = True
        self.failures_consecutive = 0
        self.last_error = None
        self.last_sync_at = datetime.now(UTC)
        self.delivered_total += delivered
        return delivered

    def _cap_bytes(self, rows: list[QueuedBatch]) -> list[QueuedBatch]:
        out: list[QueuedBatch] = []
        total = 0
        for r in rows:
            if out and total + len(r.payload) > self._max_bytes:
                break
            out.append(r)
            total += len(r.payload)
        return out

    async def _send(self, rows: list[QueuedBatch], replay: bool) -> int:
        payloads = [mark_replay(r.payload) if replay else r.payload for r in rows]
        try:
            results = await self._client.publish_batches(payloads)
        except ApiError as exc:
            if not exc.retryable:  # the whole request was rejected as invalid
                self._count_attempts(rows, exc.detail)
            raise
        by_id = {r.batch_id: r for r in results}
        ok: list[int] = []
        rejected: list[QueuedBatch] = []
        for row in rows:
            res = by_id.get(row.batch_id)
            if res is None:
                continue  # not processed this time: stays queued
            if res.status in ("accepted", "duplicate"):
                ok.append(row.row_id)
                if res.status == "duplicate":
                    self.duplicates_total += 1
            else:
                rejected.append(row)
        summary = getattr(self._client, "last_summary", None)
        if summary is not None and getattr(summary, "durable_confirmation", False):
            # Phase 10 backend: keep them until the heartbeat confirms they are written (crash-safe)
            self._store.mark_sent(ok, RESEND_UNCONFIRMED_AFTER_S)
        else:
            self._store.ack(ok)
        if summary is not None and summary.last_sequence is not None:
            self.server_last_sequence = summary.last_sequence
        unsupported = [
            r for r in rejected if (by_id[r.batch_id].detail or "").startswith("unsupported_schema")
        ]
        invalid = [r for r in rejected if r not in unsupported]
        if invalid:
            self.rejected_total += len(invalid)
            self._store.dead_letter(
                [r.row_id for r in invalid],
                "; ".join(filter(None, (by_id[r.batch_id].detail for r in invalid)))[:300] or "rejected",
            )
        if unsupported:
            self._pause(by_id[unsupported[0].batch_id].detail or "unsupported_schema_version")
        return len(ok)

    def _pause(self, reason: str) -> None:
        if self.paused_reason is None:
            log.error("sync_paused_unsupported_schema", reason=reason[:200], resume_in_s=SCHEMA_PAUSE_S)
        self.paused_reason = reason[:300]
        self._next_attempt = self._clock() + SCHEMA_PAUSE_S

    def resume(self) -> None:
        """Retry after a schema pause (e.g. on agent restart or when the operator upgraded the backend)."""
        self.paused_reason = None
        self._next_attempt = 0.0

    def _count_attempts(self, rows: list[QueuedBatch], error: str) -> None:
        dead = [r.row_id for r in rows if r.attempts + 1 >= self._max_attempts]
        retry = [r.row_id for r in rows if r.attempts + 1 < self._max_attempts]
        self._store.dead_letter(dead, error or "rejected by backend")
        self._store.retry_later(retry, error or "rejected by backend", self._clock() + self.backoff_s(), True)

    def _on_failure(self, exc: ApiError) -> None:
        if exc.status == 409:
            self._inventory_sent = False
        self.failures_consecutive += 1
        self.failures_total += 1
        self.last_error = exc.detail[:300]
        delay = self.backoff_s()
        if exc.retry_after_s is not None:
            delay = max(delay, exc.retry_after_s)  # the backend asked us to slow down (429/503)
        self._next_attempt = self._clock() + delay
        if self.online or self.failures_consecutive == 1:
            log.warning(
                "backend_unreachable", error=exc.detail[:200], status=exc.status, retry_in_s=round(delay, 1)
            )
        self.online = False
        self._inventory_sent = self._inventory_sent and exc.status not in (None, 401, 403)
