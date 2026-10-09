"""Forecast targets: which metrics are forecast, why, and with which defaults.

Only metrics with a clear operational purpose are forecast (audit, docs/predictive-analytics.md):

    disk          C: usage -> capacity exhaustion          days      robust trend on 1-hour buckets
    memory        RAM utilization -> memory exhaustion     minutes   trend, volatility-gated
    battery       charge while discharging -> critical     minutes   trend on the current discharge
    temperature   CPU-area temperature -> thermal warning  minutes   conservative trend
    cpu           sustained CPU pressure (expected range)  minutes   trend forecast only

Not forecast: gateway latency / packet loss (spiky, 30 s sampling, 0/100 % loss bursts - no stable
trend), GPU temperature (not reported by this agent), battery wear (cycle-based, kept in the legacy
analytics endpoint). Every number below is a default that ``PredictionPolicy`` can override.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from typing import Any


@dataclass(frozen=True)
class Target:
    target_id: str
    title: str
    field: str  # digital-twin field (provenance names the concrete series)
    unit: str
    # resource_exhaustion | thermal_escalation | battery_depletion | trend_forecast
    prediction_type: str
    direction: str  # "up": rising is bad; "down": falling is bad (battery)
    thresholds: tuple[float, ...]  # first = primary target (warning), last = critical
    bucket_s: int  # resampling interval
    aggregation: str  # mean | last | max
    window_s: int  # history used for fitting
    min_history_s: int  # minimum usable span
    min_points: int  # minimum buckets
    horizon_s: int  # maximum forecast horizon
    trend_horizon_s: int  # horizon of the expected-range (trend) forecast
    update_interval_s: int  # how often it is recalculated
    stale_after_s: int  # newest sample older than this: STALE_DATA
    min_slope_per_h: float  # practical minimum trend (units / hour) to call it a trend
    severity_bands_s: tuple[int, int, int, int]  # time-to-threshold for INFO/LOW/MEDIUM/HIGH boundaries
    max_regime_jump: float  # bucket-to-bucket jump treated as a regime change (cleanup, unplug ...)
    plausible: tuple[float, float]
    impact: str
    enabled: bool = True
    # compared in backtests; live forecasting uses ``trend`` (when its gates pass) or ``level_model``
    models: tuple[str, ...] = ("naive", "ewma", "trend", "holt")
    crossing: bool = True  # False: expected-range forecast only (no time-to-threshold claims)
    extra: dict[str, Any] = dc_field(default_factory=dict)
    level_model: str = "ewma"  # forecast when there is no meaningful trend (chosen by backtest)
    max_extrapolation: float = 10.0  # crossing only within this multiple of the usable history span
    saturates: bool = False  # physical levelling-off (thermal, memory): no crossing on a decelerating trend
    rearm: float = 3.0  # after a crossing, it counts again only after falling this far below
    trend_range: bool = True  # False: the expected range always uses ``level_model`` (trend only in words)
    exceedance: bool = False  # report the empirical probability of exceeding the threshold in the horizon

    def merged(self, changes: dict[str, Any]) -> Target:
        allowed = {
            "thresholds",
            "window_s",
            "min_history_s",
            "min_points",
            "horizon_s",
            "trend_horizon_s",
            "update_interval_s",
            "stale_after_s",
            "min_slope_per_h",
            "severity_bands_s",
            "enabled",
        }
        clean = {k: (tuple(v) if isinstance(v, list) else v) for k, v in changes.items() if k in allowed}
        return replace(self, **clean)  # type: ignore[arg-type]

    def public(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "title": self.title,
            "field": self.field,
            "unit": self.unit,
            "prediction_type": self.prediction_type,
            "direction": self.direction,
            "thresholds": list(self.thresholds),
            "bucket_s": self.bucket_s,
            "aggregation": self.aggregation,
            "window_s": self.window_s,
            "min_history_s": self.min_history_s,
            "min_points": self.min_points,
            "horizon_s": self.horizon_s,
            "trend_horizon_s": self.trend_horizon_s,
            "update_interval_s": self.update_interval_s,
            "stale_after_s": self.stale_after_s,
            "min_slope_per_h": self.min_slope_per_h,
            "severity_bands_s": list(self.severity_bands_s),
            "crossing": self.crossing,
            "enabled": self.enabled,
            "impact": self.impact,
        }


MIN, HOUR, DAY = 60, 3600, 86400

TARGETS: tuple[Target, ...] = (
    Target(
        "disk",
        "System drive capacity",
        "performance.disk.usage_percent",
        "%",
        "resource_exhaustion",
        "up",
        (90.0, 95.0),
        bucket_s=HOUR,  # disk changes slowly: hourly buckets keep 14 days at 336 points
        aggregation="last",
        window_s=14 * DAY,
        min_history_s=DAY,
        min_points=24,
        horizon_s=365 * DAY,
        trend_horizon_s=7 * DAY,
        update_interval_s=15 * MIN,
        stale_after_s=30 * MIN,
        min_slope_per_h=0.1 / 24,
        severity_bands_s=(180 * DAY, 30 * DAY, 7 * DAY, DAY),
        max_regime_jump=3.0,
        plausible=(0.0, 100.0),
        impact="a full system drive stops updates, logs and saves",
        level_model="naive",
        max_extrapolation=10.0,
        rearm=1.0,
    ),
    Target(
        "memory",
        "Memory utilization",
        "performance.memory.usage_percent",
        "%",
        "resource_exhaustion",
        "up",
        (90.0, 95.0),
        bucket_s=MIN,
        aggregation="mean",
        window_s=HOUR,
        min_history_s=20 * MIN,
        min_points=15,
        horizon_s=2 * HOUR,
        trend_horizon_s=15 * MIN,
        update_interval_s=MIN,
        stale_after_s=2 * MIN,
        min_slope_per_h=6.0,
        severity_bands_s=(2 * HOUR, HOUR, 30 * MIN, 10 * MIN),
        max_regime_jump=25.0,
        plausible=(0.0, 100.0),
        impact="memory exhaustion causes paging, slow-downs and application failures",
        level_model="ewma",
        max_extrapolation=2.0,
        saturates=True,
        rearm=3.0,
        exceedance=True,
    ),
    Target(
        "battery",
        "Battery charge",
        "battery.charge_percent",
        "%",
        "battery_depletion",
        "down",
        (10.0, 5.0),
        bucket_s=MIN,
        aggregation="last",
        window_s=30 * MIN,
        min_history_s=8 * MIN,
        min_points=8,
        horizon_s=2 * HOUR,
        trend_horizon_s=15 * MIN,
        update_interval_s=MIN,
        stale_after_s=2 * MIN,
        min_slope_per_h=2.0,
        severity_bands_s=(2 * HOUR, HOUR, 30 * MIN, 10 * MIN),
        max_regime_jump=8.0,
        plausible=(0.0, 100.0),
        impact="the device shuts down at a critical charge (unsaved work)",
        level_model="naive",
        max_extrapolation=3.0,
        rearm=2.0,
    ),
    Target(
        "temperature",
        "CPU-area temperature",
        "thermal.temperature_c",
        "°C",
        "thermal_escalation",
        "up",
        (90.0, 98.0),
        bucket_s=30,
        aggregation="mean",
        window_s=15 * MIN,
        min_history_s=6 * MIN,
        min_points=12,
        horizon_s=30 * MIN,
        trend_horizon_s=5 * MIN,
        update_interval_s=30,
        stale_after_s=MIN,
        min_slope_per_h=12.0,
        severity_bands_s=(30 * MIN, 15 * MIN, 8 * MIN, 3 * MIN),
        max_regime_jump=15.0,
        plausible=(-20.0, 130.0),
        impact="sustained high temperature causes throttling and shutdown",
        level_model="ewma",
        max_extrapolation=1.0,
        saturates=True,
        rearm=3.0,
        exceedance=True,
    ),
    Target(
        "cpu",
        "CPU utilization",
        "performance.cpu.usage_percent",
        "%",
        "trend_forecast",
        "up",
        (90.0,),
        bucket_s=MIN,
        aggregation="mean",
        window_s=30 * MIN,
        min_history_s=15 * MIN,
        min_points=12,
        horizon_s=HOUR,
        trend_horizon_s=15 * MIN,
        update_interval_s=MIN,
        stale_after_s=2 * MIN,
        min_slope_per_h=20.0,
        severity_bands_s=(HOUR, 30 * MIN, 15 * MIN, 5 * MIN),
        max_regime_jump=60.0,
        plausible=(0.0, 100.0),
        impact="sustained CPU saturation slows every application",
        crossing=False,
        level_model="ewma",
        max_extrapolation=1.0,
        rearm=5.0,
        trend_range=False,
        exceedance=False,  # evaluated: precision 0 at a 0.9 % base rate on real data
    ),
)

TARGETS_BY_ID = {t.target_id: t for t in TARGETS}
