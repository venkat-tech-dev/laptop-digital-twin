from __future__ import annotations

from collections.abc import Callable
from types import ModuleType

import psutil as _psutil

from app.contracts import MetricKind
from app.errors import TelemetryError
from app.platform.pdh import CounterReader, PdhCounterSet
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC_PSUTIL = "psutil (Win32 GetSystemTimes)"
SRC_PDH = "Windows Performance Counter (Processor Information)"

_PDH_PATHS = {
    "performance": r"\Processor Information(_Total)\% Processor Performance",
    # Threads ready to run but waiting for a processor: Windows' closest "load average" equivalent.
    "queue": r"\System\Processor Queue Length",
}
SRC_QUEUE = "Windows Performance Counter (System / Processor Queue Length)"
SRC_NOMINAL = "WMI Win32_Processor.MaxClockSpeed (nominal clock)"


class CPUProvider(TelemetryProvider):
    """CPU utilisation (overall + per logical processor) and effective clock frequency.

    Effective frequency = nominal frequency x ``% Processor Performance`` / 100, which is the same
    calculation Windows Task Manager uses (``% Processor Performance`` exceeds 100 under turbo).
    ``psutil.cpu_freq()`` is *not* used for the live value because on Windows it reports a static
    nominal clock.
    """

    name = "cpu"
    component = "cpu"

    def __init__(
        self,
        interval_ms: int,
        nominal_mhz: float | None,
        psutil: ModuleType = _psutil,
        counter_factory: Callable[[dict[str, str]], CounterReader] = PdhCounterSet,
    ) -> None:
        super().__init__(interval_ms)
        self._ps = psutil
        self._nominal = float(nominal_mhz) if nominal_mhz else None
        self._counters: CounterReader | None = None
        self._counter_error: str | None = None
        try:
            self._counters = counter_factory(_PDH_PATHS)
        except TelemetryError as exc:
            self._counter_error = exc.detail
        # Prime psutil's internal delta so the first real call returns a measured interval.
        self._ps.cpu_percent(interval=None)
        self._ps.cpu_percent(interval=None, percpu=True)

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [
            MetricSpec("cpu.usage_percent", "percent", SRC_PSUTIL),
            MetricSpec("cpu.frequency_mhz", "MHz", SRC_PDH),
        ]

    def collect(self) -> list[Reading]:
        readings = [self._r("cpu.usage_percent", self._ps.cpu_percent(interval=None), "percent", SRC_PSUTIL)]
        for idx, pct in enumerate(self._ps.cpu_percent(interval=None, percpu=True)):
            readings.append(
                self._r("cpu.core_usage_percent", pct, "percent", SRC_PSUTIL, labels={"core": str(idx)})
            )
        readings.extend(self._frequency())
        return readings

    def _frequency(self) -> list[Reading]:
        out: list[Reading] = []
        if self._nominal is not None:
            out.append(
                self._r(
                    "cpu.nominal_frequency_mhz", self._nominal, "MHz", SRC_NOMINAL, kind=MetricKind.STATIC
                )
            )
        else:
            out.append(
                self._na("cpu.nominal_frequency_mhz", "MHz", SRC_NOMINAL, "Processor did not report a clock")
            )
        if self._counters is None:
            reason = self._counter_error or "Performance counters unavailable"
            return [
                *out,
                self._na("cpu.frequency_mhz", "MHz", SRC_PDH, reason),
                self._na("cpu.performance_percent", "percent_of_nominal", SRC_PDH, reason),
            ]
        try:
            values = self._counters.collect()
        except TelemetryError as exc:
            return [*out, Reading.failed("cpu.frequency_mhz", self.component, "MHz", SRC_PDH, exc.detail)]
        queue = values.get("queue")
        if isinstance(queue, float):
            out.append(self._r("cpu.processor_queue_length", int(queue), "count", SRC_QUEUE))
        perf = values.get("performance")
        if not isinstance(perf, float):
            warming = "Counter warming up (needs two samples)"
            return [
                *out,
                self._na("cpu.frequency_mhz", "MHz", SRC_PDH, warming),
                self._na("cpu.performance_percent", "percent_of_nominal", SRC_PDH, warming),
            ]
        out.append(self._r("cpu.performance_percent", perf, "percent_of_nominal", SRC_PDH))
        if self._nominal is None:
            out.append(self._na("cpu.frequency_mhz", "MHz", SRC_PDH, "Nominal clock unknown"))
        else:
            out.append(
                self._r(
                    "cpu.frequency_mhz",
                    self._nominal * perf / 100.0,
                    "MHz",
                    f"{SRC_PDH} x nominal clock",
                    kind=MetricKind.DERIVED,
                )
            )
        return out

    def close(self) -> None:
        if self._counters is not None:
            self._counters.close()
