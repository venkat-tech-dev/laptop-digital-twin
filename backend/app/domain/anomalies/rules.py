"""V1 anomaly detection: explicit threshold rules with duration and hysteresis."""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.domain.anomalies.models import Severity
from app.domain.components.models import Component

_OPS: dict[str, Callable[[object, object], bool]] = {
    ">=": operator.ge,  # type: ignore[dict-item]
    "<=": operator.le,  # type: ignore[dict-item]
    "==": operator.eq,
    "!=": operator.ne,
}

Guard = Callable[[Mapping[str, Component]], bool]


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    rule_id: str
    metric: str
    op: str
    threshold: float | str
    duration_s: float
    severity: Severity
    title: str
    message: str  # format fields: value, threshold, unit, where, duration
    clear_threshold: float | None = None  # hysteresis: must cross this to resolve
    guard: Guard | None = None
    unlabelled_only: bool = False
    # Dynamic rules derive the threshold from the series' observed maximum (fan "at maximum").
    dynamic_fraction_of_max: float | None = None

    def breached(self, value: float | str) -> bool:
        return _OPS[self.op](value, self.threshold)

    def cleared(self, value: float | str) -> bool:
        if self.clear_threshold is None or not isinstance(value, (int, float)):
            return not self.breached(value)
        if self.op == ">=":
            return value < self.clear_threshold
        if self.op == "<=":
            return value > self.clear_threshold
        return not self.breached(value)


def _discharging(components: Mapping[str, Component]) -> bool:
    battery = components.get("battery")
    return battery is not None and battery.current_state == "discharging"


S = Severity

DEFAULT_RULES: tuple[ThresholdRule, ...] = (
    ThresholdRule(
        "cpu_temp_high",
        "cpu.temperature_c",
        ">=",
        90.0,
        10,
        S.WARNING,
        "CPU temperature high",
        "CPU package at {value:.1f} °C (>= {threshold:.0f} °C for {duration:.0f} s)",
        85.0,
    ),
    ThresholdRule(
        "cpu_temp_critical",
        "cpu.temperature_c",
        ">=",
        98.0,
        5,
        S.CRITICAL,
        "CPU temperature critical",
        "CPU package at {value:.1f} °C (>= {threshold:.0f} °C)",
        93.0,
    ),
    ThresholdRule(
        "thermal_zone_hot",
        "thermal.zone_temperature_c",
        ">=",
        90.0,
        10,
        S.WARNING,
        "Thermal zone hot",
        "ACPI thermal zone{where} at {value:.1f} °C (>= {threshold:.0f} °C for {duration:.0f} s)",
        85.0,
    ),
    ThresholdRule(
        "thermal_zone_critical",
        "thermal.zone_temperature_c",
        ">=",
        98.0,
        5,
        S.CRITICAL,
        "Thermal zone critical",
        "ACPI thermal zone{where} at {value:.1f} °C",
        93.0,
    ),
    ThresholdRule(
        "thermal_throttling",
        "thermal.passive_limit_percent",
        "<=",
        99.0,
        5,
        S.WARNING,
        "Thermal throttling",
        "Firmware passive cooling limits performance to {value:.0f}%{where}",
        100.0,
    ),
    ThresholdRule(
        "gpu_temp_high",
        "gpu.temperature_c",
        ">=",
        87.0,
        10,
        S.WARNING,
        "GPU temperature high",
        "GPU at {value:.1f} °C (>= {threshold:.0f} °C for {duration:.0f} s)",
        82.0,
    ),
    ThresholdRule(
        "memory_pressure",
        "memory.usage_percent",
        ">=",
        92.0,
        60,
        S.WARNING,
        "Memory pressure",
        "RAM utilization {value:.0f}% for over {duration:.0f} s (>= {threshold:.0f}%)",
        88.0,
    ),
    ThresholdRule(
        "memory_exhaustion",
        "memory.usage_percent",
        ">=",
        98.0,
        30,
        S.CRITICAL,
        "Memory nearly exhausted",
        "RAM utilization {value:.0f}% for over {duration:.0f} s",
        95.0,
    ),
    ThresholdRule(
        "disk_space_low",
        "disk.usage_percent",
        ">=",
        90.0,
        0,
        S.WARNING,
        "Disk space low",
        "Volume{where} is {value:.1f}% full",
        88.0,
    ),
    ThresholdRule(
        "disk_space_critical",
        "disk.usage_percent",
        ">=",
        95.0,
        0,
        S.CRITICAL,
        "Disk space critically low",
        "Volume{where} is {value:.1f}% full",
        93.0,
    ),
    ThresholdRule(
        "disk_latency_high",
        "disk.avg_read_latency_ms",
        ">=",
        50.0,
        15,
        S.WARNING,
        "High disk latency",
        "Average read latency{where} {value:.0f} ms for {duration:.0f} s",
        30.0,
    ),
    ThresholdRule(
        "disk_write_latency_high",
        "disk.avg_write_latency_ms",
        ">=",
        50.0,
        15,
        S.WARNING,
        "High disk latency",
        "Average write latency{where} {value:.0f} ms for {duration:.0f} s",
        30.0,
    ),
    ThresholdRule(
        "disk_health_warning",
        "disk.health_status",
        "!=",
        "Healthy",
        0,
        S.CRITICAL,
        "Drive health",
        "Windows reports drive{where} health '{value}'",
    ),
    ThresholdRule(
        "battery_health_degraded",
        "battery.health_percent",
        "<=",
        80.0,
        0,
        S.WARNING,
        "Battery degradation",
        "Full-charge capacity is {value:.1f}% of design capacity",
        81.0,
    ),
    ThresholdRule(
        "battery_low",
        "battery.charge_percent",
        "<=",
        15.0,
        0,
        S.WARNING,
        "Battery low",
        "Battery at {value:.0f}% and discharging",
        18.0,
        guard=_discharging,
    ),
    ThresholdRule(
        "battery_critical",
        "battery.charge_percent",
        "<=",
        7.0,
        0,
        S.CRITICAL,
        "Battery critical",
        "Battery at {value:.0f}% and discharging",
        10.0,
        guard=_discharging,
    ),
    ThresholdRule(
        "cpu_saturation",
        "cpu.usage_percent",
        ">=",
        95.0,
        120,
        S.INFO,
        "Sustained CPU saturation",
        "CPU utilization {value:.0f}% for over {duration:.0f} s",
        85.0,
    ),
    ThresholdRule(
        "network_errors",
        "network.errors_per_sec",
        ">=",
        10.0,
        30,
        S.WARNING,
        "Network errors",
        "{value:.0f} inbound errors/s for {duration:.0f} s",
        2.0,
        unlabelled_only=True,
    ),
    ThresholdRule(
        "fan_max_prolonged",
        "fan.speed_rpm",
        ">=",
        0.0,
        120,
        S.WARNING,
        "Fan at maximum",
        "Fan{where} at {value:.0f} rpm (>= 95% of observed maximum) for over {duration:.0f} s",
        dynamic_fraction_of_max=0.95,
    ),
)
