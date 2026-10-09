"""Builds the component hierarchy from a hardware inventory and routes metrics to components.

Laptop
 +-- Chassis
 +-- Display
 +-- Motherboard
 |    +-- CPU
 |    +-- GPU (one per adapter)
 |    +-- Memory
 |    +-- VRM
 +-- Storage
 |    +-- Disk (one per physical disk)
 +-- Battery
 +-- Cooling
 |    +-- Fan
 |    +-- Thermal sensors
 +-- Network
 |    +-- Network adapter (one per physical NIC)
 +-- Power
 +-- Operating system
      +-- Telemetry agent
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.components.models import Component, ComponentType

T = ComponentType

LAPTOP = "laptop"


def build_topology(inventory: Mapping[str, Any]) -> dict[str, Component]:
    manufacturer = inventory.get("manufacturer")
    model = inventory.get("model")
    comps: list[Component] = [
        Component(
            LAPTOP,
            T.LAPTOP,
            f"{manufacturer or ''} {model or 'Laptop'}".strip(),
            None,
            manufacturer,
            model,
            {"model_number": inventory.get("model_number"), "system_type": inventory.get("system_type")},
        ),
        Component(
            "chassis",
            T.CHASSIS,
            "Chassis",
            LAPTOP,
            manufacturer,
            model,
            {"form_factor": inventory.get("pc_system_type")},
        ),
        Component("display", T.DISPLAY, "Display", LAPTOP, manufacturer, None, _display_props(inventory)),
        Component(
            "motherboard",
            T.MOTHERBOARD,
            "Motherboard",
            LAPTOP,
            (inventory.get("motherboard") or {}).get("manufacturer"),
            (inventory.get("motherboard") or {}).get("product"),
            {"bios": inventory.get("bios")},
        ),
    ]
    cpu = inventory.get("cpu") or {}
    comps.append(
        Component(
            "cpu",
            T.CPU,
            cpu.get("model") or "CPU",
            "motherboard",
            cpu.get("manufacturer"),
            cpu.get("model"),
            {k: v for k, v in cpu.items() if k not in ("model", "manufacturer")},
        )
    )
    for gpu in inventory.get("gpu") or []:
        comps.append(
            Component(
                gpu_component_id(gpu.get("luid")),
                T.GPU,
                gpu.get("name") or "GPU",
                "motherboard",
                gpu.get("vendor"),
                gpu.get("name"),
                {k: v for k, v in gpu.items() if k != "name"},
            )
        )
    mem = inventory.get("memory") or {}
    comps.append(Component("memory", T.MEMORY, "Memory", "motherboard", None, None, dict(mem)))
    comps.append(
        Component(
            "vrm",
            T.VRM,
            "Voltage regulators (VRM)",
            "motherboard",
            None,
            None,
            {"note": "No VRM sensors are exposed to the operating system on this platform"},
        )
    )

    comps.append(Component("storage", T.STORAGE, "Storage", LAPTOP))
    for disk in inventory.get("storage") or []:
        comps.append(
            Component(
                disk_component_id(disk.get("disk")),
                T.DISK,
                disk.get("model") or "Disk",
                "storage",
                None,
                disk.get("model"),
                dict(disk),
            )
        )

    battery = inventory.get("battery")
    comps.append(
        Component(
            "battery",
            T.BATTERY,
            (battery or {}).get("name") or "Battery",
            LAPTOP,
            (battery or {}).get("manufacturer"),
            (battery or {}).get("name"),
            dict(battery) if battery else {"present": False},
        )
    )
    comps.append(Component("cooling", T.COOLING, "Cooling system", LAPTOP))
    comps.append(Component("fan", T.FAN, "System fan", "cooling"))
    comps.append(Component("thermal_sensors", T.THERMAL_SENSORS, "Thermal sensors", "cooling"))

    comps.append(Component("network", T.NETWORK, "Network", LAPTOP))
    for nic in inventory.get("network") or []:
        comps.append(
            Component(
                nic_component_id(nic.get("interface")),
                T.NETWORK_ADAPTER,
                nic.get("name") or "NIC",
                "network",
                nic.get("manufacturer"),
                nic.get("name"),
                dict(nic),
            )
        )
    comps.append(Component("power", T.POWER, "Power", LAPTOP))
    os_info = inventory.get("os") or {}
    comps.append(
        Component(
            "os",
            T.OPERATING_SYSTEM,
            os_info.get("name") or "Operating system",
            LAPTOP,
            "Microsoft" if "windows" in str(os_info.get("name", "")).lower() else None,
            os_info.get("version"),
            dict(os_info),
        )
    )
    comps.append(Component("agent", T.AGENT, "Telemetry agent", "os"))
    return {c.component_id: c for c in comps}


def _display_props(inventory: Mapping[str, Any]) -> dict[str, Any]:
    display = dict(inventory.get("display") or {})
    gpus = inventory.get("gpu") or []
    if gpus:
        display["resolution"] = gpus[0].get("resolution")
        display["refresh_rate_hz"] = gpus[0].get("refresh_rate_hz")
    return display


def gpu_component_id(luid: str | None) -> str:
    return f"gpu:{luid}" if luid else "gpu"


def disk_component_id(disk: str | None) -> str:
    return f"disk:{disk}" if disk else "storage"


def nic_component_id(nic: str | None) -> str:
    return f"nic:{nic}" if nic else "network"


_PREFIX_ROUTES = {
    "cpu": "cpu",
    "memory": "memory",
    "battery": "battery",
    "power": "power",
    "thermal": "thermal_sensors",
    "fan": "fan",
    "display": "display",
    "system": "os",
    "agent": "agent",
    "security": "motherboard",
}


def route_metric(metric: str, labels: Mapping[str, str], components: Mapping[str, Component]) -> str:
    """Return the component id a metric sample belongs to (falls back to a subsystem component)."""
    prefix = metric.split(".", 1)[0]
    if prefix == "gpu":
        luid = labels.get("luid")
        if luid and gpu_component_id(luid) in components:
            return gpu_component_id(luid)
        adapter = labels.get("adapter", "").lower()
        gpus = [c for c in components.values() if c.component_type is ComponentType.GPU]
        for g in gpus:
            if adapter and (g.name.lower() in adapter or adapter in g.name.lower()):
                return g.component_id
        return gpus[0].component_id if gpus else "motherboard"
    if prefix == "disk":
        disk = labels.get("disk")
        if disk and disk_component_id(disk) in components:
            return disk_component_id(disk)
        if metric == "disk.temperature_c":
            disks = [c for c in components.values() if c.component_type is ComponentType.DISK]
            return disks[0].component_id if disks else "storage"
        return "storage"
    if prefix == "network":
        nic = labels.get("nic")
        if nic and nic_component_id(nic) in components:
            return nic_component_id(nic)
        return "network"
    target = _PREFIX_ROUTES.get(prefix)
    if target and target in components:
        return target
    return LAPTOP
