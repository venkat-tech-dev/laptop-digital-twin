"""Device compliance from what the platform actually knows.

Every check is PASS, FAIL, UNKNOWN (the agent reports this kind of data but no value has arrived) or
NOT_SUPPORTED (this agent does not collect it, e.g. disk encryption). Nothing is guessed.

Overall: EXEMPT (policy), NON_COMPLIANT (any required check FAILS), COMPLIANT (all required PASS),
PARTIALLY_COMPLIANT (no failures, some required checks UNKNOWN), UNKNOWN (no data at all).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.governance.policies import version_tuple

CONTROL_FIELDS = {
    "antivirus": "security.antivirus_enabled",
    "realtime_protection": "security.realtime_protection",
    "firewall": "security.firewall_enabled",
    "secure_boot": "security.secure_boot",
    "tpm": "security.tpm_present",
}
SUPPORTED_OS = ("windows 10", "windows 11")


@dataclass
class DeviceFacts:
    device_id: str
    lifecycle: str
    enrolled: bool
    credential_valid: bool | None  # None: no per-device credential (legacy enrollment key)
    agent_version: str | None
    os_name: str | None
    telemetry_age_s: float | None
    controls: dict[str, bool | None] = field(default_factory=dict)  # control -> state (None = not reported)


def _check(name: str, state: str, detail: str, required: bool = True) -> dict[str, Any]:
    return {"check": name, "state": state, "detail": detail, "required": required}


def evaluate(
    f: DeviceFacts, agent_policy: dict[str, Any], compliance_policy: dict[str, Any]
) -> dict[str, Any]:
    if compliance_policy.get("exempt"):
        return {"status": "EXEMPT", "checks": [], "reasons": ["exempt by compliance policy"]}
    checks = []
    checks.append(
        _check(
            "device_enrolled",
            "PASS" if f.enrolled else "FAIL",
            "enrolled in this organization" if f.enrolled else "not enrolled",
        )
    )
    checks.append(
        _check("lifecycle_active", "PASS" if f.lifecycle == "ACTIVE" else "FAIL", f"lifecycle {f.lifecycle}")
    )
    if f.credential_valid is None:
        checks.append(
            _check("credential_valid", "UNKNOWN", "legacy shared enrollment key; no per-device credential")
        )
    else:
        checks.append(
            _check(
                "credential_valid",
                "PASS" if f.credential_valid else "FAIL",
                "per-device credential valid" if f.credential_valid else "credential expired or revoked",
            )
        )
    v = f.agent_version
    if not v:
        checks.append(_check("agent_installed", "FAIL", "no agent has reported"))
    else:
        checks.append(_check("agent_installed", "PASS", f"agent {v}"))
        vt = version_tuple(v)
        if v in (agent_policy.get("blocked_versions") or []):
            checks.append(_check("agent_version_supported", "FAIL", f"agent {v} is blocked"))
        elif vt < version_tuple(agent_policy.get("minimum_version", "0")):
            checks.append(
                _check(
                    "agent_version_supported",
                    "FAIL",
                    f"required agent version {agent_policy['minimum_version']}, installed {v}",
                )
            )
        else:
            note = " (deprecated)" if v in (agent_policy.get("deprecated_versions") or []) else ""
            older = vt < version_tuple(agent_policy.get("recommended_version", "0"))
            checks.append(
                _check(
                    "agent_version_supported",
                    "PASS",
                    f"agent {v}{note}"
                    + (f"; {agent_policy['recommended_version']} recommended" if older else ""),
                )
            )
    age, limit = f.telemetry_age_s, float(compliance_policy.get("max_telemetry_age_s", 900))
    if age is None:
        checks.append(_check("telemetry_healthy", "UNKNOWN", "no telemetry received yet"))
    else:
        checks.append(
            _check(
                "telemetry_healthy",
                "PASS" if age <= limit else "FAIL",
                f"last telemetry {age:.0f}s ago (limit {limit:.0f}s)",
            )
        )
    os_name = (f.os_name or "").lower()
    if not os_name:
        checks.append(_check("os_supported", "UNKNOWN", "operating system not reported"))
    else:
        ok = any(s in os_name for s in SUPPORTED_OS)
        checks.append(_check("os_supported", "PASS" if ok else "FAIL", f.os_name or ""))
    required = set(compliance_policy.get("required_controls") or [])
    for control in CONTROL_FIELDS:
        state = f.controls.get(control)
        req = control in required
        if state is None:
            checks.append(_check(f"control_{control}", "UNKNOWN", "not reported by the agent", req))
        else:
            checks.append(
                _check(f"control_{control}", "PASS" if state else "FAIL", "on" if state else "off", req)
            )
    checks.append(
        _check(
            "disk_encryption", "NOT_SUPPORTED", "this agent does not collect disk-encryption status", False
        )
    )
    req_checks = [c for c in checks if c["required"]]
    if any(c["state"] == "FAIL" for c in req_checks):
        status = "NON_COMPLIANT"
    elif (
        all(c["state"] == "UNKNOWN" for c in req_checks if c["check"].startswith(("telemetry", "control")))
        and not v
    ):
        status = "UNKNOWN"
    elif any(c["state"] == "UNKNOWN" for c in req_checks):
        status = "PARTIALLY_COMPLIANT"
    else:
        status = "COMPLIANT"
    reasons = [f"{c['check']}: {c['detail']}" for c in req_checks if c["state"] in ("FAIL", "UNKNOWN")]
    return {"status": status, "checks": checks, "reasons": reasons}


def controls_from_state(state: dict[str, Any]) -> dict[str, bool | None]:
    """Twin document state -> control states (value None when the field is missing or unavailable)."""
    out: dict[str, bool | None] = {}
    for control, path in CONTROL_FIELDS.items():
        fv = state.get(path)
        value = fv.get("value") if isinstance(fv, dict) else None
        out[control] = None if value is None else (value in (True, 1, "on", "enabled", "true", "ON", 1.0))
    return out
