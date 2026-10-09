"""Multi-rate collection scheduler with failure isolation and a self-imposed CPU budget.

* Every collector runs at its own interval on its own worker lane, concurrently with the others.
  A collector that is still running when it becomes due again is skipped (never queued twice).
* A collector exception or timeout becomes ERROR samples for that collector's declared metrics
  only; every other collector keeps running.
* If the agent's own CPU usage exceeds ``cpu_budget_percent`` the intervals are stretched (up to
  4x) and restored when load drops. The stretch factor is itself reported.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from datetime import UTC, datetime

import psutil
import structlog

from app.contracts import (
    CollectorHealth,
    DeviceEvent,
    EventSeverity,
    MetricKind,
    MetricSample,
    ProcessSnapshot,
)
from app.errors import ProviderTimeoutError, TelemetryError
from app.normalization.normalizer import Normalizer
from app.platform.worker import WorkerPool
from app.providers.base import Reading, TelemetryProvider

log = structlog.get_logger("agent.scheduler")

Sink = Callable[[list[MetricSample], ProcessSnapshot | None], None]
EventSink = Callable[[list[DeviceEvent]], None]
SRC_SELF = "ldt-agent self-monitoring"
SELF_METRICS_EVERY_S = 10.0
MAX_STRETCH = 4.0


class CollectionScheduler:
    def __init__(
        self,
        providers: list[TelemetryProvider],
        worker: WorkerPool,
        sink: Sink,
        cpu_budget_percent: float,
        normalizer: Normalizer | None = None,
        provider_timeout_s: float | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self._providers = providers
        self._worker = worker
        self._sink = sink
        self._event_sink = event_sink
        self.collector_failed_after = 3
        self._normalizer = normalizer or Normalizer()
        self._timeout_override = provider_timeout_s
        self._budget = cpu_budget_percent
        self._scale = 1.0
        self._next_due: dict[str, float] = {p.name: 0.0 for p in providers}
        self._in_flight: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self.health: dict[str, CollectorHealth] = {
            p.name: CollectorHealth(name=p.name, lane=p.lane, interval_ms=p.interval_ms) for p in providers
        }
        self.last_collection_at: datetime | None = None
        self._self_proc = psutil.Process()
        self._self_proc.cpu_percent(interval=None)
        self._cpu_count = psutil.cpu_count(logical=True) or 1
        self._last_budget_check = time.monotonic()
        self.self_cpu_percent: float | None = None
        self.self_rss_bytes: int | None = None

    def request_all_now(self) -> int:
        """Make every collector due immediately (remediation REFRESH_TELEMETRY); returns how many."""
        for name in self._next_due:
            self._next_due[name] = 0.0
        return len(self._next_due)

    @property
    def interval_scale(self) -> float:
        return self._scale

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                now = time.monotonic()
                for provider in self._providers:
                    if now < self._next_due[provider.name]:
                        continue
                    self._next_due[provider.name] = now + provider.interval_ms * self._scale / 1000.0
                    if provider.name in self._in_flight:
                        continue  # previous run still executing on its lane: skip this tick
                    self._in_flight.add(provider.name)
                    task = asyncio.create_task(self._run_one(provider), name=f"collect:{provider.name}")
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                if now - self._last_budget_check >= SELF_METRICS_EVERY_S:
                    self._last_budget_check = now
                    self._worker.check_hung()
                    self._sink(self._self_metrics(), None)
                next_due = min(self._next_due.values(), default=now + 1.0)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=max(0.02, next_due - time.monotonic()))
        finally:
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run_one(self, provider: TelemetryProvider) -> None:
        try:
            await self.collect_once(provider)
        finally:
            self._in_flight.discard(provider.name)

    async def collect_once(self, provider: TelemetryProvider) -> None:
        started = time.perf_counter()
        timeout = self._timeout_override or provider.timeout_s
        error: str | None = None
        try:
            readings = await self._worker.run(provider.collect, timeout_s=timeout, lane=provider.lane)
        except TimeoutError:
            error = ProviderTimeoutError(f"{provider.name} did not answer within {timeout:.0f} s").detail
        except TelemetryError as exc:
            error = exc.detail
        except Exception as exc:  # isolation boundary: a collector bug must not stop collection
            log.exception("provider_crashed", provider=provider.name)
            error = f"Collector error: {type(exc).__name__}: {exc}"
        if error is not None:
            readings = self._failed(provider, error)
        duration_ms = (time.perf_counter() - started) * 1000.0
        self._record(provider, error, duration_ms)
        ts = datetime.now(UTC)
        samples = [self._normalizer.normalize(r, ts) for r in readings]
        effective_ms = int(provider.interval_ms * self._scale)
        for smp in samples:
            smp.interval_ms = effective_ms
        snapshot = None
        pop = getattr(provider, "pop_snapshot", None)
        if callable(pop):
            snapshot = pop()
        self._sink(samples, snapshot)
        pop_events = getattr(provider, "pop_events", None)
        if callable(pop_events) and self._event_sink is not None:
            events = pop_events()
            if events:
                self._event_sink(events)

    def _record(self, provider: TelemetryProvider, error: str | None, duration_ms: float) -> None:
        h = self.health[provider.name]
        h.interval_ms = provider.interval_ms
        h.last_duration_ms = round(duration_ms, 2)
        if error is None:
            if h.consecutive_failures >= self.collector_failed_after and self._event_sink is not None:
                self._event_sink(
                    [
                        DeviceEvent(
                            type="collector_recovered",
                            severity=EventSeverity.INFO,
                            timestamp=datetime.now(UTC),
                            source="ldt-agent",
                            message=f"Collector {provider.name} recovered",
                            data={"collector": provider.name},
                        )
                    ]
                )
            h.last_success_at = datetime.now(UTC)
            h.consecutive_failures = 0
            h.last_error = None
            self.last_collection_at = h.last_success_at
        else:
            h.consecutive_failures += 1
            h.total_failures += 1
            h.last_error = error[:300]
            log.warning("provider_failed", provider=provider.name, reason=error[:300])
            if h.consecutive_failures == self.collector_failed_after and self._event_sink is not None:
                self._event_sink(
                    [
                        DeviceEvent(
                            type="collector_failed",
                            severity=EventSeverity.WARNING,
                            timestamp=datetime.now(UTC),
                            source="ldt-agent",
                            message=f"Collector {provider.name} failed {h.consecutive_failures}x in a row",
                            data={"collector": provider.name, "error": error[:200]},
                        )
                    ]
                )

    def _failed(self, provider: TelemetryProvider, reason: str) -> list[Reading]:
        return [
            Reading.failed(m.metric, provider.component, m.unit, m.source, reason)
            for m in provider.declared_metrics
        ]

    def _self_metrics(self) -> list[MetricSample]:
        cpu = self._self_proc.cpu_percent(interval=None) / self._cpu_count
        if cpu > self._budget and self._scale < MAX_STRETCH:
            self._scale = min(MAX_STRETCH, self._scale * 1.5)
            log.warning("cpu_budget_exceeded", agent_cpu_percent=round(cpu, 2), interval_scale=self._scale)
        elif cpu < self._budget / 2 and self._scale > 1.0:
            self._scale = max(1.0, self._scale / 1.5)
        rss = int(self._self_proc.memory_info().rss)
        self.self_cpu_percent, self.self_rss_bytes = round(cpu, 3), rss
        readings = [
            Reading("agent.cpu_percent", "agent", round(cpu, 3), "percent", SRC_SELF),
            Reading("agent.memory_rss_bytes", "agent", rss, "bytes", SRC_SELF),
            Reading(
                "agent.interval_scale", "agent", round(self._scale, 3), "factor", SRC_SELF, MetricKind.DERIVED
            ),
        ]
        for name, h in self.health.items():
            if h.last_duration_ms is not None:
                readings.append(
                    Reading(
                        "agent.collect_duration_ms",
                        "agent",
                        h.last_duration_ms,
                        "ms",
                        SRC_SELF,
                        labels={"provider": name},
                    )
                )
            readings.append(
                Reading(
                    "agent.provider_failures_total",
                    "agent",
                    h.total_failures,
                    "count",
                    SRC_SELF,
                    labels={"provider": name},
                )
            )
        ts = datetime.now(UTC)
        return [self._normalizer.normalize(r, ts) for r in readings]
