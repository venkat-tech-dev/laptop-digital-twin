"""Test doubles for OS interfaces so providers can be tested without specific hardware."""

from __future__ import annotations

from collections import namedtuple
from types import SimpleNamespace
from typing import Any

from app.errors import TelemetryError

Battery = namedtuple("Battery", "percent secsleft power_plugged")
DiskIo = namedtuple("DiskIo", "read_bytes write_bytes read_count write_count")
NetIo = namedtuple("NetIo", "bytes_recv bytes_sent packets_recv packets_sent errin dropin")
NicStat = namedtuple("NicStat", "isup speed")


class FakeCounters:
    def __init__(self, values: dict[str, Any] | None = None, error: TelemetryError | None = None) -> None:
        self.values = values or {}
        self.error = error

    def collect(self) -> dict[str, Any]:
        if self.error:
            raise self.error
        return self.values

    def close(self) -> None:
        pass


def counter_factory(
    values: dict[str, Any] | None = None,
    error: TelemetryError | None = None,
    construct_error: TelemetryError | None = None,
):  # type: ignore[no-untyped-def]
    def make(_: dict[str, str]) -> FakeCounters:
        if construct_error:
            raise construct_error
        return FakeCounters(values, error)

    return make


class FakeWmi:
    def __init__(self, responses: dict[str, list[dict[str, Any]] | TelemetryError] | None = None) -> None:
        self.responses = responses or {}

    def query(self, wql: str, namespace: str = "root\\cimv2") -> list[dict[str, Any]]:
        for key, value in self.responses.items():
            if key in wql:
                if isinstance(value, TelemetryError):
                    raise value
                return value
        return []


def fake_psutil(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "POWER_TIME_UNLIMITED": -2,
        "POWER_TIME_UNKNOWN": -1,
        "sensors_battery": lambda: None,
        "cpu_percent": lambda interval=None, percpu=False: [10.0, 20.0] if percpu else 15.0,
        "virtual_memory": lambda: SimpleNamespace(total=16 * 2**30, available=4 * 2**30, percent=75.0),
        "swap_memory": lambda: SimpleNamespace(total=2**30, used=2**28, percent=25.0),
        "disk_io_counters": lambda perdisk=True: {},
        "disk_partitions": lambda all=False: [],
        "net_io_counters": lambda pernic=True: {},
        "net_if_stats": lambda: {},
        "boot_time": lambda: 1000.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)
