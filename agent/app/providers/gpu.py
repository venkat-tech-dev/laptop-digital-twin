from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable

from app.contracts import MetricKind
from app.errors import TelemetryError
from app.platform.dxgi import DxgiAdapter
from app.platform.lhm import GPU_KINDS, LhmClient
from app.platform.pdh import CounterReader, PdhCounterSet
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC_ENGINE = "Windows Performance Counter (GPU Engine)"
SRC_MEM = "Windows Performance Counter (GPU Adapter Memory)"
SRC_DXGI = "DXGI adapter description"
SRC_LHM = "LibreHardwareMonitor"

_ENGINE_RE = re.compile(
    r"pid_(?P<pid>\d+)_luid_(?P<luid>0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+)_.*engtype_(?P<eng>.*)$"
)
_MEM_RE = re.compile(r"luid_(?P<luid>0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+)_phys")

_PDH_PATHS = {
    "engine": r"\GPU Engine(*)\Utilization Percentage",
    "dedicated": r"\GPU Adapter Memory(*)\Dedicated Usage",
    "shared": r"\GPU Adapter Memory(*)\Shared Usage",
}


def aggregate_engines(
    raw: dict[str, float],
) -> tuple[dict[str, dict[str, float]], dict[int, float]]:
    """Return (per-luid engine-type utilisation, per-pid max engine utilisation).

    Utilisation of an engine type is the sum over processes; adapter utilisation is the busiest
    engine type. This mirrors Windows Task Manager's GPU column.
    """
    per_adapter: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    per_pid_engine: dict[tuple[int, str], float] = defaultdict(float)
    for instance, value in raw.items():
        match = _ENGINE_RE.search(instance)
        if not match:
            continue
        luid = match["luid"].upper().replace("0X", "0x")
        engine = match["eng"] or "Other"
        per_adapter[luid][engine] += value
        per_pid_engine[(int(match["pid"]), f"{luid}/{engine}")] += value
    per_pid: dict[int, float] = defaultdict(float)
    for (pid, _), value in per_pid_engine.items():
        per_pid[pid] = max(per_pid[pid], value)
    return {k: dict(v) for k, v in per_adapter.items()}, dict(per_pid)


class GPUProvider(TelemetryProvider):
    """GPU utilisation and memory per DXGI adapter, plus LHM sensors (temperature, clock, power)."""

    name = "gpu"
    component = "gpu"

    def __init__(
        self,
        interval_ms: int,
        adapters: list[DxgiAdapter],
        lhm: LhmClient | None,
        counter_factory: Callable[[dict[str, str]], CounterReader] = PdhCounterSet,
    ) -> None:
        super().__init__(interval_ms)
        self._adapters = [a for a in adapters if not a.is_software]
        self._lhm = lhm
        self._counters: CounterReader | None = None
        self._counter_error: str | None = None
        self.per_pid_usage: dict[int, float] = {}
        if self._adapters:
            try:
                self._counters = counter_factory(_PDH_PATHS)
            except TelemetryError as exc:
                self._counter_error = exc.detail

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("gpu.usage_percent", "percent", SRC_ENGINE)]

    def collect(self) -> list[Reading]:
        if not self._adapters:
            return [self._na("gpu.usage_percent", "percent", SRC_DXGI, "No hardware GPU adapter detected")]
        values: dict[str, float | dict[str, float] | None] = {}
        counter_reason = self._counter_error
        if self._counters is not None:
            try:
                values = self._counters.collect()
            except TelemetryError as exc:
                counter_reason = exc.detail
        engine_raw = values.get("engine")
        engines, self.per_pid_usage = aggregate_engines(engine_raw if isinstance(engine_raw, dict) else {})
        readings: list[Reading] = []
        for adapter in self._adapters:
            readings += self._adapter_readings(adapter, engines, values, counter_reason)
        readings += self._lhm_readings()
        return readings

    def _adapter_readings(
        self,
        adapter: DxgiAdapter,
        engines: dict[str, dict[str, float]],
        values: dict[str, float | dict[str, float] | None],
        counter_reason: str | None,
    ) -> list[Reading]:
        labels = {"adapter": adapter.name, "luid": adapter.luid}
        out = [
            self._r(
                "gpu.dedicated_memory_total_bytes",
                adapter.dedicated_video_memory,
                "bytes",
                SRC_DXGI,
                kind=MetricKind.STATIC,
                labels=labels,
            ),
            self._r(
                "gpu.shared_memory_total_bytes",
                adapter.shared_system_memory,
                "bytes",
                SRC_DXGI,
                kind=MetricKind.STATIC,
                labels=labels,
            ),
        ]
        if counter_reason is not None and not values:
            out.append(self._na("gpu.usage_percent", "percent", SRC_ENGINE, counter_reason, labels))
            return out
        adapter_engines = engines.get(adapter.luid, {})
        if isinstance(values.get("engine"), dict):
            usage = min(100.0, max(adapter_engines.values(), default=0.0))
            out.append(self._r("gpu.usage_percent", usage, "percent", SRC_ENGINE, labels=labels))
            for engine, value in sorted(adapter_engines.items()):
                out.append(
                    self._r(
                        "gpu.engine_usage_percent",
                        min(100.0, value),
                        "percent",
                        SRC_ENGINE,
                        labels={**labels, "engine": engine},
                    )
                )
        else:
            out.append(self._na("gpu.usage_percent", "percent", SRC_ENGINE, "Counter warming up", labels))
        for key, metric in (
            ("dedicated", "gpu.dedicated_memory_used_bytes"),
            ("shared", "gpu.shared_memory_used_bytes"),
        ):
            mem = values.get(key)
            used = None
            if isinstance(mem, dict):
                used = next(
                    (
                        v
                        for inst, v in mem.items()
                        if (m := _MEM_RE.search(inst))
                        and m["luid"].upper().replace("0X", "0x") == adapter.luid
                    ),
                    None,
                )
            if used is None:
                out.append(self._na(metric, "bytes", SRC_MEM, "Adapter memory counter not reported", labels))
            else:
                out.append(self._r(metric, int(used), "bytes", SRC_MEM, labels=labels))
        return out

    def _lhm_readings(self) -> list[Reading]:
        specs = (
            ("gpu.temperature_c", "celsius", "Temperature", ("GPU Core", "GPU Hot Spot", "GPU Package")),
            ("gpu.core_clock_mhz", "MHz", "Clock", ("GPU Core",)),
            ("gpu.power_w", "W", "Power", ("GPU Package", "GPU Power", "GPU Core")),
        )
        if self._lhm is None:
            reason = "Hardware sensor provider disabled (HARDWARE_SENSOR_PROVIDER)"
            return [self._na(m, u, SRC_LHM, reason) for m, u, _, _ in specs]
        out: list[Reading] = []
        for metric, unit, sensor_type, names in specs:
            try:
                sensor = self._lhm.find(GPU_KINDS, sensor_type, names)
            except TelemetryError as exc:
                out.append(self._na(metric, unit, SRC_LHM, exc.detail))
                continue
            if sensor is None:
                out.append(
                    self._na(
                        metric, unit, SRC_LHM, "GPU does not expose this sensor via LibreHardwareMonitor"
                    )
                )
            else:
                out.append(self._r(metric, sensor.value, unit, SRC_LHM, labels={"adapter": sensor.hardware}))
        return out

    def close(self) -> None:
        if self._counters is not None:
            self._counters.close()
