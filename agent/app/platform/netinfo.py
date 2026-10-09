"""Network health primitives that need no administrator rights.

* Default IPv4 route (gateway + interface index): ``GetIpForwardTable``.
* ICMP echo latency / loss: ``IcmpSendEcho`` (iphlpapi; no raw sockets, no admin).
* Connectivity: Windows Network List Manager (``INetworkListManager``) - the same signal as the
  taskbar "No internet" indicator. The agent sends no probe traffic of its own for this.
"""

from __future__ import annotations

import ctypes
import socket
import struct
import sys
from ctypes import wintypes
from dataclasses import dataclass

from app.errors import DriverUnavailableError, HardwareMissingError, TelemetryError

_NLM_CLSID = "{DCB00C01-570F-4A9B-8D69-199FDBA5723B}"
_IP_SUCCESS = 0


@dataclass(frozen=True, slots=True)
class Route:
    gateway: str
    if_index: int
    metric: int


@dataclass(frozen=True, slots=True)
class PingResult:
    sent: int
    received: int
    avg_ms: float | None
    min_ms: float | None
    max_ms: float | None

    @property
    def loss_percent(self) -> float:
        return round(100.0 * (self.sent - self.received) / self.sent, 1) if self.sent else 100.0


@dataclass(frozen=True, slots=True)
class Connectivity:
    connected: bool
    internet: bool


def _require_windows() -> None:
    if sys.platform != "win32":
        raise DriverUnavailableError("Requires Windows")


def parse_forward_table(buf: bytes) -> list[Route]:
    """MIB_IPFORWARDTABLE: DWORD count + MIB_IPFORWARDROW[14 x DWORD]."""
    n = struct.unpack_from("<I", buf, 0)[0]
    routes = []
    for i in range(n):
        dest, mask, _policy, next_hop, if_index, _type, _proto, _age, _nhas, metric = struct.unpack_from(
            "<10I", buf, 4 + i * 56
        )
        if dest == 0 and mask == 0 and next_hop != 0:
            routes.append(Route(socket.inet_ntoa(struct.pack("<I", next_hop)), if_index, metric))
    return sorted(routes, key=lambda r: r.metric)


def default_route() -> Route:
    _require_windows()
    iphlp = ctypes.WinDLL("iphlpapi")
    size = wintypes.ULONG(0)
    iphlp.GetIpForwardTable(None, ctypes.byref(size), True)
    buf = ctypes.create_string_buffer(size.value + 1024)
    size = wintypes.ULONG(len(buf))
    rc = iphlp.GetIpForwardTable(buf, ctypes.byref(size), True)
    if rc != 0:
        raise TelemetryError(f"GetIpForwardTable failed ({rc})")
    routes = parse_forward_table(buf.raw)
    if not routes:
        raise HardwareMissingError("No default route (not connected to a network)")
    return routes[0]


class _IpOptions(ctypes.Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.c_void_p),
    ]


class _EchoReply(ctypes.Structure):
    _fields_ = [
        ("Address", ctypes.c_ulong),
        ("Status", ctypes.c_ulong),
        ("RoundTripTime", ctypes.c_ulong),
        ("DataSize", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort),
        ("Data", ctypes.c_void_p),
        ("Options", _IpOptions),
    ]


def ping(host: str, count: int, timeout_ms: int) -> PingResult:
    """ICMP echo via IcmpSendEcho (32-byte payload). Blocking; bounded by count x timeout."""
    _require_windows()
    try:
        addr = struct.unpack("<I", socket.inet_aton(socket.gethostbyname(host)))[0]
    except OSError as exc:
        raise TelemetryError(f"Cannot resolve {host}") from exc
    iphlp = ctypes.WinDLL("iphlpapi")
    iphlp.IcmpCreateFile.restype = wintypes.HANDLE
    iphlp.IcmpSendEcho.argtypes = (
        wintypes.HANDLE,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ushort,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    iphlp.IcmpCloseHandle.argtypes = (wintypes.HANDLE,)
    handle = iphlp.IcmpCreateFile()
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise TelemetryError("IcmpCreateFile failed")
    payload = b"ldt-agent-latency-probe-00000000"
    reply_size = ctypes.sizeof(_EchoReply) + len(payload) + 8
    times: list[float] = []
    try:
        for _ in range(count):
            reply = ctypes.create_string_buffer(reply_size)
            n = iphlp.IcmpSendEcho(handle, addr, payload, len(payload), None, reply, reply_size, timeout_ms)
            if n:
                echo = _EchoReply.from_buffer_copy(reply.raw[: ctypes.sizeof(_EchoReply)])
                if echo.Status == _IP_SUCCESS:
                    times.append(float(echo.RoundTripTime))
    finally:
        iphlp.IcmpCloseHandle(handle)
    return PingResult(
        count,
        len(times),
        round(sum(times) / len(times), 1) if times else None,
        min(times) if times else None,
        max(times) if times else None,
    )


def connectivity() -> Connectivity:
    _require_windows()
    import win32com.client

    try:
        nlm = win32com.client.Dispatch(_NLM_CLSID)
        return Connectivity(bool(nlm.IsConnected), bool(nlm.IsConnectedToInternet))
    except Exception as exc:
        raise TelemetryError(f"Network List Manager not available: {exc}") from exc
