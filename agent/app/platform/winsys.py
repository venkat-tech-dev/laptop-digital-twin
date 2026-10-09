"""Windows system facts that need no administrator rights.

* CPU core topology (performance / efficiency cores) - ``GetLogicalProcessorInformationEx``.
* Secure Boot state - registry ``HKLM\\SYSTEM\\CurrentControlSet\\Control\\SecureBoot\\State``.
* TPM presence and version - TPM Base Services ``Tbsi_GetDeviceInfo``.
* Per-process socket counts - ``GetExtendedTcpTable`` / ``GetExtendedUdpTable`` (no addresses kept).
* WHEA hardware-error events - Windows event log ``System`` channel.
* Optional process details (image path, owner, publisher) - opt-in only, see ``ProcessDetails``.
"""

from __future__ import annotations

import contextlib
import ctypes
import re
import struct
import sys
import xml.etree.ElementTree as ET
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.errors import DriverUnavailableError, HardwareMissingError, PermissionDeniedError

_IS_WIN = sys.platform == "win32"


def _require_windows() -> None:
    if not _IS_WIN:
        raise DriverUnavailableError("Requires Windows")


# --------------------------------------------------------------------------- CPU topology
def parse_core_relations(buf: bytes) -> list[dict[str, Any]]:
    """Parse ``SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX`` records of RelationProcessorCore."""
    cores: list[dict[str, Any]] = []
    offset = 0
    while offset + 8 <= len(buf):
        relationship, size = struct.unpack_from("<II", buf, offset)
        if size == 0:
            break
        if relationship == 0:  # RelationProcessorCore
            flags, efficiency = struct.unpack_from("<BB", buf, offset + 8)
            group_count = struct.unpack_from("<H", buf, offset + 30)[0]
            logical: list[int] = []
            for g in range(group_count):
                mask, group = struct.unpack_from("<QH", buf, offset + 32 + g * 16)
                logical.extend(group * 64 + bit for bit in range(64) if mask >> bit & 1)
            cores.append({"efficiency_class": efficiency, "smt": bool(flags & 1), "logical": sorted(logical)})
        offset += size
    return cores


def cpu_topology() -> dict[str, Any]:
    """Performance/efficiency core split. ``hybrid`` is False when every core has the same class."""
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    needed = wintypes.DWORD(0)
    kernel32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(needed))
    if needed.value == 0:
        raise DriverUnavailableError("GetLogicalProcessorInformationEx returned no data")
    buf = ctypes.create_string_buffer(needed.value)
    if not kernel32.GetLogicalProcessorInformationEx(0, buf, ctypes.byref(needed)):
        raise DriverUnavailableError(f"GetLogicalProcessorInformationEx failed ({ctypes.get_last_error()})")
    cores = parse_core_relations(buf.raw[: needed.value])
    return summarize_topology(cores)


def summarize_topology(cores: list[dict[str, Any]]) -> dict[str, Any]:
    classes = sorted({c["efficiency_class"] for c in cores})
    hybrid = len(classes) > 1
    top = classes[-1] if classes else 0
    out_cores = [
        {
            "index": i,
            "type": ("performance" if c["efficiency_class"] == top else "efficient") if hybrid else "uniform",
            "efficiency_class": c["efficiency_class"],
            "logical": c["logical"],
        }
        for i, c in enumerate(cores)
    ]
    p = [c for c in out_cores if c["type"] == "performance"]
    e = [c for c in out_cores if c["type"] == "efficient"]
    return {
        "source": "Windows GetLogicalProcessorInformationEx (EfficiencyClass)",
        "hybrid": hybrid,
        "physical_cores": len(out_cores),
        "performance_cores": len(p) if hybrid else None,
        "efficient_cores": len(e) if hybrid else None,
        "performance_logical": sorted(i for c in p for i in c["logical"]),
        "efficient_logical": sorted(i for c in e for i in c["logical"]),
        "cores": out_cores,
    }


# --------------------------------------------------------------------------- Secure Boot / TPM
def secure_boot_enabled() -> bool:
    _require_windows()
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\SecureBoot\State"
        ) as k:
            value, _ = winreg.QueryValueEx(k, "UEFISecureBootEnabled")
    except FileNotFoundError as exc:
        raise HardwareMissingError(
            "Secure Boot state not present (legacy BIOS boot or unsupported firmware)"
        ) from exc
    except PermissionError as exc:
        raise PermissionDeniedError("Secure Boot registry state not readable") from exc
    return bool(value)


class _TpmDeviceInfo(ctypes.Structure):
    _fields_ = [
        ("structVersion", ctypes.c_uint32),
        ("tpmVersion", ctypes.c_uint32),
        ("tpmInterfaceType", ctypes.c_uint32),
        ("tpmImpRevision", ctypes.c_uint32),
    ]


_TBS_E_TPM_NOT_FOUND = 0x8028400F


def tpm_info() -> dict[str, Any]:
    """TPM presence and spec version via TPM Base Services (no admin needed)."""
    _require_windows()
    try:
        tbs = ctypes.WinDLL("tbs")
    except OSError as exc:
        raise DriverUnavailableError("tbs.dll not available") from exc
    info = _TpmDeviceInfo()
    rc = tbs.Tbsi_GetDeviceInfo(ctypes.sizeof(info), ctypes.byref(info)) & 0xFFFFFFFF
    if rc == _TBS_E_TPM_NOT_FOUND:
        return {"present": False, "version": None}
    if rc != 0:
        raise DriverUnavailableError(f"Tbsi_GetDeviceInfo failed: 0x{rc:08X}")
    version = {1: "1.2", 2: "2.0"}.get(info.tpmVersion, str(info.tpmVersion))
    return {"present": True, "version": version, "interface_type": info.tpmInterfaceType}


# --------------------------------------------------------------------------- sockets per process
_AF_INET, _AF_INET6 = 2, 23
_TCP_TABLE_OWNER_PID_ALL = 5
_UDP_TABLE_OWNER_PID = 1
_TCP_ESTABLISHED, _TCP_LISTEN = 5, 2


def _table(fn: Any, family: int, table_class: int) -> bytes:
    size = wintypes.DWORD(0)
    fn(None, ctypes.byref(size), False, family, table_class, 0)
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wintypes.DWORD(len(buf))
        rc = fn(buf, ctypes.byref(size), False, family, table_class, 0)
        if rc == 0:
            return buf.raw
        if rc != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise DriverUnavailableError(f"IP Helper table query failed ({rc})")
    raise DriverUnavailableError("IP Helper table kept growing")


def parse_socket_tables(tcp4: bytes, tcp6: bytes, udp4: bytes, udp6: bytes) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}

    def bump(pid: int, key: str) -> None:
        entry = out.setdefault(pid, {"tcp_established": 0, "tcp_listening": 0, "udp": 0})
        entry[key] += 1

    for raw, row_size, state_off, pid_off in ((tcp4, 24, 0, 20), (tcp6, 56, 48, 52)):
        n = struct.unpack_from("<I", raw, 0)[0] if len(raw) >= 4 else 0
        for i in range(n):
            base = 4 + i * row_size
            state, pid = (
                struct.unpack_from("<I", raw, base + state_off)[0],
                struct.unpack_from("<I", raw, base + pid_off)[0],
            )
            if state == _TCP_ESTABLISHED:
                bump(pid, "tcp_established")
            elif state == _TCP_LISTEN:
                bump(pid, "tcp_listening")
    for raw, row_size, pid_off in ((udp4, 12, 8), (udp6, 28, 24)):
        n = struct.unpack_from("<I", raw, 0)[0] if len(raw) >= 4 else 0
        for i in range(n):
            bump(struct.unpack_from("<I", raw, 4 + i * row_size + pid_off)[0], "udp")
    return out


def sockets_by_pid() -> dict[int, dict[str, int]]:
    """Open TCP/UDP endpoints per process. Only counts are kept - no addresses or ports."""
    _require_windows()
    iphlp = ctypes.WinDLL("iphlpapi")
    return parse_socket_tables(
        _table(iphlp.GetExtendedTcpTable, _AF_INET, _TCP_TABLE_OWNER_PID_ALL),
        _table(iphlp.GetExtendedTcpTable, _AF_INET6, _TCP_TABLE_OWNER_PID_ALL),
        _table(iphlp.GetExtendedUdpTable, _AF_INET, _UDP_TABLE_OWNER_PID),
        _table(iphlp.GetExtendedUdpTable, _AF_INET6, _UDP_TABLE_OWNER_PID),
    )


# --------------------------------------------------------------------------- WHEA events
WHEA_EVENT_NAMES = {
    1: "Fatal hardware error",
    17: "Corrected PCI Express error",
    18: "Fatal machine check",
    19: "Corrected machine check",
    20: "Fatal PCI Express error",
    46: "Corrected memory error",
    47: "Corrected hardware error",
}
_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"


def parse_event_xml(xml: str) -> dict[str, Any] | None:
    try:
        root = ET.fromstring(xml)  # noqa: S314  (rendered by the local Windows event log service)
    except ET.ParseError:
        return None
    system = root.find(f"{_NS}System")
    if system is None:
        return None
    eid_el = system.find(f"{_NS}EventID")
    level_el = system.find(f"{_NS}Level")
    time_el = system.find(f"{_NS}TimeCreated")
    eid = int(eid_el.text) if eid_el is not None and eid_el.text and eid_el.text.isdigit() else None
    level = int(level_el.text) if level_el is not None and level_el.text and level_el.text.isdigit() else None
    return {
        "time": time_el.get("SystemTime") if time_el is not None else None,
        "event_id": eid,
        "level": {1: "critical", 2: "error", 3: "warning", 4: "info"}.get(level or 0, "info"),
        "fatal": (level or 4) <= 2,
        "description": WHEA_EVENT_NAMES.get(eid or -1, f"WHEA event {eid}"),
    }


def whea_events(days: int = 30, limit: int = 50) -> dict[str, Any]:
    """WHEA-Logger events from the System log within ``days`` (newest first)."""
    _require_windows()
    try:
        import win32evtlog
    except ImportError as exc:
        raise DriverUnavailableError("pywin32 win32evtlog not installed") from exc
    query = (
        "*[System[Provider[@Name='Microsoft-Windows-WHEA-Logger'] and "
        f"TimeCreated[timediff(@SystemTime) <= {days * 86_400_000}]]]"
    )
    try:
        handle = win32evtlog.EvtQuery("System", win32evtlog.EvtQueryReverseDirection, query)
    except Exception as exc:  # pywintypes.error
        raise PermissionDeniedError(f"System event log not readable: {exc}") from exc
    events: list[dict[str, Any]] = []
    total = fatal = 0
    while True:
        batch = win32evtlog.EvtNext(handle, 64)
        if not batch:
            break
        for ev in batch:
            parsed = parse_event_xml(win32evtlog.EvtRender(ev, win32evtlog.EvtRenderEventXml))
            if parsed is None:
                continue
            total += 1
            fatal += int(parsed["fatal"])
            if len(events) < limit:
                events.append(parsed)
    return {
        "source": "Windows event log (System / Microsoft-Windows-WHEA-Logger)",
        "window_days": days,
        "total": total,
        "fatal": fatal,
        "corrected": total - fatal,
        "events": events,
        "checked_at": datetime.now(UTC).isoformat(),
    }


# --------------------------------------------------------------------------- process details (opt-in)
_PROFILE = re.compile(r"^([A-Za-z]:\\Users\\)[^\\]+", re.IGNORECASE)


def redact_profile(path: str) -> str:
    """``C:\\Users\\alice\\...`` -> ``C:\\Users\\<user>\\...`` (the account name is never sent in paths)."""
    return _PROFILE.sub(r"\1<user>", path)


@dataclass(frozen=True, slots=True)
class ProcessDetail:
    path: str | None
    user: str | None
    publisher: str | None


class ProcessDetails:
    """Image path, owner and publisher for a PID, cached per (pid, start time).

    Opt-in only (agent setting ``COLLECT_PROCESS_DETAILS`` or the backend's agent configuration). Uses
    ``PROCESS_QUERY_LIMITED_INFORMATION``; protected/other-session processes stay ``None``.
    """

    def __init__(self) -> None:
        _require_windows()
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.OpenProcess.restype = wintypes.HANDLE
        self._k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self._k32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._cache: dict[tuple[int, int], ProcessDetail] = {}

    def get(self, pid: int, create_time: int) -> ProcessDetail:
        key = (pid, create_time)
        hit = self._cache.get(key)
        if hit is None:
            hit = self._cache[key] = self._read(pid)
            if len(self._cache) > 2048:
                self._cache.clear()
        return hit

    def _read(self, pid: int) -> ProcessDetail:
        if pid in (0, 4):
            return ProcessDetail(None, "NT AUTHORITY\\SYSTEM", "Microsoft Corporation")
        handle = self._k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ProcessDetail(None, None, None)
        try:
            path = self._image(handle)
            user = self._owner(handle)
        finally:
            self._k32.CloseHandle(handle)
        publisher = self._publisher(path) if path else None
        return ProcessDetail(redact_profile(path) if path else None, user, publisher)

    def _image(self, handle: Any) -> str | None:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if self._k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None

    @staticmethod
    def _owner(handle: Any) -> str | None:
        try:
            import win32security

            token = win32security.OpenProcessToken(handle, 0x0008)  # TOKEN_QUERY
            sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
            name, domain, _ = win32security.LookupAccountSid(None, sid)
            return f"{domain}\\{name}" if domain else str(name)
        except Exception:
            return None

    @staticmethod
    def _publisher(path: str) -> str | None:
        with contextlib.suppress(Exception):
            import win32api

            pairs = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")
            translations: list[tuple[int, int]] = list(pairs or [])  # type: ignore[arg-type]
            for lang, cp in translations:
                value = win32api.GetFileVersionInfo(
                    path, f"\\StringFileInfo\\{lang:04x}{cp:04x}\\CompanyName"
                )
                if value:
                    return str(value).strip()
        return None


# --------------------------------------------------------------------------- NVMe SMART / health log
_IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
_HEALTH_LOG_LEN = 512


def parse_nvme_health(d: bytes) -> dict[str, Any]:
    """NVMe SMART / Health Information log page (log identifier 02h), NVMe base spec layout."""

    def u128(off: int) -> int:
        return int.from_bytes(d[off : off + 16], "little")

    kelvin = struct.unpack_from("<H", d, 1)[0]
    return {
        "critical_warning": d[0],
        "temperature_c": kelvin - 273 if kelvin else None,
        "available_spare_percent": d[3],
        "available_spare_threshold_percent": d[4],
        "percentage_used": d[5],
        "data_read_bytes": u128(32) * 512_000,
        "data_written_bytes": u128(48) * 512_000,
        "power_cycles": u128(112),
        "power_on_hours": u128(128),
        "unsafe_shutdowns": u128(144),
        "media_errors": u128(160),
        "error_log_entries": u128(176),
    }


def nvme_health(drive_index: int) -> dict[str, Any]:
    """Read the NVMe health log through the storage stack (handle opened with no access rights)."""
    _require_windows()
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    k32.DeviceIoControl.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    )
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.CreateFileW(rf"\\.\PhysicalDrive{drive_index}", 0, 3, None, 3, 0, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise HardwareMissingError(f"PhysicalDrive{drive_index} not found ({ctypes.get_last_error()})")
    try:
        # STORAGE_PROPERTY_QUERY(StorageDeviceProtocolSpecificProperty, PropertyStandardQuery)
        # + STORAGE_PROTOCOL_SPECIFIC_DATA(ProtocolTypeNvme, NVMeDataTypeLogPage, health log 02h)
        query = struct.pack("<II", 50, 0) + struct.pack("<10I", 3, 2, 2, 0, 40, _HEALTH_LOG_LEN, 0, 0, 0, 0)
        query += bytes(_HEALTH_LOG_LEN)
        inbuf = ctypes.create_string_buffer(query, len(query))
        out = ctypes.create_string_buffer(len(query))
        returned = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(
            handle,
            _IOCTL_STORAGE_QUERY_PROPERTY,
            inbuf,
            len(query),
            out,
            len(out),
            ctypes.byref(returned),
            None,
        )
        if not ok:
            err = ctypes.get_last_error()
            if err == 50:  # ERROR_NOT_SUPPORTED
                raise HardwareMissingError("Drive is not NVMe or the driver does not expose the health log")
            raise DriverUnavailableError(f"NVMe health log query failed ({err})")
    finally:
        k32.CloseHandle(handle)
    data_off = struct.unpack_from("<I", out.raw, 8 + 16)[0]  # ProtocolDataOffset
    start = 8 + data_off
    return parse_nvme_health(out.raw[start : start + _HEALTH_LOG_LEN])
