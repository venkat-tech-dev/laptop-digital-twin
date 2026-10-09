from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from types import ModuleType
from typing import Any

import psutil as _psutil

from app.contracts import MetricKind
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC = "psutil (Win32 GetIfTable2)"
LINK_STATE_REFRESH_S = 15.0

_RATES = (
    ("network.rx_bytes_per_sec", "bytes_recv", "B/s"),
    ("network.tx_bytes_per_sec", "bytes_sent", "B/s"),
    ("network.rx_packets_per_sec", "packets_recv", "packets/s"),
    ("network.tx_packets_per_sec", "packets_sent", "packets/s"),
    ("network.errors_per_sec", "errin", "errors/s"),
    ("network.drops_per_sec", "dropin", "drops/s"),
)


class NetworkProvider(TelemetryProvider):
    """Throughput and link state per network interface.

    Only interfaces that are physical adapters (from hardware discovery) are reported individually;
    totals are the sum over those adapters so virtual switches/VPN loopbacks do not double count.
    """

    name = "network"
    component = "network"

    def __init__(
        self,
        interval_ms: int,
        physical_interfaces: Iterable[str] | None = None,
        psutil: ModuleType = _psutil,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(interval_ms)
        self._ps = psutil
        self._clock = clock
        self._physical = set(physical_interfaces or [])
        self._prev: dict[str, Any] | None = None
        self._prev_t = 0.0
        self._stats: dict[str, Any] = {}
        self._stats_at: float | None = None

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec(m, u, SRC) for m, _, u in _RATES[:2]]

    def _selected(self, names: Iterable[str]) -> list[str]:
        names = list(names)
        if self._physical:
            chosen = [n for n in names if n in self._physical]
            if chosen:
                return chosen
        return [n for n in names if "loopback" not in n.lower()]

    def collect(self) -> list[Reading]:
        now = self._clock()
        counters = self._ps.net_io_counters(pernic=True)
        # Link state/speed changes rarely and net_if_stats is the costliest call (adapter enumeration).
        if self._stats_at is None or now - self._stats_at >= LINK_STATE_REFRESH_S:
            self._stats, self._stats_at = self._ps.net_if_stats(), now
        stats = self._stats
        nics = self._selected(counters.keys())
        if not nics:
            return [self._na(m, u, SRC, "No network adapter present") for m, _, u in _RATES[:2]]

        out: list[Reading] = []
        for nic in nics:
            st = stats.get(nic)
            labels = {"nic": nic}
            if st is None:
                out.append(
                    self._na("network.link_up", "bool", SRC, "Adapter disconnected or disabled", labels)
                )
                continue
            out.append(self._r("network.link_up", bool(st.isup), "bool", SRC, labels=labels))
            if st.isup and st.speed > 0:
                out.append(self._r("network.link_speed_mbps", int(st.speed), "Mbps", SRC, labels=labels))
            else:
                out.append(self._na("network.link_speed_mbps", "Mbps", SRC, "Link down", labels))

        prev, prev_t = self._prev, self._prev_t
        self._prev, self._prev_t = counters, now
        if prev is None or now <= prev_t:
            return out + [self._na(m, u, SRC, "Rate needs two samples (warming up)") for m, _, u in _RATES]
        dt = now - prev_t
        totals = dict.fromkeys((m for m, _, _ in _RATES), 0.0)
        for nic in nics:
            cur, old = counters.get(nic), prev.get(nic)
            if cur is None or old is None:
                continue
            for metric, attr, unit in _RATES:
                rate = max(0.0, (getattr(cur, attr) - getattr(old, attr)) / dt)
                totals[metric] += rate
                out.append(self._r(metric, rate, unit, SRC, kind=MetricKind.DERIVED, labels={"nic": nic}))
        for metric, _, unit in _RATES:
            out.append(self._r(metric, totals[metric], unit, SRC, kind=MetricKind.DERIVED))
        return out
