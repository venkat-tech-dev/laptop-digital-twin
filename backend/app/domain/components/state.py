"""Derive each component's operational state from its current telemetry (pure functions)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.components.models import Component, ComponentType

T = ComponentType

# Thermal bands (°C) used for state and visual colouring. Documented in docs/telemetry.md.
THERMAL_ELEVATED_C = 80.0
THERMAL_HOT_C = 90.0
THERMAL_CRITICAL_C = 98.0


@dataclass(frozen=True, slots=True)
class CpuTemperature:
    value: float
    metric: str
    source: str
    label: str  # human description of what the sensor actually is


def cpu_temperature(components: Mapping[str, Component]) -> CpuTemperature | None:
    """Best available CPU-area temperature.

    Prefers the true CPU package sensor (LibreHardwareMonitor). Falls back to the hottest ACPI
    thermal zone, explicitly labelled as such: it is a firmware platform sensor near the SoC, not
    the CPU's own digital thermal sensor.
    """
    cpu = components.get("cpu")
    if cpu is not None:
        r = cpu.reading("cpu.temperature_c")
        if r is not None and r.numeric is not None:
            return CpuTemperature(r.numeric, r.key, r.source, "CPU package sensor")
    sensors = components.get("thermal_sensors")
    if sensors is not None:
        zones = [r for r in sensors.readings("thermal.zone_temperature_c") if r.numeric is not None]
        if zones:
            hottest = max(zones, key=lambda r: r.numeric or 0.0)
            zone = hottest.labels.get("zone", "zone")
            return CpuTemperature(
                hottest.numeric or 0.0, hottest.key, hottest.source, f"ACPI thermal zone {zone}"
            )
    return None


def thermal_band(temp_c: float | None) -> str:
    if temp_c is None:
        return "unknown"
    if temp_c >= THERMAL_CRITICAL_C:
        return "critical"
    if temp_c >= THERMAL_HOT_C:
        return "hot"
    if temp_c >= THERMAL_ELEVATED_C:
        return "elevated"
    return "normal"


def _load_state(usage: float | None) -> str:
    if usage is None:
        return "unknown"
    if usage >= 85:
        return "busy"
    if usage >= 10:
        return "active"
    return "idle"


def derive_state(component: Component, components: Mapping[str, Component]) -> str:
    t = component.component_type
    if t is T.CPU:
        sensors = components.get("thermal_sensors")
        limits = [r.numeric for r in sensors.readings("thermal.passive_limit_percent")] if sensors else []
        if any(v is not None and v < 100 for v in limits):
            return "throttling"
        return _load_state(component.value("cpu.usage_percent"))
    if t is T.GPU:
        return _load_state(component.value("gpu.usage_percent"))
    if t is T.MEMORY:
        usage = component.value("memory.usage_percent")
        if usage is None:
            return "unknown"
        return "critical" if usage >= 95 else "elevated" if usage >= 85 else "normal"
    if t is T.BATTERY:
        r = component.reading("battery.charging_state")
        if r is None:
            return "unknown"
        if not r.available:
            return "absent" if r.reason and "No battery" in r.reason else "unknown"
        return str(r.value)
    if t is T.DISK:
        active = component.value("disk.active_time_percent")
        health = component.reading("disk.health_status")
        if health is not None and health.value not in (None, "Healthy"):
            return f"health_{str(health.value).lower()}"
        return "unknown" if active is None else "active" if active >= 5 else "idle"
    if t is T.STORAGE:
        rate = (component.value("disk.read_bytes_per_sec") or 0) + (
            component.value("disk.write_bytes_per_sec") or 0
        )
        return "active" if rate > 1_000_000 else "idle"
    if t is T.NETWORK_ADAPTER:
        up = component.reading("network.link_up")
        if up is None or not up.available:
            return "disconnected"
        return "up" if up.value else "down"
    if t is T.NETWORK:
        nics = [c for c in components.values() if c.component_type is T.NETWORK_ADAPTER]
        return "connected" if any(c.current_state == "up" for c in nics) else "disconnected"
    if t is T.THERMAL_SENSORS:
        temp = cpu_temperature(components)
        return thermal_band(temp.value if temp else None)
    if t is T.FAN:
        rpm = component.value("fan.speed_rpm")
        if rpm is None:
            return "unobservable"
        return "spinning" if rpm > 0 else "stopped"
    if t is T.POWER:
        src = component.reading("power.source")
        return str(src.value) if src is not None and src.available else "unknown"
    if t is T.DISPLAY:
        return "on" if component.value("display.brightness_percent") is not None else "unknown"
    if t in (T.OPERATING_SYSTEM, T.AGENT):
        return "running" if component.telemetry else "unknown"
    if t is T.COOLING:
        sensors = components.get("thermal_sensors")
        return sensors.current_state if sensors else "unknown"
    return "passive"
