"""Telemetry readings -> normalized digital-twin fields (pure, deterministic).

Each field is either a measured value with its own timestamp, collection interval and provenance
(which batch/sequence produced it), or explicitly unknown / unsupported. Nothing is invented:
a field without a reading has ``value: None``; a sensor the agent reports as unavailable carries the
agent's reason. Missing metrics in a batch never reset a field - the twin keeps the latest known
reading per series (the ingest path also refuses to let an older reading overwrite a newer one).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain.components.state import cpu_temperature
from app.domain.telemetry.models import MetricReading

SYSTEM_VOLUME = "C:"


@dataclass(slots=True)
class FieldValue:
    value: Any
    unit: str
    reading: MetricReading | None  # the reading that determines timestamp/freshness/provenance
    static: bool = False
    unsupported_reason: str | None = None
    label: str | None = None  # e.g. which sensor/volume/adapter the value describes


class Readings:
    """All current readings of one twin, indexed by metric name."""

    def __init__(self, components: Iterable[Any]) -> None:
        self.by_metric: dict[str, list[MetricReading]] = defaultdict(list)
        for comp in components:
            for r in comp.telemetry.values():
                self.by_metric[r.metric].append(r)

    def one(self, metric: str, **labels: str) -> MetricReading | None:
        """The unlabeled (total) reading, else the one matching ``labels``, else the first by key."""
        rs = self.by_metric.get(metric)
        if not rs:
            return None
        if labels:
            for r in rs:
                if all(r.labels.get(k) == v for k, v in labels.items()):
                    return r
            return None
        for r in rs:
            if not r.labels:
                return r
        return sorted(rs, key=lambda r: r.key)[0]

    def all(self, metric: str) -> list[MetricReading]:
        return sorted(self.by_metric.get(metric, ()), key=lambda r: r.key)


def _value(
    r: MetricReading | None,
    unit: str | None = None,
    *,
    static: bool = False,
    label: str | None = None,
    transform: Callable[[Any], Any] | None = None,
) -> FieldValue:
    if r is None:
        return FieldValue(None, unit or "", None, static)
    if not r.available:
        return FieldValue(
            None,
            unit or r.unit,
            r,
            static,
            unsupported_reason=r.reason or "Not available on this device",
            label=label,
        )
    v = r.value
    if transform is not None and v is not None:
        v = transform(v)
    return FieldValue(v, unit or r.unit, r, static or r.kind == "static", label=label)


def _all_true(rs: list[MetricReading], unit: str = "bool") -> FieldValue:
    """True only when every instance is True (e.g. all firewall profiles on)."""
    available = [r for r in rs if r.available]
    if not rs:
        return FieldValue(None, unit, None)
    if not available:
        return _value(rs[0], unit)
    oldest = min(available, key=lambda r: r.timestamp)
    off = [r.labels.get("profile") or r.labels.get("nic") or r.key for r in available if r.value is not True]
    return FieldValue(not off, unit, oldest, label=("off: " + ", ".join(off)) if off else None)


def _sum(rs: list[MetricReading], unit: str) -> FieldValue:
    available = [r for r in rs if r.available and isinstance(r.value, (int, float))]
    if not available:
        return _value(rs[0] if rs else None, unit)
    oldest = min(available, key=lambda r: r.timestamp)
    total = sum(float(r.value) for r in available if isinstance(r.value, (int, float)))
    return FieldValue(round(total, 3), unit, oldest)


def project_fields(
    components: dict[str, Any], processes: dict[str, Any] | None, process_details_allowed: bool = True
) -> dict[str, FieldValue]:
    """Pure projection of the current readings to the normalized twin schema."""
    rd = Readings(components.values())
    f: dict[str, FieldValue] = {}

    # ---- performance
    f["performance.cpu.usage_percent"] = _value(rd.one("cpu.usage_percent"), "%")
    f["performance.cpu.frequency_mhz"] = _value(rd.one("cpu.frequency_mhz"), "MHz")
    f["performance.cpu.queue_length"] = _value(rd.one("cpu.processor_queue_length"), "count")
    temp = cpu_temperature(components)
    if temp is not None:
        f["performance.cpu.temperature_c"] = _value(_reading_for(rd, temp.metric), "°C", label=temp.label)
    else:
        f["performance.cpu.temperature_c"] = _value(rd.one("cpu.temperature_c"), "°C")
    f["performance.memory.usage_percent"] = _value(rd.one("memory.usage_percent"), "%")
    f["performance.memory.used_bytes"] = _value(rd.one("memory.used_bytes"), "bytes")
    f["performance.memory.total_bytes"] = _value(rd.one("memory.total_bytes"), "bytes", static=True)
    f["performance.memory.swap_percent"] = _value(rd.one("memory.swap_percent"), "%")
    vol = rd.one("disk.usage_percent", volume=SYSTEM_VOLUME) or rd.one("disk.usage_percent")
    f["performance.disk.usage_percent"] = _value(vol, "%", label=vol.labels.get("volume") if vol else None)
    free = rd.one("disk.free_bytes", volume=SYSTEM_VOLUME) or rd.one("disk.free_bytes")
    f["performance.disk.free_bytes"] = _value(free, "bytes")
    f["performance.disk.read_bytes_per_sec"] = _value(rd.one("disk.read_bytes_per_sec"), "B/s")
    f["performance.disk.write_bytes_per_sec"] = _value(rd.one("disk.write_bytes_per_sec"), "B/s")
    active = rd.all("disk.active_time_percent")
    busiest = max((r for r in active if r.available), key=lambda r: float(r.value or 0), default=None)
    f["performance.disk.active_time_percent"] = _value(busiest or (active[0] if active else None), "%")
    gpu = rd.all("gpu.usage_percent")
    gpu_busiest = max((r for r in gpu if r.available), key=lambda r: float(r.value or 0), default=None)
    f["performance.gpu.usage_percent"] = _value(gpu_busiest or (gpu[0] if gpu else None), "%")

    # ---- storage health (drive)
    f["storage.wear_percent"] = _value(rd.one("disk.wear_percent"), "%")
    f["storage.smart_temperature_c"] = _value(rd.one("disk.smart_temperature_c"), "°C")
    f["storage.smart_critical_warning"] = _value(
        rd.one("disk.critical_warning"), "bool", transform=lambda v: int(v) != 0
    )
    health = rd.one("disk.health_status")
    f["storage.health_ok"] = _value(
        health,
        "bool",
        transform=lambda v: str(v) == "Healthy",
        label=str(health.value) if health and health.available else None,
    )

    # ---- network
    f["network.device_connected"] = _value(rd.one("network.device_connected"), "bool")
    f["network.internet_connected"] = _value(rd.one("network.internet_connected"), "bool")
    f["network.connection_type"] = _value(rd.one("network.connection_type"), "state")
    f["network.active_adapter"] = _value(rd.one("network.active_adapter"), "text")
    f["network.rx_bytes_per_sec"] = _value(rd.one("network.rx_bytes_per_sec"), "B/s")
    f["network.tx_bytes_per_sec"] = _value(rd.one("network.tx_bytes_per_sec"), "B/s")
    f["network.gateway_latency_ms"] = _value(rd.one("network.gateway_latency_ms"), "ms")
    f["network.packet_loss_percent"] = _value(rd.one("network.gateway_packet_loss_percent"), "%")

    # ---- battery
    f["battery.charge_percent"] = _value(rd.one("battery.charge_percent"), "%")
    f["battery.charging_state"] = _value(rd.one("battery.charging_state"), "state")
    f["battery.power_source"] = _value(rd.one("power.source"), "state")
    f["battery.health_percent"] = _value(rd.one("battery.health_percent"), "%")
    f["battery.time_remaining_s"] = _value(rd.one("battery.time_remaining_s"), "s")
    f["battery.cycle_count"] = _value(rd.one("battery.cycle_count"), "count")

    # ---- thermal
    if temp is not None:
        f["thermal.temperature_c"] = _value(_reading_for(rd, temp.metric), "°C", label=temp.label)
    else:
        f["thermal.temperature_c"] = _value(rd.one("thermal.zone_temperature_c"), "°C")
    limits = [r for r in rd.all("thermal.passive_limit_percent") if r.available]
    if limits:
        worst = min(limits, key=lambda r: float(r.value or 100))
        f["thermal.throttling"] = FieldValue(float(worst.value or 100) < 100, "bool", worst)
    else:
        f["thermal.throttling"] = _value(rd.one("thermal.passive_limit_percent"), "bool")
    fans = [r for r in rd.all("fan.speed_rpm") if r.available]
    f["thermal.fan_rpm"] = _value(
        max(fans, key=lambda r: float(r.value or 0)) if fans else rd.one("fan.speed_rpm"), "rpm"
    )

    # ---- security
    f["security.realtime_protection"] = _value(rd.one("security.defender_realtime_enabled"), "bool")
    f["security.antivirus_enabled"] = _value(rd.one("security.antivirus_enabled"), "bool")
    f["security.antivirus_up_to_date"] = _value(rd.one("security.antivirus_up_to_date"), "bool")
    f["security.firewall_enabled"] = _all_true(rd.all("security.firewall_enabled"))
    f["security.secure_boot"] = _value(rd.one("security.secure_boot_enabled"), "bool")
    f["security.tpm_present"] = _value(rd.one("security.tpm_present"), "bool")
    f["security.signature_age_days"] = _value(rd.one("security.defender_signature_age_days"), "days")

    # ---- operating system
    f["operating_system.uptime_s"] = _value(rd.one("system.uptime_s"), "s")
    f["operating_system.process_count"] = _value(rd.one("system.process_count"), "count")
    f["operating_system.updates_pending"] = _value(rd.one("system.updates_pending"), "count")
    f["operating_system.reboot_required"] = _value(rd.one("system.update_reboot_required"), "bool")
    f["operating_system.app_crashes_24h"] = _value(rd.one("system.app_crashes_24h"), "count")

    # ---- applications (process names only where the deployment exposes them)
    procs = (processes or {}).get("processes") or []
    if process_details_allowed and procs:
        by_cpu = max(procs, key=lambda p: p.get("cpu_percent") or 0)
        by_mem = max(procs, key=lambda p: p.get("memory_rss_bytes") or 0)
        f["applications.top_cpu_process"] = FieldValue(by_cpu.get("name"), "text", None, static=True)
        f["applications.top_memory_process"] = FieldValue(by_mem.get("name"), "text", None, static=True)
    else:
        f["applications.top_cpu_process"] = FieldValue(None, "text", None)
        f["applications.top_memory_process"] = FieldValue(None, "text", None)
    return f


def _reading_for(rd: Readings, metric_key: str) -> MetricReading | None:
    """A specific series by its metric key (``metric{label=...}``)."""
    metric = metric_key.split("{", 1)[0]
    for r in rd.by_metric.get(metric, ()):
        if r.key == metric_key:
            return r
    return rd.one(metric)


def reading_age_s(r: MetricReading | None, now: datetime) -> float | None:
    return None if r is None else max(0.0, (now - r.timestamp).total_seconds())
