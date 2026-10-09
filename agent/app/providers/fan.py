from __future__ import annotations

from app.errors import TelemetryError
from app.platform.lhm import LhmClient
from app.platform.wmi import WmiQueryable
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC_LHM = "LibreHardwareMonitor"
SRC_WMI = "WMI Win32_Fan"

NO_FAN_REASON = (
    "Fan tachometer is not exposed to Windows on this machine (Win32_Fan is empty). "
    "Fan RPM requires the embedded controller to be read by LibreHardwareMonitor running as administrator."
)


class FanProvider(TelemetryProvider):
    """Fan speed. Never animated or estimated when no tachometer reading exists."""

    name = "fan"
    lane = "wmi"
    timeout_s = 15.0
    component = "cooling"

    def __init__(self, interval_ms: int, lhm: LhmClient | None, wmi: WmiQueryable | None) -> None:
        super().__init__(interval_ms)
        self._lhm = lhm
        self._wmi = wmi

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("fan.speed_rpm", "rpm", SRC_LHM)]

    def collect(self) -> list[Reading]:
        reasons: list[str] = []
        if self._lhm is not None:
            try:
                fans = [s for s in self._lhm.sensors() if s.sensor_type == "Fan" and s.value is not None]
                if fans:
                    return [
                        self._r("fan.speed_rpm", s.value, "rpm", SRC_LHM, labels={"fan": s.name or f"fan{i}"})
                        for i, s in enumerate(fans)
                    ]
                reasons.append("LibreHardwareMonitor reports no fan sensors for this embedded controller")
            except TelemetryError as exc:
                reasons.append(exc.detail)
        if self._wmi is not None:
            try:
                rows = self._wmi.query("SELECT Name, DesiredSpeed, ActiveCooling FROM Win32_Fan")
                speeds = [r for r in rows if isinstance(r.get("DesiredSpeed"), int) and r["DesiredSpeed"] > 0]
                if speeds:
                    return [
                        self._r(
                            "fan.speed_rpm",
                            r["DesiredSpeed"],
                            "rpm",
                            SRC_WMI,
                            labels={"fan": str(r.get("Name"))},
                        )
                        for r in speeds
                    ]
            except TelemetryError as exc:
                reasons.append(exc.detail)
        detail = NO_FAN_REASON + (f" [{'; '.join(reasons)}]" if reasons else "")
        return [self._na("fan.speed_rpm", "rpm", SRC_LHM, detail)]
