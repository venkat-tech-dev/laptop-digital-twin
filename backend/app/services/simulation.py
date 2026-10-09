"""What-if simulation service. Builds a baseline from the live twin and runs the domain model.

The result is always labelled SIMULATION / GENERATED DATA and is never fed back into the live twin.
"""

from __future__ import annotations

import bisect
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from app.domain.analytics.stats import linear_fit
from app.domain.components.models import ComponentType
from app.domain.components.state import cpu_temperature
from app.domain.simulation.model import (
    PROFILES,
    Baseline,
    Environment,
    Scenario,
    ThermalProfile,
    WorkloadProfile,
    simulate,
)
from app.services.digital_twin import DigitalTwinService, TwinState

GB = 1024**3


class SimulationService:
    def __init__(self, twin: DigitalTwinService) -> None:
        self._twin = twin

    @staticmethod
    def scenarios() -> list[dict[str, Any]]:
        return [{"scenario": s.value, **asdict(p)} for s, p in PROFILES.items()]

    def run(
        self,
        scenario: Scenario,
        duration_s: int,
        overrides: dict[str, float | bool] | None,
        device_id: str | None = None,
        ambient_c: float | None = None,
        thermal_profile: str | None = None,
        adapter_w: float | None = None,
    ) -> dict[str, Any]:
        twin = self._twin.get(device_id)
        if twin is None:
            raise LookupError("No live device: a simulation needs the current physical state as its baseline")
        profile = PROFILES.get(scenario) or WorkloadProfile(0.5, 0.2, 1.0, False, "Custom workload")
        if overrides:
            profile = WorkloadProfile(
                cpu_load=float(overrides.get("cpu_load", profile.cpu_load)),
                gpu_load=float(overrides.get("gpu_load", profile.gpu_load)),
                ram_gb=float(overrides.get("ram_gb", profile.ram_gb)),
                on_battery=bool(overrides.get("on_battery", profile.on_battery)),
                description=profile.description + " (customised)",
            )
        baseline = self.baseline(twin)
        env = Environment(
            ambient_c=25.0 if ambient_c is None else ambient_c,
            thermal_profile=ThermalProfile(thermal_profile or "balanced"),
            adapter_w=65.0 if adapter_w is None else adapter_w,
        )
        result = simulate(baseline, profile, scenario, duration_s, env=env)
        return {
            "mode": "SIMULATION",
            "label": "SIMULATION — GENERATED DATA",
            "generated_at": datetime.now(UTC).isoformat(),
            "device_id": twin.device.device_id,
            "baseline_captured_at": twin.device.last_seen.isoformat() if twin.device.last_seen else None,
            "baseline_device_status": twin.device.status.value,
            "workload": asdict(profile),
            **{k: v for k, v in asdict(result).items() if k != "scenario"},
            "scenario": scenario.value,
        }

    def baseline(self, twin: TwinState) -> Baseline:
        c = twin.components
        temp = cpu_temperature(c)
        mem = c.get("memory")
        bat = c.get("battery")
        gpus = [x for x in c.values() if x.component_type is ComponentType.GPU]
        gpu_usage = max((g.value("gpu.usage_percent") or 0.0 for g in gpus), default=None)
        mem_used = mem.value("memory.used_bytes") if mem else None
        mem_total = mem.value("memory.total_bytes") if mem else None
        power = c.get("power")
        fit = self._temp_vs_load(twin, temp.metric if temp else None)
        return Baseline(
            cpu_usage=c["cpu"].value("cpu.usage_percent"),
            temperature_c=temp.value if temp else None,
            temperature_label=temp.label if temp else None,
            memory_used_gb=mem_used / GB if mem_used else None,
            memory_total_gb=mem_total / GB if mem_total else None,
            gpu_usage=gpu_usage,
            battery_percent=bat.value("battery.charge_percent") if bat else None,
            battery_remaining_wh=bat.value("battery.remaining_capacity_wh") if bat else None,
            battery_full_wh=bat.value("battery.full_charge_capacity_wh") if bat else None,
            measured_system_power_w=power.value("power.system_power_w") if power else None,
            on_battery=bool(power and power.current_state == "battery"),
            cpu_model=c["cpu"].model,
            has_discrete_gpu=any(not g.properties.get("integrated", True) for g in gpus),
            temp_vs_load_fit=fit[:3] if fit else None,
            temp_fit_sigma=fit[3] if fit else None,
        )

    @staticmethod
    def _temp_vs_load(twin: TwinState, temp_key: str | None) -> tuple[float, float, float, float] | None:
        """Calibrate temperature vs. CPU load from the live window (needs varied load to be meaningful)."""
        if temp_key is None:
            return None
        load = dict(twin.window.values_since("cpu.usage_percent", 0))
        temps = twin.window.values_since(temp_key, 0)
        pairs: list[tuple[float, float]] = []
        load_ts = sorted(load)
        if not load_ts:
            return None
        for ts, t in temps:
            i = bisect.bisect_left(load_ts, ts)
            near = [load_ts[j] for j in (i - 1, i) if 0 <= j < len(load_ts)]
            best = min(near, key=lambda x: abs(x - ts))
            if abs(best - ts) <= 2.0:
                pairs.append((load[best] / 100.0, t))
        if len(pairs) < 120:
            return None
        loads = [p[0] for p in pairs]
        if max(loads) - min(loads) < 0.3:
            return None  # not enough load variation to calibrate
        fit = linear_fit(sorted(pairs))
        if fit is None or fit.r2 < 0.3 or fit.slope <= 0:
            return None
        residuals = [t - (fit.intercept + fit.slope * x) for x, t in pairs]
        sigma = (sum(r * r for r in residuals) / max(1, len(residuals) - 2)) ** 0.5
        return (fit.intercept, fit.slope, fit.r2, sigma)
