"""Telemetry domain model: a reading held by the twin, plus freshness evaluation."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

MetricValue = float | int | bool | str | None


class Quality(StrEnum):
    GOOD = "GOOD"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


def metric_key(metric: str, labels: Mapping[str, str] | None) -> str:
    """Stable identity of a time series: ``metric{k=v,...}`` with sorted labels."""
    if not labels:
        return metric
    return metric + "{" + ",".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}"


@dataclass(slots=True)
class MetricReading:
    key: str
    metric: str
    component_id: str
    value: MetricValue
    unit: str
    timestamp: datetime
    source: str
    quality: Quality
    available: bool
    kind: str
    reason: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    interval_s: float | None = None  # collection interval reported by the agent (schema >= 1.2)

    @property
    def numeric(self) -> float | None:
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            return None
        return float(self.value) if self.available else None

    def effective_quality(self, now: datetime, degraded_after_s: float, stale_after_s: float) -> Quality:
        """GOOD readings age into DEGRADED/STALE so old data is never shown as live."""
        if self.quality is not Quality.GOOD:
            return self.quality
        age = (now - self.timestamp).total_seconds()
        if age > stale_after_s:
            return Quality.STALE
        if age > degraded_after_s:
            return Quality.DEGRADED
        return Quality.GOOD

    def to_dict(
        self, now: datetime | None = None, degraded_after_s: float = 3.0, stale_after_s: float = 10.0
    ) -> dict[str, Any]:
        quality = self.effective_quality(now, degraded_after_s, stale_after_s) if now else self.quality
        return {
            "key": self.key,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "quality": quality.value,
            "availability": "available" if self.available else "unavailable",
            "kind": self.kind,
            "reason": self.reason,
            "labels": self.labels,
        }


class MetricWindow:
    """Bounded in-memory time window per series, used for sustained-condition rules and baselines."""

    def __init__(self, horizon_s: float = 900.0, max_points: int = 1800) -> None:
        self._horizon = horizon_s
        self._series: dict[str, deque[tuple[float, float]]] = {}
        self._max_points = max_points

    def add(self, key: str, ts: float, value: float) -> None:
        series = self._series.get(key)
        if series is None:
            series = self._series[key] = deque(maxlen=self._max_points)
        if series and ts <= series[-1][0]:
            return  # out-of-order / duplicate (e.g. replayed batch): ignore for windows
        series.append((ts, value))
        while series and ts - series[0][0] > self._horizon:
            series.popleft()

    def values_since(self, key: str, since_ts: float) -> list[tuple[float, float]]:
        return [p for p in self._series.get(key, ()) if p[0] >= since_ts]

    def keys(self) -> Iterable[str]:
        return self._series.keys()

    def latest(self, key: str) -> tuple[float, float] | None:
        series = self._series.get(key)
        return series[-1] if series else None
