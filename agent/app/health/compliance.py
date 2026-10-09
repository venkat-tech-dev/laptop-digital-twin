"""Device posture evaluation (pure function; every verdict cites the measured fact behind it).

Each check yields HEALTHY / WARNING / CRITICAL or UNKNOWN when the fact could not be read. The
overall state is the worst *known* check; if no check could be evaluated the state is UNKNOWN.
Unknown checks never make a device look healthier or worse than the measured facts show.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.contracts import DeviceHealth, HealthState

SIGNATURE_WARN_DAYS = 3
SIGNATURE_CRITICAL_DAYS = 7
PENDING_REBOOT_IS_WARNING = True

_RANK = {HealthState.HEALTHY: 0, HealthState.WARNING: 1, HealthState.CRITICAL: 2}


@dataclass
class PostureFacts:
    """Inputs; ``None`` means the fact could not be read on this device."""

    av_enabled: bool | None = None  # any registered/Defender antivirus with real-time protection on
    av_up_to_date: bool | None = None
    signature_age_days: int | None = None
    firewall: dict[str, bool] | None = None  # profile -> enabled
    secure_boot: bool | None = None
    tpm_present: bool | None = None
    reboot_required: bool | None = None
    disk_critical_warning: int | None = None  # NVMe critical-warning bitmask (0 = none)


def evaluate(f: PostureFacts, now: datetime | None = None) -> DeviceHealth:
    checks: dict[str, HealthState] = {}
    reasons: list[str] = []

    def add(name: str, state: HealthState, reason: str | None = None) -> None:
        checks[name] = state
        if reason and state is not HealthState.HEALTHY:
            reasons.append(reason)

    if f.av_enabled is None:
        add("antivirus", HealthState.UNKNOWN)
    elif not f.av_enabled:
        add("antivirus", HealthState.CRITICAL, "No antivirus with real-time protection is enabled")
    else:
        add("antivirus", HealthState.HEALTHY)

    age = f.signature_age_days
    if age is None and f.av_up_to_date is None:
        add("signatures", HealthState.UNKNOWN)
    elif (age is not None and age >= SIGNATURE_CRITICAL_DAYS) or f.av_up_to_date is False:
        add(
            "signatures",
            HealthState.CRITICAL,
            f"Antivirus signatures out of date ({age} days old)"
            if age is not None
            else "Antivirus reports out-of-date signatures",
        )
    elif age is not None and age >= SIGNATURE_WARN_DAYS:
        add("signatures", HealthState.WARNING, f"Antivirus signatures are {age} days old")
    else:
        add("signatures", HealthState.HEALTHY)

    if f.firewall is None:
        add("firewall", HealthState.UNKNOWN)
    else:
        off = sorted(p for p, on in f.firewall.items() if not on)
        if off and len(off) == len(f.firewall):
            add("firewall", HealthState.CRITICAL, "Windows Firewall is off for all profiles")
        elif off:
            add("firewall", HealthState.WARNING, f"Windows Firewall is off for: {', '.join(off)}")
        else:
            add("firewall", HealthState.HEALTHY)

    if f.secure_boot is None:
        add("secure_boot", HealthState.UNKNOWN)
    else:
        add(
            "secure_boot",
            HealthState.HEALTHY if f.secure_boot else HealthState.WARNING,
            "Secure Boot is disabled",
        )

    if f.tpm_present is None:
        add("tpm", HealthState.UNKNOWN)
    else:
        add("tpm", HealthState.HEALTHY if f.tpm_present else HealthState.WARNING, "No TPM detected")

    if f.reboot_required is None:
        add("pending_reboot", HealthState.UNKNOWN)
    else:
        bad = f.reboot_required and PENDING_REBOOT_IS_WARNING
        add(
            "pending_reboot",
            HealthState.WARNING if bad else HealthState.HEALTHY,
            "A restart is required to finish updates",
        )

    if f.disk_critical_warning is None:
        add("disk", HealthState.UNKNOWN)
    else:
        add(
            "disk",
            HealthState.CRITICAL if f.disk_critical_warning else HealthState.HEALTHY,
            f"Drive reports a critical warning (0x{f.disk_critical_warning:02X})",
        )

    known = [s for s in checks.values() if s is not HealthState.UNKNOWN]
    state = max(known, key=lambda s: _RANK[s]) if known else HealthState.UNKNOWN
    return DeviceHealth(state=state, reasons=reasons, checks=checks, evaluated_at=now or datetime.now(UTC))
