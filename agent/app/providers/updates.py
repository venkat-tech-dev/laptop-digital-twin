from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from app.contracts import DeviceEvent, EventSeverity
from app.errors import TelemetryError
from app.platform import updates as wu
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC = "Windows Update Agent (offline, last scan results)"


class UpdatesProvider(TelemetryProvider):
    """Windows Update posture: reboot required, last installed update, pending updates.

    Read-only: nothing is downloaded or installed. The pending-update search (slow) runs every
    ``search_interval_s``; reboot state and history on every collection.
    """

    name = "updates"
    component = "os"
    lane = "updates"
    timeout_s = 180.0

    def __init__(
        self,
        interval_ms: int,
        search_interval_s: float,
        *,
        reboot_required: Callable[[], bool] = wu.reboot_required,
        history: Callable[[int], tuple[int, list[wu.UpdateHistoryEntry]]] = wu.update_history,
        pending: Callable[[], dict[str, int]] = wu.pending_updates,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(interval_ms)
        self._search_every = search_interval_s
        self._reboot = reboot_required
        self._history = history
        self._pending = pending
        self._clock = clock
        self._last_search: float | None = None
        self._pending_readings: list[Reading] = []
        self._latest_seen: datetime | None = None
        self._events: list[DeviceEvent] = []

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("system.update_reboot_required", "bool", SRC)]

    def pop_events(self) -> list[DeviceEvent]:
        out, self._events = self._events, []
        return out

    def collect(self) -> list[Reading]:
        out: list[Reading] = []
        try:
            out.append(self._r("system.update_reboot_required", self._reboot(), "bool", SRC))
        except TelemetryError as exc:
            out.append(self._na("system.update_reboot_required", "bool", SRC, exc.detail))
        out += self._history_readings()
        now = self._clock()
        if self._last_search is None or now - self._last_search >= self._search_every:
            self._last_search = now
            try:
                p = self._pending()
                self._pending_readings = [
                    self._r("system.updates_pending", p["total"], "count", SRC),
                    self._r("system.updates_pending_security", p["security"], "count", SRC),
                ]
            except TelemetryError as exc:
                self._pending_readings = [self._na("system.updates_pending", "count", SRC, exc.detail)]
        return out + self._pending_readings

    def _history_readings(self) -> list[Reading]:
        try:
            total, entries = self._history(20)
        except TelemetryError as exc:
            return [self._na("system.last_update_installed_at", "timestamp", SRC, exc.detail)]
        ok = [e for e in entries if e.succeeded]
        out = [self._r("system.update_history_count", total, "count", SRC)]
        if not ok:
            out.append(
                self._na(
                    "system.last_update_installed_at", "timestamp", SRC, "No successful update in history"
                )
            )
            return out
        latest = max(ok, key=lambda e: e.date)
        out.append(self._r("system.last_update_installed_at", latest.date.isoformat(), "timestamp", SRC))
        out.append(self._r("system.last_update_title", latest.title, "text", SRC))
        if self._latest_seen is not None:
            for e in sorted(ok, key=lambda e: e.date):
                if e.date > self._latest_seen:
                    self._events.append(
                        DeviceEvent(
                            type="update_installed",
                            severity=EventSeverity.INFO,
                            timestamp=e.date,
                            source=SRC,
                            message=f"Update installed: {e.title}",
                            data={"title": e.title},
                        )
                    )
        self._latest_seen = latest.date if self._latest_seen is None else max(self._latest_seen, latest.date)
        failed = sum(1 for e in entries if not e.succeeded)
        out.append(self._r("system.update_failures_recent", failed, "count", SRC))
        return out
