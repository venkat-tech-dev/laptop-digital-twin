"""Runs the twin engine inside the pipeline and distributes its output.

* after every applied batch: project -> ``twin.state.patch`` (+ ``twin.status.changed``,
  ``twin.event.created``) to the device's subscribers
* every second: time-driven freshness / connectivity transitions (``tick``)
* fleet subscribers get a compact ``twin.summary`` per device, at most every ``summary_interval_s``
  unless connectivity / health / visual state changed (then immediately)
* timeline events are persisted as ``system_events`` (``twin.*`` types, idempotent ``event_uid``)
* the current document is kept in Redis so a backend restart resumes from the last known state
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from app.domain.events.events import TwinMessage
from app.repositories.base import SystemEventRecord
from app.services.twin_engine import TwinChange, TwinEngine

log = structlog.get_logger("twin")

SUMMARY_FIELDS = {
    "cpu": "performance.cpu.usage_percent",
    "memory": "performance.memory.usage_percent",
    "disk": "performance.disk.usage_percent",
    "battery": "battery.charge_percent",
    "temperature": "thermal.temperature_c",
    "internet": "network.internet_connected",
}


class TwinStateService:
    def __init__(
        self,
        engine: TwinEngine,
        twins: Any,  # DigitalTwinService
        presence: Any,  # PresenceService
        bus: Any,
        record: Callable[[SystemEventRecord], None],
        redis: Any | None,
        extras: Callable[[str], dict[str, Any]] | None = None,
        summary_interval_s: float = 10.0,
        persist_interval_s: float = 10.0,
    ) -> None:
        self.engine = engine
        self._twins = twins
        self._presence = presence
        self._bus = bus
        self._record = record
        self._redis = redis
        self.extras = extras or (lambda _d: {})
        self._summary_interval = summary_interval_s
        self._persist_interval = persist_interval_s
        self._last_summary: dict[str, tuple[float, dict[str, Any]]] = {}
        self._last_persist: dict[str, float] = {}
        self.patches_sent = 0
        self.patch_bytes = 0
        self.patch_fields = 0
        self.patches_serialized = 0  # patches somebody watched (others are skipped by interest)
        self.latency: Any = None  # LatencyStats, wired by the container

    # ------------------------------------------------------------------ inputs
    async def on_applied(self, device_id: str, now: datetime | None = None) -> TwinChange | None:
        twin = self._twins.get(device_id)
        if twin is None:
            return None
        now = now or datetime.now(UTC)
        started = time.perf_counter()
        change = self.engine.project(twin, now, self._presence.presence_of(device_id), self.extras(device_id))
        if self.latency is not None:
            self.latency.observe("twin_projection_ms", (time.perf_counter() - started) * 1000)
        if change is not None:
            await self._publish(change)
        return change

    async def tick(self, now: datetime | None = None) -> list[TwinChange]:
        now = now or datetime.now(UTC)
        twins = {d.device_id: self._twins.get(d.device_id) for d in self._twins.devices()}
        changes = self.engine.tick(twins, self._presence.presence_of, now)
        for change in changes:
            await self._publish(change)
        return changes

    async def topology_changed(self, device_id: str) -> None:
        """Hardware inventory rebuilt the component tree: clients must reload the snapshot."""
        await self._bus.publish_all(
            [
                TwinMessage(
                    device_id=device_id, kind="twin.sync.required", body={"reason": "inventory_changed"}
                )
            ]
        )

    # ------------------------------------------------------------------ outputs
    async def _publish(self, change: TwinChange) -> None:
        epoch = self.engine.epoch
        messages = [
            TwinMessage(
                device_id=change.device_id,
                kind="twin.state.patch",
                body={
                    "twin_version": change.version,
                    "base_version": change.base_version,
                    "epoch": epoch,
                    "changes": change.replace,  # whole values (null = removed)
                    "merge": change.merge,  # partial field objects: merge into the current field
                },
            )
        ]
        if change.connectivity is not None:
            messages.append(
                TwinMessage(
                    device_id=change.device_id,
                    kind="twin.status.changed",
                    body={
                        "previous": change.connectivity[0],
                        "status": change.connectivity[1],
                        "twin_version": change.version,
                        "epoch": epoch,
                    },
                )
            )
        for ev in change.events:
            messages.append(
                TwinMessage(
                    device_id=change.device_id,
                    kind="twin.event.created",
                    body={"timeline_event": ev.to_dict()},
                )
            )
            self._record(
                SystemEventRecord(
                    change.device_id,
                    ev.timestamp,
                    ev.type,
                    ev.severity,
                    ev.message,
                    ev.data,
                    event_uid=ev.event_id,
                    priority="high" if ev.severity in ("warning", "error", "critical") else "normal",
                    category="twin",
                )
            )
        summary = self._summary_if_due(change)
        if summary is not None:
            messages.append(
                TwinMessage(device_id=change.device_id, kind="twin.summary", body={"device": summary})
            )
        self.patches_sent += 1
        self.patch_fields += len(change.changes)
        await self._bus.publish_all(messages)
        await self._persist(change.device_id)

    def summary(self, device_id: str) -> dict[str, Any] | None:
        doc = self.engine.docs.get(device_id)
        if doc is None:
            return None
        s = doc.state

        def val(path: str) -> Any:
            v = s.get(path)
            return v.get("value") if isinstance(v, dict) else None

        out: dict[str, Any] = {
            "device_id": device_id,
            "twin_version": doc.version,
            "hostname": s.get("identity.hostname"),
            "manufacturer": s.get("identity.manufacturer"),
            "model": s.get("identity.model"),
            "owner": s.get("identity.owner"),
            "department": s.get("identity.department"),
            "connectivity": s.get("connectivity.status"),
            "last_telemetry_at": s.get("connectivity.last_telemetry_at"),
            "health": (s.get("health") or {}).get("state"),
            "visual": (s.get("sections.device") or {}).get("visual"),
            "active_alerts": s.get("alerts.active_count") or 0,
            "highest_severity": s.get("alerts.highest_severity"),
        }
        for key, path in SUMMARY_FIELDS.items():
            out[key] = val(path)
            fs = s.get(path)
            out[f"{key}_status"] = fs.get("status") if isinstance(fs, dict) else None
        return out

    def _summary_if_due(self, change: TwinChange) -> dict[str, Any] | None:
        summary = self.summary(change.device_id)
        if summary is None:
            return None
        now = time.monotonic()
        last = self._last_summary.get(change.device_id)
        important = ("connectivity", "health", "visual", "active_alerts", "highest_severity")
        urgent = last is None or any(last[1].get(k) != summary.get(k) for k in important)
        if not urgent and now - last[0] < self._summary_interval:  # type: ignore[index]
            return None
        self._last_summary[change.device_id] = (now, summary)
        return summary

    async def _persist(self, device_id: str, force: bool = False) -> None:
        if self._redis is None or not getattr(self._redis, "connected", False):
            return
        now = time.monotonic()
        if not force and now - self._last_persist.get(device_id, 0.0) < self._persist_interval:
            return
        self._last_persist[device_id] = now
        raw = self.engine.dump(device_id)
        if raw is None:
            return
        try:
            await self._redis.save_twin_doc(device_id, raw)
        except Exception as exc:
            log.debug("twin_doc_save_failed", error=str(exc)[:200])

    def stats(self) -> dict[str, Any]:
        e = self.engine
        return {
            "documents": len(e.docs),
            "epoch": e.epoch,
            "projections": e.projections,
            "projection_avg_ms": round(e.projection_seconds / e.projections * 1000, 3)
            if e.projections
            else None,
            "patches_sent": self.patches_sent,
            "avg_patch_fields": round(self.patch_fields / self.patches_sent, 1)
            if self.patches_sent
            else None,
            "patches_serialized": self.patches_serialized,
            "avg_patch_bytes": round(self.patch_bytes / self.patches_serialized)
            if self.patches_serialized
            else None,
        }

    async def persist_all(self) -> None:
        for device_id in list(self.engine.docs):
            await self._persist(device_id, force=True)

    async def restore(self, device_ids: list[str]) -> int:
        if self._redis is None:
            return 0
        restored = 0
        for device_id in device_ids:
            try:
                raw = await self._redis.load_twin_doc(device_id)
            except Exception as exc:
                log.debug("twin_doc_load_failed", error=str(exc)[:200])
                continue
            if raw:
                self.engine.restore(device_id, raw)
                restored += 1
        return restored
