from __future__ import annotations

import ipaddress
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psutil

from app.contracts import DeviceEvent, EventSeverity
from app.errors import HardwareMissingError, TelemetryError
from app.platform import netinfo
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC_NLM = "Windows Network List Manager"
SRC_ROUTE = "Windows IP Helper (default route)"
SRC_ICMP = "ICMP echo (IcmpSendEcho)"


def adapter_for_gateway(gateway: str, addrs: dict[str, list[Any]]) -> tuple[str, str] | None:
    """(interface name, IPv4) whose subnet contains the gateway."""
    gw = ipaddress.ip_address(gateway)
    for nic, entries in addrs.items():
        for a in entries:
            if getattr(a, "family", None) != 2 or not a.address or not a.netmask:  # AF_INET
                continue
            try:
                net = ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False)
            except ValueError:
                continue
            if gw in net:
                return nic, a.address
    return None


class NetworkHealthProvider(TelemetryProvider):
    """Device-network vs internet connectivity, active adapter, gateway latency/loss.

    * ``network.device_connected`` - a network is attached (LAN/Wi-Fi), per Windows.
    * ``network.internet_connected`` - Windows confirms internet reachability.
    These are reported separately: a laptop can be on Wi-Fi without internet.
    """

    name = "network_health"
    component = "network"
    lane = "net"
    timeout_s = 30.0

    def __init__(
        self,
        interval_ms: int,
        *,
        adapter_types: dict[str, str] | None = None,
        probe_host: str = "",
        icmp_count: int = 4,
        icmp_timeout_ms: int = 1000,
        include_ip: bool = False,
        backend_latency_ms: Callable[[], float | None] = lambda: None,
        connectivity: Callable[[], netinfo.Connectivity] = netinfo.connectivity,
        route: Callable[[], netinfo.Route] = netinfo.default_route,
        ping: Callable[[str, int, int], netinfo.PingResult] = netinfo.ping,
        addrs: Callable[[], dict[str, list[Any]]] = psutil.net_if_addrs,
    ) -> None:
        super().__init__(interval_ms)
        self._types = adapter_types or {}
        self._probe = probe_host
        self._count = icmp_count
        self._timeout = icmp_timeout_ms
        self._include_ip = include_ip
        self._backend_latency = backend_latency_ms
        self._connectivity = connectivity
        self._route = route
        self._ping = ping
        self._addrs = addrs
        self._prev: netinfo.Connectivity | None = None
        self._events: list[DeviceEvent] = []
        self.on_internet_restored: Callable[[], None] | None = None

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [
            MetricSpec("network.device_connected", "bool", SRC_NLM),
            MetricSpec("network.internet_connected", "bool", SRC_NLM),
        ]

    def pop_events(self) -> list[DeviceEvent]:
        out, self._events = self._events, []
        return out

    def collect(self) -> list[Reading]:
        out: list[Reading] = []
        try:
            conn = self._connectivity()
            out += [
                self._r("network.device_connected", conn.connected, "bool", SRC_NLM),
                self._r("network.internet_connected", conn.internet, "bool", SRC_NLM),
            ]
            self._transition(conn)
        except TelemetryError as exc:
            out += [self._na(m.metric, m.unit, SRC_NLM, exc.detail) for m in self.declared_metrics]

        try:
            route = self._route()
        except HardwareMissingError as exc:
            reason = exc.detail
            return [
                *out,
                self._na("network.active_adapter", "text", SRC_ROUTE, reason),
                self._na("network.gateway_latency_ms", "ms", SRC_ICMP, reason),
            ]
        except TelemetryError as exc:
            return [*out, self._na("network.active_adapter", "text", SRC_ROUTE, exc.detail)]

        match = adapter_for_gateway(route.gateway, self._addrs())
        if match is None:
            out.append(
                self._na(
                    "network.active_adapter", "text", SRC_ROUTE, "Adapter for default route not identified"
                )
            )
        else:
            nic, ip = match
            out.append(self._r("network.active_adapter", nic, "text", SRC_ROUTE))
            kind = self._types.get(nic)
            if kind:
                out.append(self._r("network.connection_type", kind, "state", SRC_ROUTE))
            else:
                out.append(
                    self._na("network.connection_type", "state", SRC_ROUTE, "Adapter type not in inventory")
                )
            if self._include_ip:
                out.append(self._r("network.ipv4_address", ip, "text", SRC_ROUTE, labels={"nic": nic}))
        out += self._probe_readings(route.gateway, "gateway")
        if self._probe:
            out += self._probe_readings(self._probe, "internet")
        latency = self._backend_latency()
        if latency is None:
            out.append(
                self._na("network.backend_latency_ms", "ms", "Agent API client", "No request completed yet")
            )
        else:
            out.append(
                self._r("network.backend_latency_ms", latency, "ms", "Agent API client (last request)")
            )
        return out

    def _probe_readings(self, host: str, target: str) -> list[Reading]:
        try:
            res = self._ping(host, self._count, self._timeout)
        except TelemetryError as exc:
            return [self._na(f"network.{target}_latency_ms", "ms", SRC_ICMP, exc.detail)]
        out = [self._r(f"network.{target}_packet_loss_percent", res.loss_percent, "percent", SRC_ICMP)]
        if res.avg_ms is None:
            out.append(
                self._na(
                    f"network.{target}_latency_ms",
                    "ms",
                    SRC_ICMP,
                    "No echo reply (host unreachable or ICMP blocked)",
                )
            )
        else:
            out.append(self._r(f"network.{target}_latency_ms", res.avg_ms, "ms", SRC_ICMP))
        return out

    def _transition(self, conn: netinfo.Connectivity) -> None:
        prev, self._prev = self._prev, conn
        if prev is None:
            return
        now = datetime.now(UTC)
        if prev.connected != conn.connected:
            self._events.append(
                DeviceEvent(
                    type="network_connected" if conn.connected else "network_disconnected",
                    severity=EventSeverity.INFO if conn.connected else EventSeverity.WARNING,
                    timestamp=now,
                    source=SRC_NLM,
                    message="Network connected" if conn.connected else "Network disconnected",
                )
            )
        if prev.internet != conn.internet:
            self._events.append(
                DeviceEvent(
                    type="internet_restored" if conn.internet else "internet_lost",
                    severity=EventSeverity.INFO if conn.internet else EventSeverity.WARNING,
                    timestamp=now,
                    source=SRC_NLM,
                    message="Internet connectivity restored"
                    if conn.internet
                    else "Internet connectivity lost",
                )
            )
            if conn.internet and self.on_internet_restored is not None:
                self.on_internet_restored()
