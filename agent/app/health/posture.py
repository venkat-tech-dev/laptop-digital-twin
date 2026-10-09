"""Tracks the latest posture-relevant metrics across collectors and evaluates DEVICE_HEALTH."""

from __future__ import annotations

from datetime import UTC, datetime

from app.contracts import Availability, DeviceEvent, DeviceHealth, EventSeverity, HealthState, MetricSample
from app.health.compliance import PostureFacts, evaluate

_WATCHED_PREFIXES = ("security.", "system.update_reboot_required", "disk.critical_warning")


class PostureTracker:
    def __init__(self) -> None:
        self._latest: dict[tuple[str, tuple[tuple[str, str], ...]], MetricSample] = {}
        self._last_state: HealthState | None = None

    def observe(self, samples: list[MetricSample]) -> None:
        for s in samples:
            if s.metric.startswith(_WATCHED_PREFIXES):
                self._latest[(s.metric, tuple(sorted(s.labels.items())))] = s

    def _values(self, metric: str) -> list[MetricSample]:
        return [
            s
            for (m, _), s in self._latest.items()
            if m == metric and s.availability is Availability.AVAILABLE
        ]

    def _one(self, metric: str) -> object | None:
        vals = self._values(metric)
        return vals[0].value if vals else None

    def facts(self) -> PostureFacts:
        av_flags = [bool(s.value) for s in self._values("security.antivirus_enabled")]
        av_current = [bool(s.value) for s in self._values("security.antivirus_up_to_date")]
        realtime = self._one("security.defender_realtime_enabled")
        av_enabled: bool | None
        if av_flags:
            av_enabled = any(av_flags)
        elif realtime is not None:
            av_enabled = bool(realtime)
        else:
            av_enabled = None
        fw = {s.labels.get("profile", "?"): bool(s.value) for s in self._values("security.firewall_enabled")}
        age = self._one("security.defender_signature_age_days")
        disk = [int(s.value) for s in self._values("disk.critical_warning") if isinstance(s.value, int)]
        secure_boot = self._one("security.secure_boot_enabled")
        tpm = self._one("security.tpm_present")
        reboot = self._one("system.update_reboot_required")
        return PostureFacts(
            av_enabled=av_enabled,
            av_up_to_date=all(av_current) if av_current else None,
            signature_age_days=int(age) if isinstance(age, (int, float)) else None,
            firewall=fw or None,
            secure_boot=bool(secure_boot) if secure_boot is not None else None,
            tpm_present=bool(tpm) if tpm is not None else None,
            reboot_required=bool(reboot) if reboot is not None else None,
            disk_critical_warning=max(disk) if disk else None,
        )

    def evaluate(self) -> tuple[DeviceHealth, list[DeviceEvent]]:
        health = evaluate(self.facts())
        events: list[DeviceEvent] = []
        # The first evaluations after start-up move from UNKNOWN as collectors report in: not a change.
        known_before = self._last_state not in (None, HealthState.UNKNOWN)
        previous = self._last_state.value if self._last_state else "?"
        if known_before and health.state is not HealthState.UNKNOWN and health.state is not self._last_state:
            worse = health.state in (HealthState.WARNING, HealthState.CRITICAL)
            events.append(
                DeviceEvent(
                    type="device_health_changed",
                    severity=EventSeverity.WARNING if worse else EventSeverity.INFO,
                    timestamp=datetime.now(UTC),
                    source="ldt-agent posture evaluation",
                    message=f"Device health {previous} -> {health.state.value}"
                    + (f": {health.reasons[0]}" if health.reasons else ""),
                    data={
                        "from": self._last_state.value if self._last_state else None,
                        "to": health.state.value,
                    },
                )
            )
        if health.state is not HealthState.UNKNOWN:
            self._last_state = health.state
        return health, events
