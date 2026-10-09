"""AuditService (Phase 9): central, append-only, hash-chained enterprise audit.

``record`` never blocks the caller: events go to a bounded in-memory buffer that a background loop writes in
batches (one transaction, chain under an advisory lock). ``flush`` writes synchronously (tests, shutdown).
If the buffer is full the oldest *INFO* events are dropped first and a counter records it; WARNING/HIGH
events are never dropped. Search is always organisation-scoped (platform administrators may search all).
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.logging import request_id_var
from app.core.metrics import AUDIT_DROPPED, AUDIT_EVENTS
from app.domain.governance.audit import AuditEvent, scrub
from app.repositories.governance import AuditFilter

#: spreadsheet formula triggers (OWASP CSV injection): such cells are exported with a leading quote
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

log = structlog.get_logger("audit")
MAX_BUFFER = 50_000  # above this, INFO events make room first
HARD_MAX_BUFFER = 2 * MAX_BUFFER  # absolute bound (security events included) while the database is down
EXPORT_MAX_ROWS = 50_000


class AuditService:
    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self._buffer: deque[AuditEvent] = deque()
        self._lock = asyncio.Lock()
        self.dropped = 0

    def record(
        self,
        org_id: str | None,
        actor_id: str,
        actor_type: str,
        action: str,
        category: str,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        result: str = "SUCCESS",
        reason: str | None = None,
        severity: str = "INFO",
        source: str = "api",
        ip: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        e = AuditEvent(
            uuid.uuid4().hex,
            datetime.now(UTC),
            org_id,
            str(actor_id)[:128],
            actor_type,
            action[:64],
            category,
            resource_type,
            None if resource_id is None else str(resource_id)[:128],
            result,
            None if reason is None else str(reason)[:500],
            severity,
            source,
            request_id_var.get() or None,
            ip,
            scrub(metadata),
        )
        if len(self._buffer) >= MAX_BUFFER:
            victim = next((x for x in self._buffer if x.severity == "INFO"), None)
            if victim is None and severity == "INFO":
                self.dropped += 1
                AUDIT_DROPPED.inc()
                return e
            if victim is not None:
                self._buffer.remove(victim)
                self.dropped += 1
                AUDIT_DROPPED.inc()
            elif len(self._buffer) >= HARD_MAX_BUFFER:
                # Phase 10: bounded even when only WARNING/HIGH events wait (database down for long):
                # the oldest is lost and counted, rather than the process running out of memory
                self._buffer.popleft()
                self.dropped += 1
                AUDIT_DROPPED.inc()
                if self.dropped % 1000 == 1:
                    log.error("audit_buffer_full_dropping_oldest", dropped=self.dropped)
        self._buffer.append(e)
        AUDIT_EVENTS.labels(category, result).inc()
        return e

    async def flush(self) -> int:
        async with self._lock:
            batch: list[AuditEvent] = []
            while self._buffer and len(batch) < 1000:
                batch.append(self._buffer.popleft())
            if not batch:
                return 0
            try:
                await self.repo.append_audit(batch)
            except Exception as exc:  # database down: keep the events, retry on the next flush
                for e in reversed(batch):
                    self._buffer.appendleft(e)
                log.warning("audit_flush_failed", error=str(exc)[:200], pending=len(self._buffer))
                return 0
            return len(batch)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 1.0)
            while await self.flush():
                pass
        while await self.flush():  # drain on shutdown
            pass

    async def search(self, f: AuditFilter, limit: int, before_id: int | None) -> dict[str, Any]:
        await self.flush()
        rows = await self.repo.search_audit(f, limit, before_id)
        return {
            "items": [dict(e.public(), id=i) for i, e in rows],
            "next_before_id": rows[-1][0] if len(rows) == limit else None,
        }

    async def export(self, f: AuditFilter, fmt: str) -> tuple[str, int]:
        await self.flush()
        rows: list[tuple[int, AuditEvent]] = []
        before: int | None = None
        while len(rows) < EXPORT_MAX_ROWS:
            chunk = await self.repo.search_audit(f, min(5000, EXPORT_MAX_ROWS - len(rows)), before)
            if not chunk:
                break
            rows.extend(chunk)
            before = chunk[-1][0]
            if len(chunk) < 5000:
                break
        if fmt == "json":
            return json.dumps([e.public() for _, e in rows], default=str), len(rows)
        buf = io.StringIO()
        cols = [
            "at",
            "org_id",
            "actor_id",
            "actor_type",
            "action",
            "category",
            "resource_type",
            "resource_id",
            "result",
            "reason",
            "severity",
            "source",
            "request_id",
            "ip",
            "event_id",
            "hash",
        ]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for _, e in rows:
            row = e.public()
            # neutralise spreadsheet formula injection in free-text cells
            w.writerow(
                {
                    k: ("'" + str(v) if isinstance(v, str) and v.startswith(FORMULA_START) else v)
                    for k, v in row.items()
                }
            )
        return buf.getvalue(), len(rows)
