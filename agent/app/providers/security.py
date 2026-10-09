from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.contracts import DeviceEvent, EventSeverity
from app.errors import TelemetryError
from app.platform import security as sec
from app.platform.wmi import WmiQueryable
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC_DEFENDER = "WMI root\\Microsoft\\Windows\\Defender MSFT_MpComputerStatus"
SRC_WSC = "Windows Security Center (root\\SecurityCenter2)"
SRC_FW = "Windows Firewall policy (HNetCfg.FwPolicy2)"
SRC_SECBOOT = "Windows registry (SecureBoot\\State)"
SRC_TPM = "Windows TPM Base Services (Tbsi_GetDeviceInfo)"


class SecurityProvider(TelemetryProvider):
    """Security posture: Defender, registered antivirus, firewall profiles, Secure Boot, TPM.

    Read-only. Emits ``security_posture_changed`` events when a protection turns on/off.
    """

    name = "security"
    component = "motherboard"
    lane = "slow"
    timeout_s = 60.0

    def __init__(
        self,
        interval_ms: int,
        wmi: WmiQueryable | None,
        *,
        firewall: Callable[[], dict[str, bool]] = sec.firewall_profiles,
        active_profiles: Callable[[], list[str]] = sec.active_firewall_profiles,
        secure_boot: Callable[[], bool] | None = None,
        tpm: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(interval_ms)
        self._wmi = wmi
        self._firewall = firewall
        self._active = active_profiles
        self._secure_boot = secure_boot
        self._tpm = tpm
        self._previous: dict[str, Any] = {}
        self._events: list[DeviceEvent] = []

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [
            MetricSpec("security.defender_realtime_enabled", "bool", SRC_DEFENDER),
            MetricSpec("security.antivirus_products", "count", SRC_WSC),
            MetricSpec("security.firewall_enabled", "bool", SRC_FW),
        ]

    def pop_events(self) -> list[DeviceEvent]:
        out, self._events = self._events, []
        return out

    def collect(self) -> list[Reading]:
        out = self._defender() + self._antivirus() + self._firewall_readings() + self._platform()
        self._detect_changes(out)
        return out

    # ------------------------------------------------------------------ readers
    def _defender(self) -> list[Reading]:
        metrics = (
            ("security.defender_service_enabled", "service_enabled", "bool"),
            ("security.defender_antivirus_enabled", "antivirus_enabled", "bool"),
            ("security.defender_realtime_enabled", "realtime_enabled", "bool"),
            ("security.defender_tamper_protected", "tamper_protected", "bool"),
            ("security.defender_signature_age_days", "signature_age_days", "days"),
        )
        if self._wmi is None:
            return [self._na(m, u, SRC_DEFENDER, "WMI unavailable") for m, _, u in metrics]
        try:
            d = sec.defender_status(self._wmi)
        except TelemetryError as exc:
            return [self._na(m, u, SRC_DEFENDER, exc.detail) for m, _, u in metrics]
        out = []
        for metric, key, unit in metrics:
            value = d.get(key)
            if value is None:
                out.append(self._na(metric, unit, SRC_DEFENDER, "Not reported by Defender"))
            else:
                out.append(self._r(metric, bool(value) if unit == "bool" else int(value), unit, SRC_DEFENDER))
        for metric, key in (
            ("security.defender_signature_updated", "signature_updated"),
            ("security.defender_last_quick_scan", "last_quick_scan"),
            ("security.defender_last_full_scan", "last_full_scan"),
        ):
            when: datetime | None = d.get(key)
            if when is None:
                out.append(self._na(metric, "timestamp", SRC_DEFENDER, "Never run or not reported"))
            else:
                out.append(self._r(metric, when.isoformat(), "timestamp", SRC_DEFENDER))
        return out

    def _antivirus(self) -> list[Reading]:
        if self._wmi is None:
            return [self._na("security.antivirus_products", "count", SRC_WSC, "WMI unavailable")]
        try:
            products = sec.antivirus_products(self._wmi)
        except TelemetryError as exc:
            reason = exc.detail
            if "namespace" in reason.lower():
                reason = "Windows Security Center not present (Windows Server or restricted edition)"
            return [self._na("security.antivirus_products", "count", SRC_WSC, reason)]
        out = [self._r("security.antivirus_products", len(products), "count", SRC_WSC)]
        for p in products:
            labels = {"product": p.name}
            out.append(self._r("security.antivirus_enabled", p.enabled, "bool", SRC_WSC, labels=labels))
            out.append(self._r("security.antivirus_up_to_date", p.up_to_date, "bool", SRC_WSC, labels=labels))
        return out

    def _firewall_readings(self) -> list[Reading]:
        try:
            profiles = self._firewall()
        except TelemetryError as exc:
            return [self._na("security.firewall_enabled", "bool", SRC_FW, exc.detail)]
        out = [
            self._r("security.firewall_enabled", on, "bool", SRC_FW, labels={"profile": name})
            for name, on in profiles.items()
        ]
        active = self._active()
        if active:
            out.append(self._r("security.firewall_active_profile", ",".join(active), "state", SRC_FW))
        return out

    def _platform(self) -> list[Reading]:
        out: list[Reading] = []
        if self._secure_boot is not None:
            try:
                out.append(self._r("security.secure_boot_enabled", self._secure_boot(), "bool", SRC_SECBOOT))
            except TelemetryError as exc:
                out.append(self._na("security.secure_boot_enabled", "bool", SRC_SECBOOT, exc.detail))
        if self._tpm is not None:
            try:
                tpm = self._tpm()
                out.append(self._r("security.tpm_present", bool(tpm["present"]), "bool", SRC_TPM))
                if tpm["present"]:
                    out.append(self._r("security.tpm_version", str(tpm["version"]), "version", SRC_TPM))
                else:
                    out.append(self._na("security.tpm_version", "version", SRC_TPM, "No TPM found"))
            except TelemetryError as exc:
                out.append(self._na("security.tpm_present", "bool", SRC_TPM, exc.detail))
        return out

    # ------------------------------------------------------------------ change events
    def _detect_changes(self, readings: list[Reading]) -> None:
        watched = {
            "security.defender_realtime_enabled",
            "security.defender_antivirus_enabled",
            "security.firewall_enabled",
            "security.antivirus_enabled",
            "security.secure_boot_enabled",
        }
        now = datetime.now(UTC)
        for r in readings:
            if r.metric not in watched or r.value is None:
                continue
            key = r.metric + "".join(f"|{k}={v}" for k, v in sorted(r.labels.items()))
            before = self._previous.get(key)
            self._previous[key] = r.value
            if before is None or before == r.value:
                continue
            turned_off = r.value is False
            self._events.append(
                DeviceEvent(
                    type="security_posture_changed",
                    severity=EventSeverity.WARNING if turned_off else EventSeverity.INFO,
                    timestamp=now,
                    source=r.source,
                    message=f"{r.metric.removeprefix('security.').replace('_', ' ')} "
                    f"{'disabled' if turned_off else 'enabled'}"
                    + (f" ({', '.join(f'{k}={v}' for k, v in r.labels.items())})" if r.labels else ""),
                    data={"metric": r.metric, "value": bool(r.value), **{k: v for k, v in r.labels.items()}},
                )
            )
