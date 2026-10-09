"""Hardware discovery: builds the HardwareInventory sent to the backend at startup.

Privacy defaults: serial numbers and MAC addresses are excluded unless explicitly enabled. The
device ID is a one-way hash of the SMBIOS UUID so it is stable without revealing identifiers.
"""

from __future__ import annotations

import hashlib
import platform
import socket
from collections.abc import Callable
from datetime import datetime
from typing import Any

import psutil

from app.errors import TelemetryError
from app.platform.dxgi import DxgiAdapter
from app.platform.powercfg import BatteryReportEntry
from app.platform.wmi import WmiQueryable

_MEMORY_TYPES = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5", 35: "LPDDR5"}
_FORM_FACTORS = {8: "DIMM", 12: "SODIMM"}


class InventoryCollector:
    def __init__(
        self,
        wmi: WmiQueryable | None,
        adapters: Callable[[], list[DxgiAdapter]],
        battery_report: Callable[[], list[BatteryReportEntry]],
        include_serials: bool = False,
        include_macs: bool = False,
        include_hostname: bool = True,
        extras: dict[str, Callable[[], Any]] | None = None,
    ) -> None:
        self._extras = extras or {}
        self._include_hostname = include_hostname
        self._wmi = wmi
        self._adapters = adapters
        self._battery_report = battery_report
        self._include_serials = include_serials
        self._include_macs = include_macs
        self.errors: dict[str, str] = {}

    def _q(self, section: str, wql: str, namespace: str = "root\\cimv2") -> list[dict[str, Any]]:
        if self._wmi is None:
            self.errors[section] = "WMI unavailable"
            return []
        try:
            return self._wmi.query(wql, namespace)
        except TelemetryError as exc:
            self.errors[section] = exc.detail
            return []

    def collect(self) -> dict[str, Any]:
        cs = next(iter(self._q("system", "SELECT * FROM Win32_ComputerSystem")), {})
        product = next(iter(self._q("system", "SELECT * FROM Win32_ComputerSystemProduct")), {})
        inventory: dict[str, Any] = {
            "manufacturer": _clean(cs.get("Manufacturer")),
            "model": _clean(product.get("Version"))
            or _clean(cs.get("SystemFamily"))
            or _clean(cs.get("Model")),
            "model_number": _clean(cs.get("Model")),
            "system_family": _clean(cs.get("SystemFamily")),
            "system_type": _clean(cs.get("SystemType")),
            "pc_system_type": {1: "Desktop", 2: "Mobile", 3: "Workstation"}.get(
                cs.get("PCSystemType") or 0, "Unknown"
            ),
            "motherboard": self._motherboard(),
            "bios": self._bios(),
            "os": self._os(),
            "cpu": self._cpu(),
            "memory": self._memory(),
            "gpu": self._gpu(),
            "storage": self._storage(),
            "network": self._network(),
            "battery": self._battery(),
            "display": self._display(),
        }
        # Optional facts from Windows APIs (CPU topology, Secure Boot / TPM, WHEA history).
        for section, fn in self._extras.items():
            try:
                inventory[section] = fn()
            except TelemetryError as exc:
                self.errors[section] = exc.detail
                inventory[section] = {"available": False, "reason": exc.detail}
        if self._include_hostname:
            inventory["hostname"] = socket.gethostname()
        if self._include_serials:
            inventory["serial_number"] = _clean(product.get("IdentifyingNumber"))
        inventory["_uuid_hash"] = hashlib.sha256(
            str(product.get("UUID", platform.node())).encode()
        ).hexdigest()
        inventory["discovery_errors"] = dict(self.errors)
        return inventory

    def _motherboard(self) -> dict[str, Any]:
        bb = next(
            iter(self._q("motherboard", "SELECT Manufacturer, Product, Version FROM Win32_BaseBoard")), {}
        )
        return {
            "manufacturer": _clean(bb.get("Manufacturer")),
            "product": _clean(bb.get("Product")),
            "version": _clean(bb.get("Version")),
        }

    def _bios(self) -> dict[str, Any]:
        b = next(
            iter(self._q("bios", "SELECT Manufacturer, SMBIOSBIOSVersion, ReleaseDate FROM Win32_BIOS")), {}
        )
        return {
            "manufacturer": _clean(b.get("Manufacturer")),
            "version": _clean(b.get("SMBIOSBIOSVersion")),
            "release_date": b.get("ReleaseDate"),
        }

    def _os(self) -> dict[str, Any]:
        o = next(
            iter(
                self._q(
                    "os",
                    "SELECT Caption, Version, BuildNumber, OSArchitecture, InstallDate, LastBootUpTime "
                    "FROM Win32_OperatingSystem",
                )
            ),
            {},
        )
        return {
            "name": _clean(o.get("Caption")) or platform.system(),
            "version": o.get("Version") or platform.version(),
            "build": o.get("BuildNumber"),
            "architecture": o.get("OSArchitecture") or platform.machine(),
            "install_date": _wmi_iso(o.get("InstallDate")),
            "last_boot": _wmi_iso(o.get("LastBootUpTime")),
            "edition_id": _edition_id(),
            "display_version": _registry_value("DisplayVersion"),
            "ubr": _registry_value("UBR"),
        }

    def _cpu(self) -> dict[str, Any]:
        p = next(iter(self._q("cpu", "SELECT * FROM Win32_Processor")), {})
        return {
            "model": _clean(p.get("Name")) or platform.processor(),
            "manufacturer": _clean(p.get("Manufacturer")),
            "cores": p.get("NumberOfCores") or psutil.cpu_count(logical=False),
            "threads": p.get("NumberOfLogicalProcessors") or psutil.cpu_count(logical=True),
            "base_clock_mhz": p.get("MaxClockSpeed"),
            "l2_cache_kb": p.get("L2CacheSize"),
            "l3_cache_kb": p.get("L3CacheSize"),
            "socket": _clean(p.get("SocketDesignation")),
            "virtualization_enabled": p.get("VirtualizationFirmwareEnabled"),
        }

    def _memory(self) -> dict[str, Any]:
        modules = []
        for m in self._q("memory", "SELECT * FROM Win32_PhysicalMemory"):
            modules.append(
                {
                    "slot": _clean(m.get("DeviceLocator")),
                    "capacity_bytes": int(m["Capacity"]) if m.get("Capacity") else None,
                    "speed_mts": m.get("Speed"),
                    "configured_speed_mts": m.get("ConfiguredClockSpeed"),
                    "manufacturer": _clean(m.get("Manufacturer")),
                    "part_number": _clean(m.get("PartNumber")),
                    "type": _MEMORY_TYPES.get(m.get("SMBIOSMemoryType") or 0),
                    "form_factor": _FORM_FACTORS.get(m.get("FormFactor") or 0),
                }
            )
        return {"total_bytes": psutil.virtual_memory().total, "modules": modules}

    def _gpu(self) -> list[dict[str, Any]]:
        try:
            adapters = [a for a in self._adapters() if not a.is_software]
        except TelemetryError as exc:
            self.errors["gpu"] = exc.detail
            adapters = []
        controllers = {
            (_clean(v.get("Name")) or ""): v for v in self._q("gpu", "SELECT * FROM Win32_VideoController")
        }
        out = []
        for a in adapters:
            vc = controllers.get(a.name, {})
            out.append(
                {
                    "name": a.name,
                    "vendor": {0x8086: "Intel", 0x10DE: "NVIDIA", 0x1002: "AMD"}.get(
                        a.vendor_id, hex(a.vendor_id)
                    ),
                    "luid": a.luid,
                    "dedicated_vram_bytes": a.dedicated_video_memory,
                    "shared_memory_bytes": a.shared_system_memory,
                    "driver_version": vc.get("DriverVersion"),
                    "integrated": a.dedicated_video_memory < 512 * 1024 * 1024,
                    "resolution": f"{vc['CurrentHorizontalResolution']}x{vc['CurrentVerticalResolution']}"
                    if vc.get("CurrentHorizontalResolution")
                    else None,
                    "refresh_rate_hz": vc.get("CurrentRefreshRate"),
                }
            )
        return out

    def _storage(self) -> list[dict[str, Any]]:
        physical = {
            str(d.get("DeviceId")): d
            for d in self._q(
                "storage",
                "SELECT DeviceId, MediaType, BusType, HealthStatus, Size FROM MSFT_PhysicalDisk",
                "root\\Microsoft\\Windows\\Storage",
            )
        }
        out = []
        for d in self._q("storage", "SELECT * FROM Win32_DiskDrive"):
            index = str(d.get("Index"))
            pd = physical.get(index, {})
            entry = {
                "disk": f"PhysicalDrive{index}",
                "model": _clean(d.get("Model")),
                "size_bytes": int(d["Size"]) if d.get("Size") else None,
                "interface": _clean(d.get("InterfaceType")),
                "media_type": {3: "HDD", 4: "SSD", 5: "SCM"}.get(
                    pd.get("MediaType") or 0, _clean(d.get("MediaType"))
                ),
                "bus_type": {7: "USB", 11: "SATA", 17: "NVMe"}.get(pd.get("BusType") or 0),
                "firmware": _clean(d.get("FirmwareRevision")),
                "partitions": d.get("Partitions"),
            }
            if self._include_serials:
                entry["serial_number"] = _clean(d.get("SerialNumber"))
            out.append(entry)
        return out

    def _network(self) -> list[dict[str, Any]]:
        out = []
        for n in self._q("network", "SELECT * FROM Win32_NetworkAdapter WHERE PhysicalAdapter = TRUE"):
            if not n.get("NetConnectionID"):
                continue
            entry = {
                "name": _clean(n.get("Name")),
                "interface": n.get("NetConnectionID"),
                "manufacturer": _clean(n.get("Manufacturer")),
                "type": "wifi"
                if "wi-fi" in str(n.get("Name", "")).lower() or "wireless" in str(n.get("Name", "")).lower()
                else "ethernet"
                if "ethernet" in str(n.get("Name", "")).lower()
                else "other",
            }
            if self._include_macs:
                entry["mac_address"] = n.get("MACAddress")
            out.append(entry)
        return out

    def _battery(self) -> dict[str, Any] | None:
        rows = self._q("battery", "SELECT Name, DeviceID, DesignVoltage, Chemistry FROM Win32_Battery")
        if not rows:
            return None
        b = rows[0]
        info: dict[str, Any] = {
            "name": _clean(b.get("Name")),
            "design_voltage_mv": int(b["DesignVoltage"]) if b.get("DesignVoltage") else None,
        }
        try:
            report = self._battery_report()[0]
            info.update(
                {
                    "manufacturer": _clean(report.manufacturer),
                    "chemistry": report.chemistry,
                    "design_capacity_wh": report.design_capacity_mwh / 1000
                    if report.design_capacity_mwh
                    else None,
                    "full_charge_capacity_wh": report.full_charge_capacity_mwh / 1000
                    if report.full_charge_capacity_mwh
                    else None,
                    "cycle_count": report.cycle_count,
                }
            )
        except (TelemetryError, IndexError) as exc:
            self.errors["battery_report"] = str(exc)
        return info

    def _display(self) -> dict[str, Any]:
        monitors = self._q(
            "display",
            "SELECT InstanceName, MaxHorizontalImageSize, MaxVerticalImageSize, Active "
            "FROM WmiMonitorBasicDisplayParams",
            "root\\wmi",
        )
        internal = next((m for m in monitors if m.get("Active")), None)
        size = None
        if internal and internal.get("MaxHorizontalImageSize") and internal.get("MaxVerticalImageSize"):
            w, h = internal["MaxHorizontalImageSize"], internal["MaxVerticalImageSize"]
            size = {"width_cm": w, "height_cm": h, "diagonal_in": round(((w**2 + h**2) ** 0.5) / 2.54, 1)}
        return {"panel_size": size, "monitor_count": len(monitors)}


def _wmi_iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and "T" in value and "-" in value[:5]:
        return value  # already ISO-8601 (WmiClient converts COM dates)
    from app.platform.security import parse_wmi_datetime

    parsed = parse_wmi_datetime(value)
    return parsed.isoformat() if parsed else None


def _registry_value(name: str) -> Any:
    r"""HKLM\...\Windows NT\CurrentVersion value (e.g. DisplayVersion '24H2', UBR build revision)."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            return winreg.QueryValueEx(k, name)[0]
    except (OSError, ImportError):
        return None


def _edition_id() -> str | None:
    value = _registry_value("EditionID")
    return str(value) if value else None


def device_id_from(inventory: dict[str, Any]) -> str:
    seed = f"{inventory.get('manufacturer')}|{inventory.get('model_number')}|{inventory.get('_uuid_hash')}"
    return "ldt-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None
