"""Thermal/performance analytics and cautious, explainable predictions.

Predictions use simple, inspectable methods (least-squares trends, capacity ratios). Each result
carries its method, assumptions, supporting metrics and a confidence that reflects how much data it
was computed from. Language is deliberately conservative ("elevated trend detected"), never
"will fail on <date>".
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.analytics.stats import LinearFit, confidence_from_fit, linear_fit, summarize
from app.domain.components.state import cpu_temperature
from app.repositories.base import TelemetryRepository
from app.services.digital_twin import DigitalTwinService, TwinState

Series = list[tuple[float, float]]


class AnalyticsService:
    def __init__(self, twin: DigitalTwinService, repo: TelemetryRepository) -> None:
        self._twin = twin
        self._repo = repo

    async def _series(self, twin: TwinState, key: str, minutes: int, end: datetime | None = None) -> Series:
        """Persisted history for the window (ending now or at ``end``), topped up with live data."""
        now = datetime.now(UTC)
        end = min(end, now) if end is not None else now
        start = end - timedelta(minutes=minutes)
        try:
            rows = await self._repo.raw_values(twin.device.device_id, key, start, end)
        except Exception:
            rows = []
        points = [(t.timestamp(), v) for t, v in rows]
        live = twin.window.values_since(key, start.timestamp())
        last = points[-1][0] if points else 0.0
        cutoff = end.timestamp()
        points.extend(p for p in live if last < p[0] <= cutoff)
        return points

    # ------------------------------------------------------------------ thermal
    async def thermal(
        self, device_id: str | None, minutes: int, end: datetime | None = None
    ) -> dict[str, Any]:
        twin = self._require(device_id)
        temp = cpu_temperature(twin.components)
        sensors: list[dict[str, Any]] = []
        keys = [
            r.key
            for c in twin.components.values()
            for r in c.telemetry.values()
            if r.metric
            in ("cpu.temperature_c", "thermal.zone_temperature_c", "gpu.temperature_c", "disk.temperature_c")
            and r.available
        ]
        for key in keys:
            pts = await self._series(twin, key, minutes, end)
            vals = [v for _, v in pts]
            s = summarize(vals)
            fit = linear_fit(pts)
            interval = _median_interval(pts)
            sensors.append(
                {
                    "metric_key": key,
                    "samples": s.count,
                    "mean_c": _r(s.mean),
                    "min_c": _r(s.minimum),
                    "max_c": _r(s.maximum),
                    "p95_c": _r(s.p95),
                    "trend_c_per_min": _r(fit.slope * 60 if fit else None, 3),
                    "trend_r2": _r(fit.r2 if fit else None, 3),
                    "time_above_80c_s": round(sum(1 for v in vals if v >= 80) * interval)
                    if interval
                    else None,
                    "time_above_90c_s": round(sum(1 for v in vals if v >= 90) * interval)
                    if interval
                    else None,
                }
            )
        throttle_keys = [
            r.key for r in twin.components["thermal_sensors"].readings("thermal.passive_limit_percent")
        ]
        throttled_s = 0.0
        for key in throttle_keys:
            pts = await self._series(twin, key, minutes, end)
            interval = _median_interval(pts) or 0.0
            throttled_s += sum(1 for _, v in pts if v < 100) * interval
        return {
            "device_id": twin.device.device_id,
            "window_minutes": minutes,
            "window_end": (end or datetime.now(UTC)).isoformat(),
            "primary_sensor": {
                "label": temp.label,
                "metric_key": temp.metric,
                "value_c": round(temp.value, 2),
                "source": temp.source,
            }
            if temp
            else None,
            "sensors": sensors,
            "throttled_seconds": round(throttled_s),
            "cpu_package_sensor_available": bool(temp and temp.label == "CPU package sensor"),
            "notes": []
            if temp and temp.label == "CPU package sensor"
            else [
                "CPU package temperature is not exposed without LibreHardwareMonitor (administrator). "
                "Values shown come from the ACPI thermal zone, a firmware platform sensor near the SoC."
            ],
        }

    # -------------------------------------------------------------- performance
    async def performance(
        self, device_id: str | None, minutes: int, end: datetime | None = None
    ) -> dict[str, Any]:
        twin = self._require(device_id)
        wanted = [
            "cpu.usage_percent",
            "cpu.frequency_mhz",
            "memory.usage_percent",
            "disk.read_bytes_per_sec",
            "disk.write_bytes_per_sec",
            "network.rx_bytes_per_sec",
            "network.tx_bytes_per_sec",
            "power.system_power_w",
            "battery.charge_percent",
        ]
        gpu_keys = [r.key for c in twin.components.values() for r in c.readings("gpu.usage_percent")]
        out: dict[str, Any] = {}
        for key in wanted + gpu_keys:
            pts = await self._series(twin, key, minutes, end)
            s = summarize([v for _, v in pts])
            out[key] = {
                "samples": s.count,
                "mean": _r(s.mean, 3),
                "min": _r(s.minimum, 3),
                "max": _r(s.maximum, 3),
                "p95": _r(s.p95, 3),
                "stddev": _r(s.stddev, 3),
            }
        return {
            "device_id": twin.device.device_id,
            "window_minutes": minutes,
            "window_end": (end or datetime.now(UTC)).isoformat(),
            "metrics": out,
        }

    # -------------------------------------------------------------- predictions
    async def predictions(self, device_id: str | None) -> dict[str, Any]:
        twin = self._require(device_id)
        items = [
            await self._thermal_trend(twin),
            await self._memory_pressure(twin),
            await self._storage_trend(twin),
            self._battery_wear(twin),
        ]
        return {
            "device_id": twin.device.device_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "disclaimer": (
                "Trend estimates from observed telemetry; not a guarantee of future hardware behaviour."
            ),
            "predictions": items,
        }

    async def _thermal_trend(self, twin: TwinState) -> dict[str, Any]:
        temp = cpu_temperature(twin.components)
        base = _prediction(
            "thermal_trend", "CPU-area thermal trend", "Least-squares trend over the last 30 minutes"
        )
        if temp is None:
            return {**base, "status": "unavailable", "statement": "No temperature sensor available."}
        pts = await self._series(twin, temp.metric, 30)
        fit = linear_fit(pts)
        level, score = confidence_from_fit(fit, min_points=30, min_span_s=300)
        base["supporting_metrics"] = {
            "sensor": temp.label,
            "current_c": round(temp.value, 1),
            "samples": len(pts),
            **_fit_dict(fit, per="min"),
        }
        if level == "insufficient" or fit is None:
            return {
                **base,
                "status": "insufficient_data",
                "confidence": "insufficient",
                "confidence_score": 0.0,
                "statement": "Not enough history yet (needs >= 5 minutes of samples).",
            }
        slope_min = fit.slope * 60
        reference = 95.0
        statement = f"Temperature stable ({slope_min:+.2f} °C/min)."
        status = "normal"
        if slope_min > 0.2 and fit.r2 > 0.4:
            status = "elevated"
            eta = fit.time_to_reach(reference, pts[-1][0])
            statement = f"Elevated thermal trend detected ({slope_min:+.2f} °C/min)."
            if eta is not None and eta < 3600:
                statement += (
                    f" At this rate the {reference:.0f} °C reference level would be reached"
                    f" in ~{eta / 60:.0f} min."
                )
        risk = (
            "high" if temp.value >= 90 else "moderate" if temp.value >= 80 or status == "elevated" else "low"
        )
        base["supporting_metrics"]["throttling_risk"] = risk
        base["assumptions"] = [
            "Linear extrapolation; real thermal behaviour saturates as fans ramp up.",
            f"Reference level {reference:.0f} °C (below typical Intel/AMD junction limits).",
        ]
        return {
            **base,
            "status": status,
            "confidence": level,
            "confidence_score": score,
            "statement": statement + f" Thermal throttling risk: {risk}.",
        }

    async def _memory_pressure(self, twin: TwinState) -> dict[str, Any]:
        base = _prediction(
            "memory_pressure", "Memory pressure", "Least-squares trend of RAM utilization (30 min)"
        )
        pts = await self._series(twin, "memory.usage_percent", 30)
        fit = linear_fit(pts)
        level, score = confidence_from_fit(fit, min_points=30, min_span_s=300)
        current = pts[-1][1] if pts else None
        base["supporting_metrics"] = {
            "current_percent": _r(current),
            "samples": len(pts),
            **_fit_dict(fit, per="min"),
        }
        if level == "insufficient" or fit is None or current is None:
            return {
                **base,
                "status": "insufficient_data",
                "confidence": "insufficient",
                "confidence_score": 0.0,
                "statement": "Not enough history yet (needs >= 5 minutes of samples).",
            }
        status = "elevated" if current >= 90 else "normal"
        statement = f"RAM utilization {current:.0f}%."
        if fit.slope * 60 > 0.05 and fit.r2 > 0.3 and current < 95:
            eta = fit.time_to_reach(95.0, pts[-1][0])
            if eta is not None and eta < 4 * 3600:
                status = "elevated"
                rate = fit.slope * 3600
                statement += f" Rising {rate:+.1f} %/h; would reach 95% in ~{eta / 60:.0f} min at this rate."
        elif current >= 90:
            statement += " Sustained high utilization: the system is likely paging to disk."
        return {
            **base,
            "status": status,
            "confidence": level,
            "confidence_score": score,
            "statement": statement,
            "assumptions": ["Workload stays similar to the observed window."],
        }

    async def _storage_trend(self, twin: TwinState) -> dict[str, Any]:
        base = _prediction(
            "storage_capacity", "Storage capacity trend", "Least-squares trend of volume usage (7 days)"
        )
        readings = [
            r for r in twin.components["storage"].readings("disk.usage_percent") if r.numeric is not None
        ]
        if not readings:
            return {**base, "status": "unavailable", "statement": "No volume usage data."}
        r = max(readings, key=lambda x: x.numeric or 0.0)
        pts = await self._series(twin, r.key, 7 * 24 * 60)
        fit = linear_fit(pts)
        level, score = confidence_from_fit(fit, min_points=50, min_span_s=24 * 3600)
        current = r.numeric or 0.0
        base["supporting_metrics"] = {
            "volume": r.labels.get("volume"),
            "current_percent": round(current, 1),
            "samples": len(pts),
            "history_hours": round(fit.span_s / 3600, 1) if fit else 0,
        }
        if level == "insufficient" or fit is None:
            return {
                **base,
                "status": "insufficient_data",
                "confidence": "insufficient",
                "confidence_score": 0.0,
                "statement": f"Volume {r.labels.get('volume')} is {current:.0f}% used. "
                "A capacity trend needs at least 24 hours of history.",
            }
        per_day = fit.slope * 86400
        eta = fit.time_to_reach(95.0, pts[-1][0]) if per_day > 0.01 else None
        statement = f"Volume {r.labels.get('volume')} {current:.0f}% used, changing {per_day:+.2f} %/day."
        if eta is not None:
            statement += f" Would reach 95% in ~{eta / 86400:.0f} days at this rate."
        return {
            **base,
            "status": "elevated" if current >= 90 else "normal",
            "confidence": level,
            "confidence_score": score,
            "statement": statement,
        }

    def _battery_wear(self, twin: TwinState) -> dict[str, Any]:
        base = _prediction(
            "battery_degradation",
            "Battery degradation",
            "Capacity ratio (full-charge / design) and wear per cycle",
        )
        bat = twin.components.get("battery")
        health = bat.value("battery.health_percent") if bat else None
        cycles = bat.value("battery.cycle_count") if bat else None
        if health is None:
            reason = (
                "No battery present."
                if bat and bat.current_state == "absent"
                else "Battery capacity data unavailable."
            )
            return {**base, "status": "unavailable", "statement": reason}
        wear = max(0.0, 100.0 - health)
        base["supporting_metrics"] = {
            "health_percent": round(health, 2),
            "cycle_count": cycles,
            "full_charge_wh": bat.value("battery.full_charge_capacity_wh") if bat else None,
            "design_wh": bat.value("battery.design_capacity_wh") if bat else None,
        }
        statement = f"Battery retains {health:.1f}% of its design capacity ({wear:.1f}% wear)."
        if cycles and cycles > 20 and wear > 0.5:
            per_100 = wear / cycles * 100
            to_80 = (health - 80.0) / (wear / cycles) if health > 80 else 0
            base["supporting_metrics"]["wear_per_100_cycles_percent"] = round(per_100, 2)
            statement += (
                f" Observed wear ~{per_100:.1f}% per 100 cycles; at that rate the common 80% end-of-life "
                f"threshold would be reached after ~{to_80:.0f} more cycles."
            )
        return {
            **base,
            "status": "elevated" if health < 80 else "normal",
            "confidence": "low",
            "confidence_score": 0.25,
            "statement": statement,
            "assumptions": [
                "Assumes linear wear since new; lithium cells usually degrade faster early and late.",
                "Capacity values are reported by the battery fuel gauge and can drift until recalibrated.",
            ],
        }

    def _require(self, device_id: str | None) -> TwinState:
        twin = self._twin.get(device_id)
        if twin is None:
            raise LookupError("No device has reported telemetry yet")
        return twin


def _prediction(pid: str, title: str, method: str) -> dict[str, Any]:
    return {
        "prediction_id": pid,
        "title": title,
        "method": method,
        "kind": "PREDICTED",
        "confidence": "insufficient",
        "confidence_score": 0.0,
        "supporting_metrics": {},
        "assumptions": [],
    }


def _fit_dict(fit: LinearFit | None, per: str) -> dict[str, Any]:
    if fit is None:
        return {}
    factor = 60 if per == "min" else 1
    return {
        f"trend_per_{per}": round(fit.slope * factor, 4),
        "r2": round(fit.r2, 3),
        "span_minutes": round(fit.span_s / 60, 1),
    }


def _median_interval(points: Series) -> float | None:
    if len(points) < 2:
        return None
    gaps = sorted(b[0] - a[0] for a, b in itertools.pairwise(points))
    return gaps[len(gaps) // 2]


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)
