"""LibreHardwareMonitor (LHM) sensor access.

LHM exposes motherboard/EC sensors (CPU package temperature, package power, fan RPM, GPU
temperature/clock/power, NVMe temperature) that Windows does not publish to normal users. Reading
those sensors requires LHM's kernel driver, i.e. LHM running **as administrator**.

Two transports are supported:

* HTTP: LHM "Remote Web Server" (Options -> Remote Web Server -> Run), JSON at ``/data.json``.
* WMI:  namespace ``root\\LibreHardwareMonitor`` (published while LHM is running).

If neither is reachable every LHM-backed metric is reported as unavailable with that reason.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.errors import DriverUnavailableError, TelemetryError
from app.platform.wmi import WmiQueryable

_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")
_RETRY_AFTER_S = 30.0


@dataclass(frozen=True, slots=True)
class LhmSensor:
    identifier: str  # e.g. "/intelcpu/0/temperature/6"
    hardware: str  # e.g. "13th Gen Intel Core i5-1335U"
    sensor_type: str  # Temperature | Power | Fan | Clock | Load | Control ...
    name: str  # e.g. "CPU Package"
    value: float | None

    @property
    def hardware_kind(self) -> str:
        parts = self.identifier.strip("/").split("/")
        return parts[0] if parts else ""


def _parse_value(raw: Any) -> float | None:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        match = _NUMBER.search(raw)
        if match:
            return float(match.group(0).replace(",", "."))
    return None


def parse_data_json(
    node: dict[str, Any], hardware: str = "", out: list[LhmSensor] | None = None
) -> list[LhmSensor]:
    """Flatten LHM's ``data.json`` tree into sensors."""
    out = [] if out is None else out
    children = node.get("Children") or []
    sensor_id = node.get("SensorId")
    if sensor_id and node.get("Type"):
        out.append(
            LhmSensor(
                identifier=str(sensor_id),
                hardware=hardware,
                sensor_type=str(node["Type"]),
                name=str(node.get("Text", "")),
                value=_parse_value(node.get("Value")),
            )
        )
    # Hardware nodes carry an image/HardwareId; sensor-group nodes ("Temperatures") do not.
    next_hw = hardware
    if node.get("HardwareId") or (children and node.get("ImageURL") and "Type" not in node):
        next_hw = str(node.get("Text", hardware))
    for child in children:
        parse_data_json(child, next_hw, out)
    return out


class LhmClient:
    """Fetches LHM sensors with a short cache so several providers share one read per tick."""

    def __init__(self, url: str, wmi: WmiQueryable | None, cache_ms: int = 400) -> None:
        self._url = url
        self._wmi = wmi
        self._cache_s = cache_ms / 1000.0
        self._cached: list[LhmSensor] | None = None
        self._cached_at = 0.0
        self._unreachable_since: float | None = None
        self._last_error = "LibreHardwareMonitor not yet queried"
        self._http = httpx.Client(timeout=0.5)

    @property
    def last_error(self) -> str:
        return self._last_error

    def sensors(self) -> list[LhmSensor]:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self._cache_s:
            return self._cached
        if self._unreachable_since is not None and now - self._unreachable_since < _RETRY_AFTER_S:
            raise DriverUnavailableError(self._last_error)
        try:
            sensors = self._fetch()
        except TelemetryError as exc:
            self._unreachable_since = now
            self._last_error = exc.detail
            self._cached = None
            raise
        self._unreachable_since = None
        self._cached, self._cached_at = sensors, now
        return sensors

    def _fetch(self) -> list[LhmSensor]:
        errors: list[str] = []
        try:
            resp = self._http.get(self._url)
            resp.raise_for_status()
            sensors = parse_data_json(resp.json())
            if sensors:
                return sensors
            errors.append(f"{self._url} returned no sensors")
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"HTTP {self._url} unreachable ({type(exc).__name__})")
        if self._wmi is not None:
            try:
                rows = self._wmi.query(
                    "SELECT Identifier, Name, SensorType, Value, Parent FROM Sensor",
                    "root\\LibreHardwareMonitor",
                )
                if rows:
                    return [
                        LhmSensor(
                            identifier=str(r.get("Identifier", "")),
                            hardware=str(r.get("Parent", "")),
                            sensor_type=str(r.get("SensorType", "")),
                            name=str(r.get("Name", "")),
                            value=_parse_value(r.get("Value")),
                        )
                        for r in rows
                    ]
                errors.append("WMI root\\LibreHardwareMonitor has no sensors")
            except TelemetryError as exc:
                errors.append(f"WMI root\\LibreHardwareMonitor: {exc.detail}")
        raise DriverUnavailableError(
            "LibreHardwareMonitor not running as administrator or not reachable: " + "; ".join(errors)
        )

    def find(self, kinds: tuple[str, ...], sensor_type: str, names: tuple[str, ...]) -> LhmSensor | None:
        """First sensor matching hardware kind prefix, type and one of the names (in priority order)."""
        sensors = [
            s
            for s in self.sensors()
            if s.sensor_type == sensor_type and s.hardware_kind.startswith(kinds) and s.value is not None
        ]
        for wanted in names:
            for s in sensors:
                if s.name.lower() == wanted.lower():
                    return s
        return None

    def close(self) -> None:
        self._http.close()


CPU_KINDS = ("intelcpu", "amdcpu", "cpu")
GPU_KINDS = ("gpu-nvidia", "gpu-amd", "gpu-intel", "nvidiagpu", "atigpu", "gpu")
STORAGE_KINDS = ("nvme", "hdd", "ssd", "storage")
