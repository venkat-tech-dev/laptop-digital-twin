from __future__ import annotations

import time
from collections.abc import Callable
from types import ModuleType

import psutil as _psutil

from app.contracts import MetricKind
from app.errors import TelemetryError
from app.platform.wmi import WmiQueryable
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC_BOOT = "psutil (GetTickCount64)"
SRC_BRIGHTNESS = "WMI root\\wmi WmiMonitorBrightness"


class SystemProvider(TelemetryProvider):
    """Uptime and OS-level counters."""

    name = "system"
    component = "os"

    def __init__(
        self, interval_ms: int, psutil: ModuleType = _psutil, clock: Callable[[], float] = time.time
    ) -> None:
        super().__init__(interval_ms)
        self._ps = psutil
        self._clock = clock

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("system.uptime_s", "s", SRC_BOOT)]

    def collect(self) -> list[Reading]:
        boot = self._ps.boot_time()
        return [
            self._r(
                "system.uptime_s", max(0, int(self._clock() - boot)), "s", SRC_BOOT, kind=MetricKind.DERIVED
            ),
            self._r("system.boot_time_epoch_s", int(boot), "s", SRC_BOOT, kind=MetricKind.STATIC),
        ]


class DisplayProvider(TelemetryProvider):
    """Built-in panel brightness (drives the 3D screen emission so it mirrors the real panel)."""

    name = "display"
    lane = "wmi"
    timeout_s = 10.0
    component = "display"

    def __init__(self, interval_ms: int, wmi: WmiQueryable | None) -> None:
        super().__init__(interval_ms)
        self._wmi = wmi

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("display.brightness_percent", "percent", SRC_BRIGHTNESS)]

    def collect(self) -> list[Reading]:
        if self._wmi is None:
            return [self._na("display.brightness_percent", "percent", SRC_BRIGHTNESS, "WMI unavailable")]
        try:
            rows = self._wmi.query("SELECT CurrentBrightness, Active FROM WmiMonitorBrightness", "root\\wmi")
        except TelemetryError as exc:
            return [self._na("display.brightness_percent", "percent", SRC_BRIGHTNESS, exc.detail)]
        active = [r for r in rows if r.get("Active") and r.get("CurrentBrightness") is not None]
        if not active:
            return [
                self._na(
                    "display.brightness_percent",
                    "percent",
                    SRC_BRIGHTNESS,
                    "No internal panel with software brightness control (external monitor or lid closed)",
                )
            ]
        return [
            self._r(
                "display.brightness_percent", int(active[0]["CurrentBrightness"]), "percent", SRC_BRIGHTNESS
            )
        ]
