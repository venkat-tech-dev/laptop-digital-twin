from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psutil

from app.contracts import ProcessInfo, ProcessSnapshot
from app.errors import TelemetryError
from app.platform.ntprocess import RawProcess
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC = "Windows NtQuerySystemInformation (SystemProcessInformation)"

# Privacy: by default only image name, PID, state, start time, handle/socket counts and resource usage
# are collected. Image path (profile name redacted), owner and publisher are opt-in (details_enabled).
# Command lines, open files, environment variables, window titles and socket addresses are never read.

_FILETIME_EPOCH_OFFSET_S = 11_644_473_600


def filetime_to_datetime(ft: int) -> datetime | None:
    if ft <= 0:
        return None
    return datetime.fromtimestamp(ft / 1e7 - _FILETIME_EPOCH_OFFSET_S, UTC)


class ProcessProvider(TelemetryProvider):
    """Top processes by CPU, memory, GPU and disk I/O. Read-only: never signals or terminates."""

    name = "process"
    lane = "proc"
    timeout_s = 20.0
    component = "os"

    def __init__(
        self,
        interval_ms: int,
        top_n: int,
        snapshot: Callable[[], list[RawProcess]],
        gpu_usage_by_pid: Callable[[], dict[int, float]] | None = None,
        total_memory_bytes: int | None = None,
        cpu_count: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sockets_by_pid: Callable[[], dict[int, dict[str, int]]] | None = None,
        details: Callable[[int, int], Any] | None = None,
        details_enabled: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__(interval_ms)
        self._snapshot_fn = snapshot
        self.top_n = top_n
        self._gpu_usage = gpu_usage_by_pid
        self._clock = clock
        self._total_mem = total_memory_bytes or psutil.virtual_memory().total
        self._cpu_count = cpu_count or psutil.cpu_count(logical=True) or 1
        self._prev: dict[int, RawProcess] = {}
        self._prev_t: float | None = None
        self._snapshot: ProcessSnapshot | None = None
        self._sockets = sockets_by_pid
        self._details = details
        self._details_enabled = details_enabled

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("system.process_count", "count", SRC)]

    def pop_snapshot(self) -> ProcessSnapshot | None:
        snap, self._snapshot = self._snapshot, None
        return snap

    def collect(self) -> list[Reading]:
        try:
            raw = self._snapshot_fn()
        except TelemetryError as exc:
            return [self._na("system.process_count", "count", SRC, exc.detail)]
        now = self._clock()
        dt = (now - self._prev_t) if self._prev_t is not None else None
        gpu = self._gpu_usage() if self._gpu_usage else {}
        sockets: dict[int, dict[str, int]] | None = None
        socket_reason = "Socket table provider not configured"
        if self._sockets is not None:
            try:
                sockets = self._sockets()
            except TelemetryError as exc:
                socket_reason = exc.detail
        infos: list[ProcessInfo] = []
        threads = 0
        for proc in raw:
            threads += proc.num_threads
            if proc.pid == 0:  # System Idle Process: idle time, not a workload
                continue
            info = self._describe(proc, self._prev.get(proc.pid), dt, gpu.get(proc.pid))
            if sockets is not None:
                sock = sockets.get(proc.pid, {})
                info.tcp_established = sock.get("tcp_established", 0)
                info.tcp_listening = sock.get("tcp_listening", 0)
                info.udp_endpoints = sock.get("udp", 0)
            infos.append(info)
        self._prev = {p.pid: p for p in raw}
        self._prev_t = now
        selected = self._select(infos)
        details_on = self._details is not None and self._details_enabled()
        if details_on and self._details is not None:
            for info in selected:
                rawp = self._prev.get(info.pid)
                d = self._details(info.pid, rawp.create_time_100ns if rawp else 0)
                info.path, info.user, info.publisher = d.path, d.user, d.publisher
        unavailable = {
            "network": (
                "Per-process network throughput needs ETW kernel tracing (administrator); "
                "open socket counts are reported instead"
            ),
            **({"cpu_percent": "Rates need two snapshots (warming up)"} if dt is None else {}),
            **({"sockets": socket_reason} if sockets is None else {}),
            **(
                {} if details_on else {"details": "Path, user and publisher are opt-in (Settings -> Privacy)"}
            ),
        }
        self._snapshot = ProcessSnapshot(
            timestamp=datetime.now(UTC),
            source=SRC,
            total_processes=len(infos),
            processes=selected,
            unavailable_fields=unavailable,
            details_collected=details_on,
        )
        return [
            self._r("system.process_count", len(infos), "count", SRC),
            self._r("system.thread_count", threads, "count", SRC),
        ]

    def _describe(
        self, cur: RawProcess, prev: RawProcess | None, dt: float | None, gpu: float | None
    ) -> ProcessInfo:
        cpu = read_rate = write_rate = None
        if prev is not None and dt and dt > 0:
            cpu = max(0.0, (cur.cpu_time_100ns - prev.cpu_time_100ns) / 1e7 / dt / self._cpu_count * 100.0)
            read_rate = max(0.0, (cur.read_transfer_bytes - prev.read_transfer_bytes) / dt)
            write_rate = max(0.0, (cur.write_transfer_bytes - prev.write_transfer_bytes) / dt)
        return ProcessInfo(
            pid=cur.pid,
            name=cur.name,
            status="suspended" if cur.suspended else "running",
            cpu_percent=round(min(cpu, 100.0), 2) if cpu is not None else None,
            memory_rss_bytes=cur.working_set_bytes,
            memory_percent=round(100.0 * cur.working_set_bytes / self._total_mem, 2),
            num_threads=cur.num_threads,
            gpu_percent=round(gpu, 2) if gpu is not None else None,
            io_read_bytes_per_sec=read_rate,
            io_write_bytes_per_sec=write_rate,
            handle_count=cur.handle_count or None,
            started_at=filetime_to_datetime(cur.create_time_100ns),
        )

    def _select(self, infos: list[ProcessInfo]) -> list[ProcessInfo]:
        n = self.top_n
        keys: list[Callable[[ProcessInfo], float]] = [
            lambda p: p.cpu_percent or 0.0,
            lambda p: float(p.memory_rss_bytes or 0),
            lambda p: p.gpu_percent or 0.0,
            lambda p: (p.io_read_bytes_per_sec or 0.0) + (p.io_write_bytes_per_sec or 0.0),
        ]
        chosen: dict[int, ProcessInfo] = {}
        for key in keys:
            for p in sorted(infos, key=key, reverse=True)[:n]:
                if key(p) > 0:
                    chosen[p.pid] = p
        return sorted(chosen.values(), key=lambda p: p.cpu_percent or 0.0, reverse=True)
