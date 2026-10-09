"""Batch accumulation between flushes (latest value per metric+labels).

Flushed batches go to the durable SQLite outbox (``app.storage.queue``); delivery is handled by
``app.publisher.sync``. Nothing is ever fabricated to fill gaps.
"""

from __future__ import annotations

import time

import structlog

from app.contracts import (
    AgentHealth,
    DeviceEvent,
    DeviceHealth,
    MetricKind,
    MetricSample,
    Priority,
    ProcessSnapshot,
)

log = structlog.get_logger("agent.publisher")


def sample_key(sample: MetricSample) -> str:
    if not sample.labels:
        return sample.metric
    return sample.metric + "{" + ",".join(f"{k}={v}" for k, v in sorted(sample.labels.items())) + "}"


MAX_PENDING_EVENTS = 500


class BatchAccumulator:
    """Coalesces collector output between flushes: latest value per metric+labels, latest process
    snapshot, all events (bounded), latest device health and agent health."""

    def __init__(self, static_resend_s: float = 600.0) -> None:
        self._static_resend_s = static_resend_s
        self._static_sent: dict[str, tuple[object, float]] = {}  # key -> (value/availability, sent at)
        self.static_suppressed_total = 0
        self.events_generated_total = 0
        self._samples: dict[str, MetricSample] = {}
        self._processes: ProcessSnapshot | None = None
        self._events: list[DeviceEvent] = []
        self._device_health: DeviceHealth | None = None
        self._agent_health: AgentHealth | None = None
        self.events_dropped = 0

    def add(self, samples: list[MetricSample], processes: ProcessSnapshot | None = None) -> None:
        for s in samples:
            self._samples[sample_key(s)] = s
        if processes is not None:
            self._processes = processes

    def add_events(self, events: list[DeviceEvent]) -> None:
        self.events_generated_total += len(events)
        self._events.extend(events)
        overflow = len(self._events) - MAX_PENDING_EVENTS
        if overflow > 0:
            self.events_dropped += overflow
            del self._events[:overflow]

    def set_device_health(self, health: DeviceHealth) -> None:
        self._device_health = health

    def set_agent_health(self, health: AgentHealth) -> None:
        self._agent_health = health

    def drain(self) -> tuple[list[MetricSample], ProcessSnapshot | None]:
        samples, procs = self._without_unchanged_static(list(self._samples.values())), self._processes
        self._samples, self._processes = {}, None
        return samples, procs

    def _without_unchanged_static(self, samples: list[MetricSample]) -> list[MetricSample]:
        """STATIC samples (capacities, versions, configuration) are sent when they change and as a
        periodic keyframe every ``static_resend_s``; in between they are redundant on the wire."""
        now = time.monotonic()
        out: list[MetricSample] = []
        for s in samples:
            if s.kind is not MetricKind.STATIC:
                out.append(s)
                continue
            key = sample_key(s)
            fingerprint = (s.value, s.availability, s.reason)
            last = self._static_sent.get(key)
            if last is not None and last[0] == fingerprint and now - last[1] < self._static_resend_s:
                self.static_suppressed_total += 1
                continue
            self._static_sent[key] = (fingerprint, now)
            out.append(s)
        return out

    def resend_static(self) -> None:
        """Force a keyframe (backend reconnected/restarted, or inventory re-announced)."""
        self._static_sent.clear()

    def urgent(self) -> bool:
        """True when a pending event must not wait for the next scheduled flush."""
        return any(e.priority is not None and e.priority.rank >= Priority.HIGH.rank for e in self._events)

    @staticmethod
    def batch_priority(events: list[DeviceEvent]) -> Priority:
        ranks = [e.priority for e in events if e.priority is not None]
        return max(ranks, key=lambda p: p.rank) if ranks else Priority.NORMAL

    def drain_extras(self) -> tuple[list[DeviceEvent], DeviceHealth | None, AgentHealth | None]:
        events, dh, ah = self._events, self._device_health, self._agent_health
        self._events, self._device_health, self._agent_health = [], None, None
        return events, dh, ah

    @property
    def pending(self) -> bool:
        return bool(
            self._samples or self._processes or self._events or self._device_health or self._agent_health
        )

    def __len__(self) -> int:
        return len(self._samples)
