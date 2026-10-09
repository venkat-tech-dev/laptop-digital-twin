from __future__ import annotations

from types import ModuleType

import psutil as _psutil

from app.contracts import MetricKind
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC = "psutil (Win32 GlobalMemoryStatusEx)"


class MemoryProvider(TelemetryProvider):
    name = "memory"
    component = "memory"

    def __init__(self, interval_ms: int, psutil: ModuleType = _psutil) -> None:
        super().__init__(interval_ms)
        self._ps = psutil

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("memory.usage_percent", "percent", SRC)]

    def collect(self) -> list[Reading]:
        vm = self._ps.virtual_memory()
        readings = [
            self._r("memory.total_bytes", int(vm.total), "bytes", SRC, kind=MetricKind.STATIC),
            self._r("memory.used_bytes", int(vm.total - vm.available), "bytes", SRC, kind=MetricKind.DERIVED),
            self._r("memory.available_bytes", int(vm.available), "bytes", SRC),
            self._r("memory.usage_percent", float(vm.percent), "percent", SRC),
        ]
        try:
            sw = self._ps.swap_memory()
            readings += [
                self._r("memory.swap_total_bytes", int(sw.total), "bytes", "psutil (page file)"),
                self._r("memory.swap_used_bytes", int(sw.used), "bytes", "psutil (page file)"),
                self._r("memory.swap_percent", float(sw.percent), "percent", "psutil (page file)"),
            ]
        except (OSError, RuntimeError) as exc:
            readings.append(self._na("memory.swap_percent", "percent", "psutil (page file)", str(exc)))
        return readings
