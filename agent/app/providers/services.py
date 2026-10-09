from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psutil

from app.contracts import DeviceEvent, EventSeverity
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC = "Windows Service Control Manager"


def _lookup(name: str) -> dict[str, Any] | None:
    try:
        return dict(psutil.win_service_get(name).as_dict())  # type: ignore[attr-defined]
    except psutil.NoSuchProcess:
        return None


class ServicesProvider(TelemetryProvider):
    """Status and start type of an allowlist of important Windows services (not every service)."""

    name = "services"
    component = "os"
    lane = "wmi"
    timeout_s = 20.0

    def __init__(
        self, interval_ms: int, allowlist: list[str], lookup: Callable[[str], dict[str, Any] | None] = _lookup
    ) -> None:
        super().__init__(interval_ms)
        self._allow = allowlist
        self._lookup = lookup
        self._previous: dict[str, str] = {}
        self._events: list[DeviceEvent] = []

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("system.service_status", "state", SRC)]

    def pop_events(self) -> list[DeviceEvent]:
        out, self._events = self._events, []
        return out

    def collect(self) -> list[Reading]:
        out: list[Reading] = []
        running = 0
        for name in self._allow:
            labels = {"service": name}
            try:
                info = self._lookup(name)
            except Exception as exc:  # access denied on a hardened service, etc.
                out.append(
                    self._na("system.service_status", "state", SRC, f"Not readable: {exc}"[:200], labels)
                )
                continue
            if info is None:
                out.append(self._na("system.service_status", "state", SRC, "Service not installed", labels))
                continue
            status = str(info.get("status") or "unknown")
            running += status == "running"
            out.append(self._r("system.service_status", status, "state", SRC, labels=labels))
            out.append(
                self._r(
                    "system.service_start_type",
                    str(info.get("start_type") or "unknown"),
                    "state",
                    SRC,
                    labels=labels,
                )
            )
            before = self._previous.get(name)
            self._previous[name] = status
            if before is not None and before != status:
                auto = str(info.get("start_type")) == "automatic"
                self._events.append(
                    DeviceEvent(
                        type="service_state_changed",
                        severity=EventSeverity.WARNING
                        if auto and status != "running"
                        else EventSeverity.INFO,
                        timestamp=datetime.now(UTC),
                        source=SRC,
                        message=f"{info.get('display_name') or name}: {before} -> {status}",
                        data={
                            "service": name,
                            "from": before,
                            "to": status,
                            "start_type": str(info.get("start_type")),
                        },
                    )
                )
        out.append(self._r("system.services_running", running, "count", SRC))
        return out
