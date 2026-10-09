"""Explainable, rule-based health scoring.

Every component starts at 100. Each rule that fires deducts a fixed, documented number of points and
records a human-readable reason with the metric and value that caused it. Components whose
telemetry is unavailable get ``score=None`` / ``unknown`` and are excluded from the overall score
(an unobservable sensor is not evidence of good or bad health).

Thresholds are documented in ``docs/telemetry.md`` ("Health engine").
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.domain.components.models import Component, ComponentHealth, ComponentType, HealthReason, HealthStatus
from app.domain.components.state import cpu_temperature
from app.domain.telemetry.models import MetricWindow

T = ComponentType

WEIGHTS: dict[str, float] = {
    "cpu": 0.20,
    "thermal_sensors": 0.20,
    "memory": 0.15,
    "storage": 0.15,
    "battery": 0.15,
    "gpu": 0.10,
    "network": 0.05,
}


def status_for(score: int | None) -> HealthStatus:
    if score is None:
        return HealthStatus.UNKNOWN
    if score >= 85:
        return HealthStatus.HEALTHY
    if score >= 60:
        return HealthStatus.WARNING
    return HealthStatus.CRITICAL


@dataclass
class _Score:
    reasons: list[HealthReason]
    observed: bool = False

    def ok(self, message: str, metric: str | None = None, value: float | str | None = None) -> None:
        self.observed = True
        self.reasons.append(HealthReason("ok", message, 0, metric, value))

    def deduct(
        self,
        points: int,
        severity: str,
        message: str,
        metric: str | None = None,
        value: float | str | None = None,
    ) -> None:
        self.observed = True
        self.reasons.append(HealthReason(severity, message, -points, metric, value))

    def result(self) -> ComponentHealth:
        if not self.observed:
            return ComponentHealth(None, HealthStatus.UNKNOWN, self.reasons)
        score = max(0, 100 + sum(r.impact for r in self.reasons))
        return ComponentHealth(score, status_for(score), self.reasons)


def _avg(window: MetricWindow, key: str, since: float) -> float | None:
    pts = window.values_since(key, since)
    return sum(v for _, v in pts) / len(pts) if pts else None


class HealthEngine:
    def __init__(self, window: MetricWindow, sustained_s: float = 60.0) -> None:
        self._window = window
        self._sustained_s = sustained_s
        self._current: dict[str, ComponentHealth] = {}
        self._rules: dict[
            ComponentType, Callable[[Component, Mapping[str, Component], float], ComponentHealth]
        ] = {
            T.CPU: self._cpu,
            T.GPU: self._gpu,
            T.MEMORY: self._memory,
            T.STORAGE: self._storage,
            T.DISK: self._disk,
            T.BATTERY: self._battery,
            T.THERMAL_SENSORS: self._thermal,
            T.NETWORK: self._network,
            T.FAN: self._fan,
        }

    def evaluate(self, components: Mapping[str, Component], now_ts: float) -> dict[str, ComponentHealth]:
        out: dict[str, ComponentHealth] = {}
        self._current = out
        # Aggregates (storage) are evaluated after their children (disks).
        ordered = sorted(components.items(), key=lambda kv: kv[1].component_type is T.STORAGE)
        for cid, comp in ordered:
            rule = self._rules.get(comp.component_type)
            if rule is not None:
                out[cid] = rule(comp, components, now_ts)
        return out

    def overall(
        self, health: Mapping[str, ComponentHealth], components: Mapping[str, Component]
    ) -> ComponentHealth:
        total_w = 0.0
        acc = 0.0
        reasons: list[HealthReason] = []
        for cid, weight in WEIGHTS.items():
            ids = (
                [cid]
                if cid in health
                else [c for c, comp in components.items() if comp.component_type.value == cid and c in health]
            )
            scores = [health[i].score for i in ids if health[i].score is not None]
            if not scores:
                if any(i in components for i in ids) or cid in components:
                    reasons.append(
                        HealthReason("info", f"{_label(cid)} health not observable (sensor unavailable)")
                    )
                continue
            comp_score = min(s for s in scores if s is not None)
            acc += weight * comp_score
            total_w += weight
            for i in ids:
                reasons.extend(r for r in health[i].reasons if r.severity != "ok")
        if total_w == 0:
            return ComponentHealth(None, HealthStatus.UNKNOWN, reasons)
        score = round(acc / total_w)
        ok = [
            r
            for i in ("cpu", "thermal_sensors", "memory", "storage", "battery")
            if i in health
            for r in health[i].reasons
            if r.severity == "ok"
        ]
        problems = sorted(reasons, key=lambda r: r.impact)
        return ComponentHealth(score, status_for(score), problems + ok[:6])

    # ----------------------------------------------------------------- rules
    def _cpu(self, c: Component, comps: Mapping[str, Component], now: float) -> ComponentHealth:
        s = _Score([])
        temp = cpu_temperature(comps)
        if temp is not None:
            v = round(temp.value, 1)
            if temp.value >= 98:
                s.deduct(40, "critical", f"CPU temperature critical ({v} °C, {temp.label})", temp.metric, v)
            elif temp.value >= 90:
                s.deduct(25, "warning", f"CPU temperature high ({v} °C, {temp.label})", temp.metric, v)
            elif temp.value >= 80:
                s.deduct(10, "warning", f"CPU temperature elevated ({v} °C, {temp.label})", temp.metric, v)
            else:
                s.ok(f"CPU temperature normal ({v} °C, {temp.label})", temp.metric, v)
        if c.current_state == "throttling":
            s.deduct(
                15,
                "warning",
                "Firmware is passively limiting CPU performance (thermal throttling)",
                "thermal.passive_limit_percent",
            )
        avg = _avg(self._window, "cpu.usage_percent", now - self._sustained_s)
        if avg is not None:
            if avg >= 90:
                s.deduct(
                    5,
                    "info",
                    f"Sustained CPU load ({avg:.0f}% average over {self._sustained_s:.0f} s)",
                    "cpu.usage_percent",
                    round(avg, 1),
                )
            else:
                s.ok(
                    f"CPU load {avg:.0f}% (avg {self._sustained_s:.0f} s)", "cpu.usage_percent", round(avg, 1)
                )
        return s.result()

    def _gpu(self, c: Component, _: Mapping[str, Component], now: float) -> ComponentHealth:
        s = _Score([])
        temp = c.value("gpu.temperature_c")
        if temp is not None:
            if temp >= 95:
                s.deduct(
                    35, "critical", f"GPU temperature critical ({temp:.0f} °C)", "gpu.temperature_c", temp
                )
            elif temp >= 85:
                s.deduct(15, "warning", f"GPU temperature high ({temp:.0f} °C)", "gpu.temperature_c", temp)
            else:
                s.ok(f"GPU temperature normal ({temp:.0f} °C)", "gpu.temperature_c", temp)
        usage = c.reading("gpu.usage_percent")
        if usage is not None and usage.numeric is not None:
            avg = _avg(self._window, usage.key, now - self._sustained_s)
            if avg is not None and avg >= 95:
                s.deduct(5, "info", f"Sustained GPU load ({avg:.0f}%)", usage.key, round(avg, 1))
            else:
                s.ok(f"GPU load {usage.numeric:.0f}%", usage.key, round(usage.numeric, 1))
        return s.result()

    def _memory(self, c: Component, _: Mapping[str, Component], now: float) -> ComponentHealth:
        s = _Score([])
        usage = c.value("memory.usage_percent")
        if usage is not None:
            avg = _avg(self._window, "memory.usage_percent", now - self._sustained_s) or usage
            if avg >= 95:
                s.deduct(
                    25,
                    "warning",
                    f"Memory utilization critical ({avg:.0f}% sustained)",
                    "memory.usage_percent",
                    round(avg, 1),
                )
            elif avg >= 85:
                s.deduct(
                    10,
                    "warning",
                    f"Memory utilization elevated ({avg:.0f}%)",
                    "memory.usage_percent",
                    round(avg, 1),
                )
            else:
                s.ok(f"Memory utilization normal ({avg:.0f}%)", "memory.usage_percent", round(avg, 1))
        swap = c.value("memory.swap_percent")
        if swap is not None and swap >= 50:
            s.deduct(5, "info", f"Page file heavily used ({swap:.0f}%)", "memory.swap_percent", swap)
        return s.result()

    def _storage(self, c: Component, comps: Mapping[str, Component], _: float) -> ComponentHealth:
        s = _Score([])
        for r in c.readings("disk.usage_percent"):
            if r.numeric is None:
                continue
            vol = r.labels.get("volume", "volume")
            if r.numeric >= 95:
                s.deduct(30, "critical", f"Volume {vol} almost full ({r.numeric:.0f}%)", r.key, r.numeric)
            elif r.numeric >= 90:
                s.deduct(15, "warning", f"Volume {vol} low on space ({r.numeric:.0f}%)", r.key, r.numeric)
            else:
                s.ok(f"Volume {vol} {r.numeric:.0f}% used", r.key, r.numeric)
        for disk in (d for d in comps.values() if d.component_type is T.DISK):
            h = self._current.get(disk.component_id, disk.health)
            if h.score is not None:
                s.observed = True
                s.reasons.extend(h.reasons)
        return s.result()

    def _disk(self, c: Component, _: Mapping[str, Component], now: float) -> ComponentHealth:
        s = _Score([])
        status = c.reading("disk.health_status")
        if status is not None and status.available:
            if status.value == "Healthy":
                s.ok(f"{c.name}: Windows reports drive health 'Healthy'", status.key, "Healthy")
            elif status.value == "Warning":
                s.deduct(
                    40, "warning", f"{c.name}: Windows reports drive health 'Warning'", status.key, "Warning"
                )
            elif status.value == "Unhealthy":
                s.deduct(
                    80,
                    "critical",
                    f"{c.name}: Windows reports drive health 'Unhealthy'",
                    status.key,
                    "Unhealthy",
                )
        for metric in ("disk.avg_read_latency_ms", "disk.avg_write_latency_ms"):
            r = c.reading(metric)
            if r is None or r.numeric is None:
                continue
            avg = _avg(self._window, r.key, now - self._sustained_s)
            if avg is not None and avg >= 50:
                s.deduct(
                    10, "warning", f"{c.name}: high average I/O latency ({avg:.0f} ms)", r.key, round(avg, 1)
                )
        temp = c.value("disk.temperature_c")
        if temp is not None and temp >= 70:
            s.deduct(
                15, "warning", f"{c.name}: drive temperature high ({temp:.0f} °C)", "disk.temperature_c", temp
            )
        return s.result()

    def _battery(self, c: Component, _: Mapping[str, Component], __: float) -> ComponentHealth:
        s = _Score([])
        health = c.value("battery.health_percent")
        if health is not None:
            wear = max(0.0, 100.0 - health)
            if wear < 1:
                s.ok(f"Battery capacity {health:.1f}% of design", "battery.health_percent", round(health, 1))
            else:
                severity = "critical" if health < 60 else "warning" if health < 80 else "info"
                s.deduct(
                    round(min(wear, 60)),
                    severity,
                    f"Battery health reduced by {wear:.1f}% (full charge {health:.1f}% of design capacity)",
                    "battery.health_percent",
                    round(health, 1),
                )
        cycles = c.value("battery.cycle_count")
        if cycles is not None and cycles >= 800:
            s.deduct(5, "info", f"High battery cycle count ({cycles:.0f})", "battery.cycle_count", cycles)
        charge = c.value("battery.charge_percent")
        if charge is not None and c.current_state == "discharging" and charge < 10:
            s.deduct(10, "warning", f"Battery charge low ({charge:.0f}%)", "battery.charge_percent", charge)
        return s.result()

    def _thermal(self, c: Component, comps: Mapping[str, Component], _: float) -> ComponentHealth:
        s = _Score([])
        for r in c.readings("thermal.zone_temperature_c"):
            if r.numeric is None:
                continue
            zone = r.labels.get("zone", "zone")
            if r.numeric >= 98:
                s.deduct(
                    40, "critical", f"Thermal zone {zone} critical ({r.numeric:.1f} °C)", r.key, r.numeric
                )
            elif r.numeric >= 90:
                s.deduct(20, "warning", f"Thermal zone {zone} hot ({r.numeric:.1f} °C)", r.key, r.numeric)
            elif r.numeric >= 80:
                s.deduct(8, "info", f"Thermal zone {zone} elevated ({r.numeric:.1f} °C)", r.key, r.numeric)
            else:
                s.ok(f"Thermal zone {zone} normal ({r.numeric:.1f} °C)", r.key, r.numeric)
        for r in c.readings("thermal.passive_limit_percent"):
            if r.numeric is not None and r.numeric < 100:
                s.deduct(
                    20,
                    "warning",
                    f"Passive cooling limit active ({r.numeric:.0f}% of max performance)",
                    r.key,
                    r.numeric,
                )
        return s.result()

    def _network(self, c: Component, comps: Mapping[str, Component], _: float) -> ComponentHealth:
        s = _Score([])
        nics = [n for n in comps.values() if n.component_type is T.NETWORK_ADAPTER]
        if not nics:
            return s.result()
        if any(n.current_state == "up" for n in nics):
            s.ok("Network link up")
        else:
            s.deduct(10, "warning", "No network adapter has an active link")
        errs = c.value("network.errors_per_sec")
        if errs is not None and errs > 10:
            s.deduct(10, "warning", f"Network errors {errs:.0f}/s", "network.errors_per_sec", errs)
        return s.result()

    def _fan(self, c: Component, _: Mapping[str, Component], __: float) -> ComponentHealth:
        s = _Score([])
        rpm = c.value("fan.speed_rpm")
        if rpm is not None:
            s.ok(f"Fan reporting {rpm:.0f} rpm", "fan.speed_rpm", rpm)
        return s.result()


def _label(cid: str) -> str:
    return {"thermal_sensors": "Thermal", "cpu": "CPU", "gpu": "GPU"}.get(cid, cid.capitalize())
