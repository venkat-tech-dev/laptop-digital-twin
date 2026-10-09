from __future__ import annotations

from collections.abc import Callable

from app.errors import TelemetryError
from app.platform.lhm import CPU_KINDS, STORAGE_KINDS, LhmClient
from app.platform.pdh import CounterReader, PdhCounterSet
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC_ACPI = "Windows Performance Counter (ACPI Thermal Zone)"
SRC_LHM = "LibreHardwareMonitor"

_ACPI_PATHS = {
    "temperature": r"\Thermal Zone Information(*)\High Precision Temperature",
    "passive_limit": r"\Thermal Zone Information(*)\% Passive Limit",
    "throttle": r"\Thermal Zone Information(*)\Throttle Reasons",
}

CPU_TEMP_NO_LHM = (
    "CPU package sensor (MSR) is only readable by a kernel driver: run LibreHardwareMonitor as "
    "administrator. ACPI thermal-zone temperature is reported separately."
)


class TemperatureProvider(TelemetryProvider):
    """Thermal sensors.

    * ACPI thermal zones (firmware-defined platform sensors, readable without admin rights).
      ``% Passive Limit`` < 100 means the firmware is passively cooling (throttling) the CPU.
    * LibreHardwareMonitor (when running): CPU package temperature/power and NVMe temperature.
    """

    name = "temperature"
    component = "thermal"

    def __init__(
        self,
        interval_ms: int,
        lhm: LhmClient | None,
        use_acpi: bool = True,
        counter_factory: Callable[[dict[str, str]], CounterReader] = PdhCounterSet,
    ) -> None:
        super().__init__(interval_ms)
        self._lhm = lhm
        self._acpi: CounterReader | None = None
        self._acpi_error: str | None = None if use_acpi else "ACPI thermal zones disabled by configuration"
        if use_acpi:
            try:
                self._acpi = counter_factory(_ACPI_PATHS)
            except TelemetryError as exc:
                self._acpi_error = exc.detail

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("thermal.zone_temperature_c", "decikelvin", SRC_ACPI)]

    def collect(self) -> list[Reading]:
        return self._acpi_readings() + self._lhm_readings()

    def _acpi_readings(self) -> list[Reading]:
        if self._acpi is None:
            return [
                self._na(
                    "thermal.zone_temperature_c",
                    "decikelvin",
                    SRC_ACPI,
                    self._acpi_error or "ACPI thermal zones not exposed by firmware",
                )
            ]
        try:
            values = self._acpi.collect()
        except TelemetryError as exc:
            return [
                Reading.failed(
                    "thermal.zone_temperature_c", self.component, "decikelvin", SRC_ACPI, exc.detail
                )
            ]
        temps = values.get("temperature")
        if not isinstance(temps, dict) or not temps:
            return [
                self._na(
                    "thermal.zone_temperature_c", "decikelvin", SRC_ACPI, "Firmware exposes no thermal zones"
                )
            ]
        out: list[Reading] = []
        limits = values.get("passive_limit")
        throttle = values.get("throttle")
        for zone, raw in temps.items():
            labels = {"zone": zone.strip("\\")}
            if raw <= 0:
                out.append(
                    Reading.failed(
                        "thermal.zone_temperature_c",
                        self.component,
                        "decikelvin",
                        SRC_ACPI,
                        "Thermal zone returned 0 K",
                        labels,
                    )
                )
            else:
                out.append(self._r("thermal.zone_temperature_c", raw, "decikelvin", SRC_ACPI, labels=labels))
            if isinstance(limits, dict) and zone in limits:
                out.append(
                    self._r("thermal.passive_limit_percent", limits[zone], "percent", SRC_ACPI, labels=labels)
                )
            if isinstance(throttle, dict) and zone in throttle:
                out.append(
                    self._r(
                        "thermal.throttle_reasons", int(throttle[zone]), "bitmask", SRC_ACPI, labels=labels
                    )
                )
        return out

    def _lhm_readings(self) -> list[Reading]:
        specs = (
            (
                "cpu.temperature_c",
                "cpu",
                CPU_KINDS,
                "Temperature",
                ("CPU Package", "Core (Tctl/Tdie)", "Core Max", "Core Average"),
                "celsius",
            ),
            ("cpu.package_power_w", "cpu", CPU_KINDS, "Power", ("CPU Package", "Package"), "W"),
            (
                "cpu.core_voltage_v",
                "cpu",
                CPU_KINDS,
                "Voltage",
                ("CPU Core", "Core (SVI2 TFN)", "Core #1 VID", "Core VID"),
                "V",
            ),
            (
                "cpu.power_limit_pl1_w",
                "cpu",
                CPU_KINDS,
                "Power",
                ("PL1", "Power Limit 1", "Package Power Limit 1", "PPT Limit"),
                "W",
            ),
            (
                "cpu.power_limit_pl2_w",
                "cpu",
                CPU_KINDS,
                "Power",
                ("PL2", "Power Limit 2", "Package Power Limit 2"),
                "W",
            ),
            (
                "disk.temperature_c",
                "storage",
                STORAGE_KINDS,
                "Temperature",
                ("Composite Temperature", "Temperature", "Temperature 1"),
                "celsius",
            ),
        )
        out: list[Reading] = []
        for metric, component, kinds, sensor_type, names, unit in specs:
            if self._lhm is None:
                reason = (
                    CPU_TEMP_NO_LHM if metric == "cpu.temperature_c" else "Hardware sensor provider disabled"
                )
                out.append(Reading.unavailable(metric, component, unit, SRC_LHM, reason))
                continue
            try:
                sensor = self._lhm.find(kinds, sensor_type, names)
            except TelemetryError as exc:
                reason = f"{CPU_TEMP_NO_LHM} ({exc.detail})" if metric == "cpu.temperature_c" else exc.detail
                out.append(Reading.unavailable(metric, component, unit, SRC_LHM, reason))
                continue
            if sensor is None:
                out.append(
                    Reading.unavailable(
                        metric, component, unit, SRC_LHM, "Sensor not exposed by this hardware"
                    )
                )
            else:
                out.append(
                    Reading(metric, component, sensor.value, unit, SRC_LHM, labels={"sensor": sensor.name})
                )
        return out

    def close(self) -> None:
        if self._acpi is not None:
            self._acpi.close()
