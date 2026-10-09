"""What-if workload simulation.

SIMULATION ONLY. Nothing here touches the physical laptop. The model starts from the *current live
state* (baseline) and applies a transparent, deterministic first-order model:

* CPU package power  P = P_idle + (P_sustained - P_idle) * load      (power class from the CPU SKU)
* Steady temperature  T_ss = T_amb + R_th * P                        (R_th calibrated from the live state)
  or, if enough history exists, T_ss = a + b * load  (linear fit of temperature vs. CPU load)
* Temperature response T(t) = T_ss + (T0 - T_ss) * exp(-t / tau)
* Throttling when T_ss exceeds the junction limit: power capped so T_ss == limit.
* Memory: used + workload demand; the excess beyond physical RAM is paged.
* Battery: system power = baseline system power + delta package power; runtime = remaining Wh / P.
* Environment: ambient temperature shifts the thermal curve 1:1; the thermal profile (quiet / balanced /
  performance) scales the sustained power limit and the effective cooling (thermal resistance).
* Fan: estimated duty from a linear fan curve between the profile's start temperature and the junction
  limit (an estimate - this laptop does not expose fan RPM to Windows).
* Charging: on AC below 100 %, charge power = min(adapter - system power, max charge power), tapering
  above 80 % state of charge.
* Confidence intervals: temperature +/- 1.96 sigma, sigma = residual std of the calibrated fit (or a
  documented heuristic when not calibrated), growing with the thermal response; runtime +/- 15 % when
  system power was measured, +/- 30 % when estimated.

Every assumption is returned with the result. Outputs are estimates with an explicit confidence.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Scenario(StrEnum):
    CPU_INTENSIVE = "cpu_intensive"
    GPU_INTENSIVE = "gpu_intensive"
    RAM_INTENSIVE = "ram_intensive"
    GAMING = "gaming"
    AI_ML = "ai_ml"
    BATTERY_ONLY = "battery_only"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class WorkloadProfile:
    cpu_load: float  # 0..1 of all logical processors
    gpu_load: float  # 0..1
    ram_gb: float  # additional resident memory demanded by the workload
    on_battery: bool
    description: str


PROFILES: dict[Scenario, WorkloadProfile] = {
    Scenario.CPU_INTENSIVE: WorkloadProfile(
        1.0, 0.05, 1.0, False, "All cores saturated (compile / render / encode)"
    ),
    Scenario.GPU_INTENSIVE: WorkloadProfile(0.30, 1.0, 1.5, False, "GPU saturated (3D rendering, shaders)"),
    Scenario.RAM_INTENSIVE: WorkloadProfile(
        0.35, 0.05, 8.0, False, "Large in-memory dataset (8 GB working set)"
    ),
    Scenario.GAMING: WorkloadProfile(0.60, 0.95, 4.0, False, "Modern game: high GPU, moderate CPU, 4 GB RAM"),
    Scenario.AI_ML: WorkloadProfile(
        0.90, 0.80, 6.0, False, "Local model inference / training (CPU+GPU, 6 GB RAM)"
    ),
    Scenario.BATTERY_ONLY: WorkloadProfile(0.25, 0.15, 0.5, True, "Typical office use, unplugged"),
}

AMBIENT_C = 25.0
IDLE_PACKAGE_W = 2.5
ADAPTER_W = 65.0  # typical USB-C adapter for this class; assumption, not measured
MAX_CHARGE_W = 45.0


class ThermalProfile(StrEnum):
    QUIET = "quiet"
    BALANCED = "balanced"
    PERFORMANCE = "performance"


# (sustained power multiplier, thermal resistance multiplier, fan start temperature °C)
THERMAL_PROFILES: dict[ThermalProfile, tuple[float, float, float]] = {
    ThermalProfile.QUIET: (0.8, 1.15, 60.0),
    ThermalProfile.BALANCED: (1.0, 1.0, 55.0),
    ThermalProfile.PERFORMANCE: (1.2, 0.9, 50.0),
}


@dataclass(frozen=True, slots=True)
class Environment:
    ambient_c: float = AMBIENT_C
    thermal_profile: ThermalProfile = ThermalProfile.BALANCED
    adapter_w: float = ADAPTER_W


PLATFORM_W = 5.0  # display, memory, SSD, Wi-Fi etc. when not measurable
TAU_S = 60.0


@dataclass(frozen=True, slots=True)
class PowerClass:
    sustained_w: float
    tj_max_c: float
    basis: str


def power_class(cpu_model: str | None) -> PowerClass:
    """Sustained package power class inferred from the CPU SKU suffix (documented assumption)."""
    name = (cpu_model or "").upper()
    match = re.search(r"\d{4,5}([A-Z]{1,2})\b", name)
    suffix = match.group(1) if match else ""
    table = {"U": 15.0, "P": 28.0, "H": 45.0, "HS": 35.0, "HX": 55.0, "G": 15.0}
    sustained = table.get(suffix, table.get(suffix[:1], 25.0))
    tj = 100.0 if "INTEL" in name else 95.0
    return PowerClass(
        sustained, tj, f"'{suffix or '?'}'-suffix SKU -> ~{sustained:.0f} W sustained (vendor class)"
    )


@dataclass(slots=True)
class Baseline:
    cpu_usage: float | None
    temperature_c: float | None
    temperature_label: str | None
    memory_used_gb: float | None
    memory_total_gb: float | None
    gpu_usage: float | None
    battery_percent: float | None
    battery_remaining_wh: float | None
    battery_full_wh: float | None
    measured_system_power_w: float | None
    on_battery: bool
    cpu_model: str | None
    has_discrete_gpu: bool
    temp_vs_load_fit: tuple[float, float, float] | None = None  # (intercept, slope per load fraction, r2)
    temp_fit_sigma: float | None = None  # residual standard deviation of the fit (°C)


@dataclass(slots=True)
class SimulationResult:
    scenario: Scenario
    duration_s: int
    current: dict[str, Any]
    predicted: dict[str, Any]
    difference: dict[str, Any]
    assumptions: list[str]
    confidence: str
    confidence_score: float
    warnings: list[str]
    trajectory: list[dict[str, Any]] = field(default_factory=list)


def _taper(percent: float) -> float:
    """Charge power fraction: full below 80 %, linear taper to 10 % at 100 %."""
    return 1.0 if percent < 80 else max(0.1, (100.0 - percent) / 20.0)


def _round(v: float | None, nd: int = 1) -> float | None:
    return None if v is None else round(v, nd)


def simulate(
    baseline: Baseline,
    profile: WorkloadProfile,
    scenario: Scenario,
    duration_s: int,
    step_s: int = 10,
    env: Environment | None = None,
) -> SimulationResult:
    env = env or Environment()
    base_pc = power_class(baseline.cpu_model)
    power_mult, rth_mult, fan_start = THERMAL_PROFILES[env.thermal_profile]
    pc = PowerClass(base_pc.sustained_w * power_mult, base_pc.tj_max_c, base_pc.basis)
    ambient_shift = env.ambient_c - AMBIENT_C
    assumptions = [
        f"CPU power class: {pc.basis}; idle package ~{IDLE_PACKAGE_W} W; "
        f"junction limit {pc.tj_max_c:.0f} °C.",
        f"Ambient temperature {env.ambient_c:.0f} °C (baseline measured at an assumed "
        f"{AMBIENT_C:.0f} °C room; "
        f"curve shifted {ambient_shift:+.0f} °C); first-order thermal time constant {TAU_S:.0f} s.",
        f"Thermal profile '{env.thermal_profile.value}': sustained power x{power_mult:.2f} "
        f"({pc.sustained_w:.0f} W), cooling resistance x{rth_mult:.2f}, "
        f"fan curve starts at {fan_start:.0f} °C.",
        "Workload load levels are fixed for the whole duration (no burst/turbo phases modelled).",
    ]
    warnings: list[str] = []
    confidence_score = 0.3

    cur_load = (baseline.cpu_usage or 0.0) / 100.0
    gpu_share = 0.35 if not baseline.has_discrete_gpu else 0.0  # iGPU shares the CPU package budget

    def pkg_power(cpu_load: float, gpu_load: float) -> float:
        return IDLE_PACKAGE_W + (pc.sustained_w - IDLE_PACKAGE_W) * min(1.0, cpu_load + gpu_share * gpu_load)

    p_now = pkg_power(cur_load, (baseline.gpu_usage or 0.0) / 100.0)
    p_new = pkg_power(profile.cpu_load, profile.gpu_load)
    if baseline.has_discrete_gpu:
        assumptions.append(
            "Discrete GPU power not modelled for the package temperature (separate cooling path)."
        )

    # ----- thermal
    t0 = baseline.temperature_c
    t_ss: float | None = None
    throttled = False
    if t0 is None:
        warnings.append("No live temperature available: thermal prediction skipped.")
    elif baseline.temp_vs_load_fit is not None:
        a, b, r2 = baseline.temp_vs_load_fit
        load_eff = min(1.0, profile.cpu_load + gpu_share * profile.gpu_load) * power_mult
        rise = b * load_eff * rth_mult
        t_ss = a + rise + ambient_shift
        assumptions.append(
            f"Temperature model calibrated from live history: T = {a:.1f} + {b:.1f} x load (r² {r2:.2f})."
        )
        confidence_score += 0.3 * min(1.0, r2 * 2)
    else:
        r_th = max(1.5, min(6.0, (t0 - AMBIENT_C) / max(p_now, 1.0))) * rth_mult
        t_ss = env.ambient_c + r_th * p_new
        assumptions.append(
            f"Thermal resistance {r_th:.2f} °C/W derived from current temperature and estimated power."
        )
    sigma: float | None = None
    if t_ss is not None and t0 is not None:
        if baseline.temp_fit_sigma is not None and baseline.temp_vs_load_fit is not None:
            sigma = max(0.5, baseline.temp_fit_sigma)
            assumptions.append(
                f"Temperature interval from fit residuals: sigma {sigma:.1f} °C (95 % = +/-1.96 sigma)."
            )
        else:
            sigma = max(1.5, 0.12 * abs(t_ss - env.ambient_c))
            assumptions.append(
                "Temperature interval heuristic (uncalibrated): sigma = max(1.5, 12 % of rise) = "
                f"{sigma:.1f} °C."
            )
    if t_ss is not None and t_ss > pc.tj_max_c:
        throttled = True
        t_ss = pc.tj_max_c - 2.0
        warnings.append(
            "Thermal throttling likely: the firmware would reduce clocks to stay under the junction limit."
        )
    if baseline.temperature_label and "ACPI" in baseline.temperature_label:
        assumptions.append(
            f"Temperature baseline is the {baseline.temperature_label}, not the CPU package sensor."
        )

    # ----- memory
    mem_total = baseline.memory_total_gb
    mem_used_new = None
    paging_gb = 0.0
    if baseline.memory_used_gb is not None and mem_total:
        mem_used_new = baseline.memory_used_gb + profile.ram_gb
        if mem_used_new > mem_total * 0.97:
            paging_gb = mem_used_new - mem_total * 0.97
            mem_used_new = mem_total * 0.97
            warnings.append(
                f"Memory demand exceeds physical RAM: ~{paging_gb:.1f} GB would be paged to disk."
            )

    # ----- battery
    on_batt = profile.on_battery or baseline.on_battery
    sys_power_new: float | None = None
    runtime_h: float | None = None
    runtime_band: tuple[float, float] | None = None
    battery_end: float | None = None
    charge_w: float | None = None
    if not on_batt and baseline.battery_percent is not None and baseline.battery_full_wh:
        base_sys = baseline.measured_system_power_w or (p_now + PLATFORM_W)
        sys_on_ac = max(1.0, base_sys + (p_new - p_now))
        headroom = max(0.0, env.adapter_w - sys_on_ac)
        charge_w = min(MAX_CHARGE_W, headroom)
        assumptions.append(
            f"Charging: {env.adapter_w:.0f} W adapter (assumed) minus ~{sys_on_ac:.0f} W system load leaves "
            f"{charge_w:.0f} W for charging (max {MAX_CHARGE_W:.0f} W, tapering above 80 %)."
        )
    if on_batt:
        if baseline.battery_remaining_wh is None or baseline.battery_full_wh is None:
            warnings.append("Battery capacity unknown: runtime prediction skipped.")
        else:
            if baseline.measured_system_power_w is not None and baseline.on_battery:
                base_sys = baseline.measured_system_power_w
                assumptions.append(
                    f"System power baseline measured from battery discharge rate ({base_sys:.1f} W)."
                )
                confidence_score += 0.15
            else:
                base_sys = p_now + PLATFORM_W
                assumptions.append(
                    f"System power baseline estimated: package + {PLATFORM_W:.0f} W platform (on AC, "
                    "discharge rate not measurable)."
                )
            sys_power_new = max(
                1.0,
                base_sys + (p_new - p_now) + (25.0 * profile.gpu_load if baseline.has_discrete_gpu else 0.0),
            )
            runtime_h = baseline.battery_remaining_wh / sys_power_new
            spread = 0.15 if (baseline.measured_system_power_w is not None and baseline.on_battery) else 0.30
            runtime_band = (runtime_h * (1 - spread), runtime_h * (1 + spread))
            used_wh = sys_power_new * duration_s / 3600.0
            battery_end = max(
                0.0, 100.0 * (baseline.battery_remaining_wh - used_wh) / baseline.battery_full_wh
            )

    # ----- trajectory (deterministic)
    trajectory: list[dict[str, Any]] = []
    charge_pct = baseline.battery_percent

    def fan_duty(temp: float) -> float:
        span = max(5.0, pc.tj_max_c - 5.0 - fan_start)
        return max(0.0, min(100.0, 100.0 * (temp - fan_start) / span))

    for t in range(0, duration_s + 1, step_s):
        point: dict[str, Any] = {
            "t_s": t,
            "cpu_usage": round(100 * profile.cpu_load, 1),
            "gpu_usage": round(100 * profile.gpu_load, 1),
        }
        if t0 is not None and t_ss is not None:
            temp_t = t_ss + (t0 - t_ss) * math.exp(-t / TAU_S)
            point["temperature_c"] = round(temp_t, 2)
            if sigma is not None:
                band = 1.96 * sigma * (1.0 - math.exp(-t / TAU_S))
                point["temperature_low_c"] = round(temp_t - band, 2)
                point["temperature_high_c"] = round(temp_t + band, 2)
            point["fan_duty_percent_est"] = round(fan_duty(temp_t), 1)
        if mem_used_new is not None and mem_total:
            point["memory_percent"] = round(100 * mem_used_new / mem_total, 1)
        if (
            sys_power_new is not None
            and baseline.battery_remaining_wh is not None
            and baseline.battery_full_wh
        ):
            remaining = baseline.battery_remaining_wh - sys_power_new * t / 3600.0
            point["battery_percent"] = round(max(0.0, 100 * remaining / baseline.battery_full_wh), 2)
        elif charge_w is not None and charge_pct is not None and baseline.battery_full_wh:
            if t > 0:
                taper = 1.0 if charge_pct < 80 else max(0.1, (100.0 - charge_pct) / 20.0)
                charge_pct = min(
                    100.0, charge_pct + 100.0 * charge_w * taper * step_s / 3600.0 / baseline.battery_full_wh
                )
            point["battery_percent"] = round(charge_pct, 2)
            point["charge_power_w"] = round(
                charge_w * (1.0 if charge_pct < 80 else max(0.1, (100 - charge_pct) / 20)), 2
            )
        point["package_power_w"] = round(p_new if not throttled else pc.sustained_w, 2)
        trajectory.append(point)

    final_temp = trajectory[-1].get("temperature_c") if trajectory else None
    current = {
        "cpu_usage_percent": _round(baseline.cpu_usage),
        "gpu_usage_percent": _round(baseline.gpu_usage),
        "temperature_c": _round(t0),
        "temperature_sensor": baseline.temperature_label,
        "memory_used_gb": _round(baseline.memory_used_gb, 2),
        "memory_percent": _round(
            100 * baseline.memory_used_gb / mem_total if baseline.memory_used_gb and mem_total else None
        ),
        "estimated_package_power_w": _round(p_now),
        "battery_percent": _round(baseline.battery_percent),
        "power_source": "battery" if baseline.on_battery else "ac",
    }
    predicted = {
        "cpu_usage_percent": round(100 * profile.cpu_load, 1),
        "gpu_usage_percent": round(100 * profile.gpu_load, 1),
        "temperature_c": final_temp,
        "steady_state_temperature_c": _round(t_ss),
        "thermal_throttling": throttled,
        "memory_used_gb": _round(mem_used_new, 2),
        "memory_percent": _round(100 * mem_used_new / mem_total if mem_used_new and mem_total else None),
        "paged_memory_gb": round(paging_gb, 2),
        "estimated_package_power_w": round(p_new, 1),
        "estimated_system_power_w": _round(sys_power_new),
        "battery_runtime_h": _round(runtime_h, 2),
        "battery_runtime_h_low": _round(runtime_band[0], 2) if runtime_band else None,
        "battery_runtime_h_high": _round(runtime_band[1], 2) if runtime_band else None,
        "battery_percent_at_end": _round(battery_end)
        if battery_end is not None
        else (trajectory[-1].get("battery_percent") if trajectory and charge_w is not None else None),
        "charge_power_w": _round(charge_w),
        "power_source": "battery" if on_batt else "ac",
        "temperature_low_c": trajectory[-1].get("temperature_low_c") if trajectory else None,
        "temperature_high_c": trajectory[-1].get("temperature_high_c") if trajectory else None,
        "temperature_sigma_c": _round(sigma, 2),
        "fan_duty_percent_est": trajectory[-1].get("fan_duty_percent_est") if trajectory else None,
        "ambient_c": env.ambient_c,
        "thermal_profile": env.thermal_profile.value,
    }

    def _diff(a: Any, b: Any) -> float | None:
        if isinstance(a, bool) or not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return None
        return _round(float(a) - float(b))

    difference = {
        k: _diff(predicted.get(k), current.get(k))
        for k in (
            "cpu_usage_percent",
            "gpu_usage_percent",
            "temperature_c",
            "memory_percent",
            "estimated_package_power_w",
            "battery_percent",
        )
    }
    if predicted.get("battery_percent_at_end") is not None and current.get("battery_percent") is not None:
        difference["battery_percent"] = _diff(predicted["battery_percent_at_end"], current["battery_percent"])
    level = "medium" if confidence_score >= 0.5 else "low"
    return SimulationResult(
        scenario,
        duration_s,
        current,
        predicted,
        difference,
        assumptions,
        level,
        round(min(confidence_score, 0.8), 2),
        warnings,
        trajectory,
    )
