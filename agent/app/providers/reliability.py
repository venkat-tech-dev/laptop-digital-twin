from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.contracts import MetricKind
from app.errors import TelemetryError
from app.platform.wmi import WmiQueryable
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC_NVME = "Windows storage stack (NVMe SMART / Health log 02h)"
SRC_WHEA = "Windows event log (Microsoft-Windows-WHEA-Logger)"

_SLOW_S = 300.0  # security state and WHEA scan cadence


class ReliabilityProvider(TelemetryProvider):
    """Drive SMART health log and WHEA hardware-error counts (security posture: providers/security.py).

    Everything here works without administrator rights. The WHEA scan changes
    rarely, so they are refreshed every 5 minutes; NVMe health on every collection.
    """

    name = "reliability"
    lane = "slow"
    timeout_s = 60.0
    component = "os"

    def __init__(
        self,
        interval_ms: int,
        drives: list[int],
        nvme_health: Callable[[int], dict[str, Any]],
        whea: Callable[[], dict[str, Any]],
        wmi: WmiQueryable | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(interval_ms)
        self._drives = drives
        self._nvme = nvme_health
        self._whea = whea
        self._wmi = wmi
        self._clock = clock
        self._slow_at: float | None = None
        self._slow: list[Reading] = []
        self.last_whea: dict[str, Any] | None = None

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [
            MetricSpec("disk.wear_percent", "percent", SRC_NVME),
            MetricSpec("disk.smart_temperature_c", "celsius", SRC_NVME),
            MetricSpec("system.whea_errors_30d", "count", SRC_WHEA),
        ]

    def collect(self) -> list[Reading]:
        out = self._drive_readings()
        now = self._clock()
        if self._slow_at is None or now - self._slow_at >= _SLOW_S:
            self._slow_at = now
            self._slow = self._whea_readings()
        return out + self._slow

    # ------------------------------------------------------------------ drives
    def _drive_readings(self) -> list[Reading]:
        out: list[Reading] = []
        for idx in self._drives:
            labels = {"disk": f"PhysicalDrive{idx}"}
            try:
                h = self._nvme(idx)
            except TelemetryError as exc:
                for metric, unit in (
                    ("disk.wear_percent", "percent"),
                    ("disk.smart_temperature_c", "celsius"),
                ):
                    out.append(Reading.unavailable(metric, "storage", unit, SRC_NVME, exc.detail, labels))
                continue
            for metric, key, unit in (
                ("disk.smart_temperature_c", "temperature_c", "celsius"),
                ("disk.wear_percent", "percentage_used", "percent"),
                ("disk.available_spare_percent", "available_spare_percent", "percent"),
                ("disk.power_on_hours", "power_on_hours", "hours"),
                ("disk.power_cycles", "power_cycles", "count"),
                ("disk.unsafe_shutdowns", "unsafe_shutdowns", "count"),
                ("disk.media_errors", "media_errors", "count"),
                ("disk.data_written_bytes", "data_written_bytes", "bytes"),
                ("disk.data_read_bytes", "data_read_bytes", "bytes"),
                ("disk.critical_warning", "critical_warning", "bitmask"),
            ):
                value = h.get(key)
                if value is None:
                    out.append(
                        Reading.unavailable(
                            metric, "storage", unit, SRC_NVME, "Not reported by drive", labels
                        )
                    )
                else:
                    out.append(Reading(metric, "storage", value, unit, SRC_NVME, labels=labels))
        return out

    # ------------------------------------------------------------------ security / WHEA
    def _whea_readings(self) -> list[Reading]:
        try:
            w = self._whea()
        except TelemetryError as exc:
            self.last_whea = None
            return [
                Reading.unavailable("system.whea_errors_30d", "os", "count", SRC_WHEA, exc.detail),
                Reading.unavailable("system.whea_fatal_30d", "os", "count", SRC_WHEA, exc.detail),
            ]
        self.last_whea = w
        return [
            Reading("system.whea_errors_30d", "os", w["total"], "count", SRC_WHEA, MetricKind.DERIVED),
            Reading("system.whea_fatal_30d", "os", w["fatal"], "count", SRC_WHEA, MetricKind.DERIVED),
        ]
