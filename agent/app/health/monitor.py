"""AGENT_HEALTH: the agent's view of itself (uptime, collectors, queue, sync, footprint).

Written atomically to ``health.json`` in the data directory (for local support tooling and the
service watchdog) and attached to a batch every ``agent_health_interval_s``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.contracts import AgentHealth
from app.platform.worker import WorkerPool
from app.publisher.sync import SyncManager
from app.scheduler import CollectionScheduler
from app.storage.queue import TelemetryStore


class HealthMonitor:
    def __init__(
        self,
        *,
        agent_version: str,
        run_mode: str,
        scheduler: CollectionScheduler,
        store: TelemetryStore,
        sync: SyncManager,
        worker: WorkerPool,
        path: Path | None,
    ) -> None:
        self._version = agent_version
        self._mode = run_mode
        self._scheduler = scheduler
        self._store = store
        self._sync = sync
        self._worker = worker
        self._path = path
        self._started = datetime.now(UTC)
        self._started_mono = time.monotonic()
        #: Phase-2 pipeline counters supplied by the runner (api latency, batches, events...).
        self.extra: Callable[[], dict[str, Any]] | None = None

    def snapshot(self) -> AgentHealth:
        stats = self._store.stats()
        return AgentHealth(
            agent_version=self._version,
            run_mode=self._mode,
            started_at=self._started,
            uptime_s=round(time.monotonic() - self._started_mono, 1),
            cpu_percent=self._scheduler.self_cpu_percent,
            memory_rss_bytes=self._scheduler.self_rss_bytes,
            queue_depth=stats.depth,
            queue_bytes=stats.bytes,
            queue_dropped_total=stats.dropped_total + stats.dead_lettered_total,
            last_collection_at=self._scheduler.last_collection_at,
            last_sync_at=self._sync.last_sync_at,
            sync_failures_consecutive=self._sync.failures_consecutive,
            sync_failures_total=self._sync.failures_total,
            lanes_replaced_total=sum(s.replaced for s in self._worker.stats()),
            collectors=[h.model_copy() for h in self._scheduler.health.values()],
            queue_thinned_total=stats.thinned_total,
            upload_failures_total=self._sync.failures_total,
            batches_uploaded_total=self._sync.delivered_total,
            **(self.extra() if self.extra is not None else {}),
        )

    def write(self, health: AgentHealth | None = None) -> None:
        if self._path is None:
            return
        health = health or self.snapshot()
        payload = {
            **json.loads(health.model_dump_json()),
            "sync_online": self._sync.online,
            "sync_last_error": self._sync.last_error,
            "sync_paused_reason": self._sync.paused_reason,
            "server_last_sequence": self._sync.server_last_sequence,
            "queue_by_priority": self._store.stats().by_priority,
            "written_at": datetime.now(UTC).isoformat(),
            "pid": os.getpid(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
