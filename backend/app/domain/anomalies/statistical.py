"""V2 anomaly detection: per-series EWMA baselines and z-scores.

A point is anomalous when it deviates from the exponentially weighted baseline by at least
``z_threshold`` standard deviations *and* by a minimum absolute amount (so a perfectly flat series
does not alarm on trivial changes). An anomaly opens after ``confirm`` consecutive anomalous points
and resolves after ``resolve`` consecutive normal points.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StatisticalSpec:
    metric: str
    title: str
    min_std: float
    min_delta: float
    unlabelled_only: bool = False
    direction: str = "up"  # "up" | "both"


DEFAULT_SPECS: tuple[StatisticalSpec, ...] = (
    StatisticalSpec("cpu.usage_percent", "Unexpected CPU spike", 5.0, 35.0),
    StatisticalSpec("memory.usage_percent", "Unusual memory growth", 1.5, 10.0),
    StatisticalSpec("cpu.temperature_c", "Unusual CPU temperature rise", 1.5, 10.0),
    StatisticalSpec("thermal.zone_temperature_c", "Unusual temperature rise", 1.5, 10.0),
    StatisticalSpec("gpu.usage_percent", "Unexpected GPU spike", 5.0, 40.0),
    StatisticalSpec(
        "network.rx_bytes_per_sec", "Unusual network traffic (download)", 50_000, 5_000_000, True
    ),
    StatisticalSpec("network.tx_bytes_per_sec", "Unusual network traffic (upload)", 50_000, 2_000_000, True),
    StatisticalSpec("disk.write_bytes_per_sec", "Unusual disk write activity", 1_000_000, 80_000_000, True),
)


class EwmaBaseline:
    def __init__(self, alpha: float) -> None:
        self.alpha = alpha
        self.mean = 0.0
        self.var = 0.0
        self.n = 0

    def zscore(self, x: float, min_std: float) -> float:
        std = max(math.sqrt(self.var), min_std)
        return (x - self.mean) / std

    def update(self, x: float) -> None:
        if self.n == 0:
            self.mean, self.var = x, 0.0
        else:
            diff = x - self.mean
            incr = self.alpha * diff
            self.mean += incr
            self.var = (1 - self.alpha) * (self.var + diff * incr)
        self.n += 1


@dataclass
class SeriesState:
    baseline: EwmaBaseline
    anomalous_run: int = 0
    normal_run: int = 0
    open: bool = False


@dataclass(frozen=True, slots=True)
class Verdict:
    opened: bool
    resolved: bool
    zscore: float
    mean: float
    std: float


class StatisticalDetector:
    def __init__(
        self,
        alpha: float = 0.01,
        warmup: int = 120,
        z_threshold: float = 4.0,
        confirm: int = 5,
        resolve: int = 10,
    ) -> None:
        self.alpha = alpha
        self.warmup = warmup
        self.z_threshold = z_threshold
        self.confirm = confirm
        self.resolve_after = resolve
        self._series: dict[str, SeriesState] = {}

    def state(self, key: str) -> SeriesState:
        st = self._series.get(key)
        if st is None:
            st = self._series[key] = SeriesState(EwmaBaseline(self.alpha))
        return st

    def seed(self, key: str, values: list[float]) -> None:
        st = self.state(key)
        for v in values:
            st.baseline.update(v)

    def observe(self, key: str, x: float, spec: StatisticalSpec) -> Verdict:
        st = self.state(key)
        b = st.baseline
        z = b.zscore(x, spec.min_std) if b.n else 0.0
        std = max(math.sqrt(b.var), spec.min_std)
        delta = x - b.mean
        anomalous = (
            b.n >= self.warmup
            and (z >= self.z_threshold if spec.direction == "up" else abs(z) >= self.z_threshold)
            and (delta >= spec.min_delta if spec.direction == "up" else abs(delta) >= spec.min_delta)
        )
        mean = b.mean
        b.update(x)
        opened = resolved = False
        if anomalous:
            st.anomalous_run += 1
            st.normal_run = 0
            if not st.open and st.anomalous_run >= self.confirm:
                st.open = opened = True
        else:
            st.normal_run += 1
            st.anomalous_run = 0
            if st.open and st.normal_run >= self.resolve_after:
                st.open = False
                resolved = True
        return Verdict(opened, resolved, z, mean, std)
