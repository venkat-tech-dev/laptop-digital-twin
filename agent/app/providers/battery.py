from __future__ import annotations

import threading
import time
from collections.abc import Callable
from types import ModuleType
from typing import Any

import psutil as _psutil

from app.contracts import MetricKind
from app.errors import TelemetryError
from app.platform.powercfg import BatteryReportEntry, read_battery_report
from app.platform.wmi import WmiQueryable
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC_PS = "Windows power subsystem (GetSystemPowerStatus)"
SRC_WMI = "Windows ACPI battery driver (root\\wmi BatteryStatus)"
SRC_REPORT = "Windows battery report (powercfg)"

_REPORT_REFRESH_S = 3600.0


def charging_state(
    plugged: bool | None, charging: bool | None, discharging: bool | None, percent: float | None
) -> str:
    if discharging:
        return "discharging"
    if charging:
        return "charging"
    if plugged:
        return "full" if percent is not None and percent >= 99.5 else "idle_on_ac"
    if plugged is False:
        return "discharging"
    return "unknown"


class BatteryProvider(TelemetryProvider):
    """Charge, charging state, voltage, power, capacity, cycle count and wear.

    ``battery.health_percent`` is *derived*: full-charge capacity / design capacity, both reported
    by the battery's own fuel gauge through Windows. It is never estimated.
    """

    name = "battery"
    lane = "wmi"
    timeout_s = 20.0
    component = "battery"

    def __init__(
        self,
        interval_ms: int,
        wmi: WmiQueryable | None,
        psutil: ModuleType = _psutil,
        report_reader: Callable[[], list[BatteryReportEntry]] = read_battery_report,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(interval_ms)
        self._wmi = wmi
        self._ps = psutil
        self._report_reader = report_reader
        self._clock = clock
        self._report: BatteryReportEntry | None = None
        self._report_error: str | None = None
        self._report_at: float | None = None
        self._report_lock = threading.Lock()
        self._report_thread: threading.Thread | None = None

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("battery.charge_percent", "percent", SRC_PS)]

    def collect(self) -> list[Reading]:
        bat = self._ps.sensors_battery()
        if bat is None:
            return [
                self._na("battery.charge_percent", "percent", SRC_PS, "No battery present"),
                self._na("battery.charging_state", "state", SRC_PS, "No battery present"),
                self._r("power.source", "ac", "state", SRC_PS),
            ]
        status = self._wmi_one("SELECT * FROM BatteryStatus")
        full = self._wmi_one("SELECT FullChargedCapacity FROM BatteryFullChargedCapacity")
        cycles = self._wmi_one("SELECT CycleCount FROM BatteryCycleCount")
        report = self._battery_report()

        percent = float(bat.percent)
        plugged = bool(bat.power_plugged) if bat.power_plugged is not None else None
        charging = _bool(status, "Charging")
        discharging = _bool(status, "Discharging")
        out = [
            self._r("battery.charge_percent", percent, "percent", SRC_PS),
            self._r("battery.power_plugged", plugged, "bool", SRC_PS),
            self._r(
                "battery.charging_state",
                charging_state(plugged, charging, discharging, percent),
                "state",
                SRC_WMI if isinstance(status, dict) else SRC_PS,
                kind=MetricKind.DERIVED,
            ),
            self._r("power.source", "ac" if plugged else "battery", "state", SRC_PS),
        ]
        out.append(self._time_remaining(bat))
        out += self._status_readings(status, discharging, charging)
        out += self._capacity_readings(full, cycles, report)
        return out

    def _time_remaining(self, bat: Any) -> Reading:
        secs = bat.secsleft
        if secs == self._ps.POWER_TIME_UNLIMITED:
            return self._na("battery.time_remaining_s", "s", SRC_PS, "On AC power (not discharging)")
        if secs == self._ps.POWER_TIME_UNKNOWN or secs is None or secs < 0:
            return self._na(
                "battery.time_remaining_s", "s", SRC_PS, "Windows has not yet estimated remaining time"
            )
        return self._r("battery.time_remaining_s", int(secs), "s", SRC_PS)

    def _status_readings(
        self, status: dict[str, Any] | str, discharging: bool | None, charging: bool | None
    ) -> list[Reading]:
        names = (
            ("battery.voltage_v", "mV"),
            ("battery.discharge_rate_w", "mW"),
            ("battery.charge_rate_w", "mW"),
            ("battery.remaining_capacity_wh", "mWh"),
            ("power.system_power_w", "mW"),
        )
        if isinstance(status, str):
            return [self._na(n, u, SRC_WMI, status) for n, u in names]
        out = [
            self._raw("battery.voltage_v", status.get("Voltage"), "mV", SRC_WMI),
            self._raw("battery.remaining_capacity_wh", status.get("RemainingCapacity"), "mWh", SRC_WMI),
        ]
        discharge = status.get("DischargeRate")
        charge = status.get("ChargeRate")
        out.append(self._raw("battery.discharge_rate_w", discharge, "mW", SRC_WMI))
        out.append(self._raw("battery.charge_rate_w", charge, "mW", SRC_WMI))
        if discharging and isinstance(discharge, int) and discharge > 0:
            # On battery the whole system is powered from the pack: discharge rate == system draw.
            out.append(self._r("power.system_power_w", discharge, "mW", SRC_WMI))
        else:
            out.append(
                self._na(
                    "power.system_power_w",
                    "mW",
                    SRC_WMI,
                    "System power is only measurable while running on battery (no AC-side meter exposed)",
                )
            )
        return out

    def _capacity_readings(
        self, full: dict[str, Any] | str, cycles: dict[str, Any] | str, report: BatteryReportEntry | str
    ) -> list[Reading]:
        out: list[Reading] = []
        full_mwh: int | None = None
        if isinstance(full, dict) and isinstance(full.get("FullChargedCapacity"), int):
            full_mwh = full["FullChargedCapacity"]
            out.append(self._r("battery.full_charge_capacity_wh", full_mwh, "mWh", SRC_WMI))
        elif isinstance(report, BatteryReportEntry) and report.full_charge_capacity_mwh:
            full_mwh = report.full_charge_capacity_mwh
            out.append(self._r("battery.full_charge_capacity_wh", full_mwh, "mWh", SRC_REPORT))
        else:
            out.append(
                self._na(
                    "battery.full_charge_capacity_wh",
                    "mWh",
                    SRC_WMI,
                    full if isinstance(full, str) else "Not reported",
                )
            )

        if (
            isinstance(cycles, dict)
            and isinstance(cycles.get("CycleCount"), int)
            and cycles["CycleCount"] > 0
        ):
            out.append(self._r("battery.cycle_count", cycles["CycleCount"], "count", SRC_WMI))
        elif isinstance(report, BatteryReportEntry) and report.cycle_count:
            out.append(self._r("battery.cycle_count", report.cycle_count, "count", SRC_REPORT))
        else:
            out.append(
                self._na(
                    "battery.cycle_count", "count", SRC_WMI, "Battery firmware does not report cycle count"
                )
            )

        if isinstance(report, BatteryReportEntry) and report.design_capacity_mwh:
            design = report.design_capacity_mwh
            out.append(
                self._r("battery.design_capacity_wh", design, "mWh", SRC_REPORT, kind=MetricKind.STATIC)
            )
            if full_mwh:
                out.append(
                    self._r(
                        "battery.health_percent",
                        100.0 * full_mwh / design,
                        "percent",
                        f"{SRC_WMI} / {SRC_REPORT}",
                        kind=MetricKind.DERIVED,
                    )
                )
            else:
                out.append(
                    self._na("battery.health_percent", "percent", SRC_REPORT, "Full-charge capacity unknown")
                )
        else:
            reason = report if isinstance(report, str) else "Design capacity not reported"
            out.append(self._na("battery.design_capacity_wh", "mWh", SRC_REPORT, reason))
            out.append(self._na("battery.health_percent", "percent", SRC_REPORT, reason))
        return out

    def _raw(self, metric: str, value: Any, unit: str, source: str) -> Reading:
        if value is None:
            return self._na(metric, unit, source, "Not reported by battery driver")
        return self._r(metric, value, unit, source)

    def _wmi_one(self, wql: str) -> dict[str, Any] | str:
        if self._wmi is None:
            return "WMI unavailable"
        try:
            rows = self._wmi.query(wql, "root\\wmi")
        except TelemetryError as exc:
            return exc.detail
        return rows[0] if rows else "Battery driver returned no data"

    def _refresh_report(self) -> None:
        try:
            entries = self._report_reader()
            with self._report_lock:
                self._report, self._report_error = entries[0], None
        except TelemetryError as exc:
            with self._report_lock:
                self._report, self._report_error = None, exc.detail

    def _battery_report(self) -> BatteryReportEntry | str:
        """Cached powercfg battery report, refreshed hourly on a background thread (takes 1-3 s)."""
        now = self._clock()
        running = self._report_thread is not None and self._report_thread.is_alive()
        if not running and (self._report_at is None or now - self._report_at > _REPORT_REFRESH_S):
            self._report_at = now
            self._report_thread = threading.Thread(
                target=self._refresh_report, name="battery-report", daemon=True
            )
            self._report_thread.start()
        with self._report_lock:
            if self._report is not None:
                return self._report
            return self._report_error or "Battery report is being generated (powercfg)"

    def wait_for_report(self, timeout_s: float = 10.0) -> None:
        """Test/CLI helper: block until the first report refresh finished."""
        if self._report_thread is not None:
            self._report_thread.join(timeout_s)


def _bool(status: dict[str, Any] | str, key: str) -> bool | None:
    if isinstance(status, dict) and key in status and status[key] is not None:
        return bool(status[key])
    return None
