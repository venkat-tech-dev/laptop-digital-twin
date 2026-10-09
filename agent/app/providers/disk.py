from __future__ import annotations

import re
import time
from collections.abc import Callable
from types import ModuleType
from typing import Any

import psutil as _psutil

from app.contracts import MetricKind
from app.errors import TelemetryError
from app.platform.pdh import CounterReader, PdhCounterSet
from app.platform.wmi import WmiQueryable
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC_IO = "psutil (IOCTL_DISK_PERFORMANCE)"
SRC_PDH = "Windows Performance Counter (PhysicalDisk)"
SRC_USAGE = "psutil (GetDiskFreeSpaceEx)"
SRC_HEALTH = "Windows Storage Management (MSFT_PhysicalDisk)"

_PDH_PATHS = {
    "idle": r"\PhysicalDisk(*)\% Idle Time",
    "read_latency": r"\PhysicalDisk(*)\Avg. Disk sec/Read",
    "write_latency": r"\PhysicalDisk(*)\Avg. Disk sec/Write",
    "queue": r"\PhysicalDisk(*)\Current Disk Queue Length",
}
_PDH_INSTANCE = re.compile(r"^(\d+)")
_HEALTH = {0: "Healthy", 1: "Warning", 2: "Unhealthy", 5: "Unknown"}


def pdh_instance_to_disk(instance: str) -> str | None:
    """PDH instance "0 C:" -> psutil/Windows name "PhysicalDrive0"."""
    match = _PDH_INSTANCE.match(instance.strip())
    return f"PhysicalDrive{match.group(1)}" if match else None


class DiskProvider(TelemetryProvider):
    """Disk throughput, IOPS, active time, latency and queue length per physical disk."""

    name = "disk"
    component = "storage"

    def __init__(
        self,
        interval_ms: int,
        psutil: ModuleType = _psutil,
        counter_factory: Callable[[dict[str, str]], CounterReader] = PdhCounterSet,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(interval_ms)
        self._ps = psutil
        self._clock = clock
        self._prev: dict[str, Any] | None = None
        self._prev_t = 0.0
        self._counters: CounterReader | None = None
        self._counter_error: str | None = None
        try:
            self._counters = counter_factory(_PDH_PATHS)
        except TelemetryError as exc:
            self._counter_error = exc.detail

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [
            MetricSpec("disk.read_bytes_per_sec", "B/s", SRC_IO),
            MetricSpec("disk.write_bytes_per_sec", "B/s", SRC_IO),
        ]

    def collect(self) -> list[Reading]:
        return self._throughput() + self._pdh()

    def _throughput(self) -> list[Reading]:
        now = self._clock()
        counters = self._ps.disk_io_counters(perdisk=True)
        if not counters:
            return [
                self._na(m, "B/s", SRC_IO, "Disk performance counters disabled or no disks found")
                for m in ("disk.read_bytes_per_sec", "disk.write_bytes_per_sec")
            ]
        prev, prev_t = self._prev, self._prev_t
        self._prev, self._prev_t = counters, now
        metrics = (
            ("disk.read_bytes_per_sec", "read_bytes", "B/s"),
            ("disk.write_bytes_per_sec", "write_bytes", "B/s"),
            ("disk.read_ops_per_sec", "read_count", "ops/s"),
            ("disk.write_ops_per_sec", "write_count", "ops/s"),
        )
        if prev is None or now <= prev_t:
            return [self._na(m, u, SRC_IO, "Rate needs two samples (warming up)") for m, _, u in metrics]
        dt = now - prev_t
        out: list[Reading] = []
        totals = dict.fromkeys((m for m, _, _ in metrics), 0.0)
        for disk, cur in counters.items():
            old = prev.get(disk)
            if old is None:
                continue
            for metric, attr, unit in metrics:
                delta = getattr(cur, attr) - getattr(old, attr)
                rate = max(0.0, delta / dt)  # counters reset on driver reload -> clamp at 0
                totals[metric] += rate
                out.append(
                    self._r(metric, rate, unit, SRC_IO, kind=MetricKind.DERIVED, labels={"disk": disk})
                )
        for metric, _, unit in metrics:
            out.append(self._r(metric, totals[metric], unit, SRC_IO, kind=MetricKind.DERIVED))
        return out

    def _pdh(self) -> list[Reading]:
        specs = (
            ("disk.active_time_percent", "percent"),
            ("disk.avg_read_latency_ms", "ms"),
            ("disk.avg_write_latency_ms", "ms"),
            ("disk.queue_length", "count"),
        )
        if self._counters is None:
            return [self._na(m, u, SRC_PDH, self._counter_error or "unavailable") for m, u in specs]
        try:
            values = self._counters.collect()
        except TelemetryError as exc:
            return [Reading.failed(m, self.component, u, SRC_PDH, exc.detail) for m, u in specs]
        out: list[Reading] = []
        sources: dict[str, tuple[str, Callable[[float], float]]] = {
            "disk.active_time_percent": ("idle", lambda v: max(0.0, 100.0 - v)),
            "disk.avg_read_latency_ms": ("read_latency", lambda v: v * 1000.0),
            "disk.avg_write_latency_ms": ("write_latency", lambda v: v * 1000.0),
            "disk.queue_length": ("queue", lambda v: v),
        }
        for metric, unit in specs:
            key, transform = sources[metric]
            per_instance = values.get(key)
            if not isinstance(per_instance, dict):
                out.append(self._na(metric, unit, SRC_PDH, "Counter warming up"))
                continue
            for instance, raw in per_instance.items():
                if instance == "_Total":
                    continue
                disk = pdh_instance_to_disk(instance)
                if disk is None:
                    continue
                kind = MetricKind.DERIVED if metric == "disk.active_time_percent" else MetricKind.MEASURED
                out.append(self._r(metric, transform(raw), unit, SRC_PDH, kind=kind, labels={"disk": disk}))
        return out

    def close(self) -> None:
        if self._counters is not None:
            self._counters.close()


class StorageCapacityProvider(TelemetryProvider):
    """Volume capacity/free space and physical-disk health (slow tier)."""

    name = "storage_capacity"
    lane = "wmi"
    timeout_s = 20.0
    component = "storage"

    def __init__(self, interval_ms: int, wmi: WmiQueryable | None, psutil: ModuleType = _psutil) -> None:
        super().__init__(interval_ms)
        self._wmi = wmi
        self._ps = psutil

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("disk.usage_percent", "percent", SRC_USAGE)]

    def collect(self) -> list[Reading]:
        out: list[Reading] = []
        for part in self._ps.disk_partitions(all=False):
            opts = part.opts or ""
            if "cdrom" in opts or not part.fstype:
                continue
            labels = {"volume": part.mountpoint.rstrip("\\"), "fstype": part.fstype}
            try:
                usage = self._ps.disk_usage(part.mountpoint)
            except (PermissionError, OSError) as exc:
                out.append(
                    self._na(
                        "disk.usage_percent", "percent", SRC_USAGE, f"Volume not readable: {exc}", labels
                    )
                )
                continue
            out += [
                self._r(
                    "disk.total_bytes",
                    int(usage.total),
                    "bytes",
                    SRC_USAGE,
                    kind=MetricKind.STATIC,
                    labels=labels,
                ),
                self._r("disk.used_bytes", int(usage.used), "bytes", SRC_USAGE, labels=labels),
                self._r("disk.free_bytes", int(usage.free), "bytes", SRC_USAGE, labels=labels),
                self._r("disk.usage_percent", float(usage.percent), "percent", SRC_USAGE, labels=labels),
            ]
        out += self._health()
        return out

    def _health(self) -> list[Reading]:
        if self._wmi is None:
            return [self._na("disk.health_status", "state", SRC_HEALTH, "WMI unavailable")]
        try:
            rows = self._wmi.query(
                "SELECT DeviceId, FriendlyName, HealthStatus, MediaType, BusType FROM MSFT_PhysicalDisk",
                "root\\Microsoft\\Windows\\Storage",
            )
        except TelemetryError as exc:
            return [self._na("disk.health_status", "state", SRC_HEALTH, exc.detail)]
        out: list[Reading] = []
        for row in rows:
            labels = {
                "disk": f"PhysicalDrive{row.get('DeviceId')}",
                "model": str(row.get("FriendlyName", "")),
            }
            raw = row.get("HealthStatus")
            status = _HEALTH.get(int(raw), "Unknown") if raw is not None else "Unknown"
            out.append(self._r("disk.health_status", status, "state", SRC_HEALTH, labels=labels))
        return out
