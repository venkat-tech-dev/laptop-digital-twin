"""Enterprise policies: typed schemas, deterministic inheritance, locks, validation, versions.

Precedence (most specific wins):

    device  >  device group (lowest priority number, then id)  >  team  >  department  >  business unit
            >  organization  >  platform default

A field listed in ``locked`` at a scope cannot be overridden below it: validation refuses such a draft and
evaluation ignores the override (defence in depth). Every value is validated against its field type and
range; unknown fields are refused. A policy is a sequence of immutable versions: DRAFT -> PUBLISHED; a newer
publication ARCHIVES the previous one; rollback publishes an older version's body as a new version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

SCOPES = ("organization", "business_unit", "department", "team", "device_group", "device")
SCOPE_RANK = {s: i for i, s in enumerate(SCOPES)}
RISKS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL", "OFF")
MODES = ("MANUAL_APPROVAL", "AUTO_APPROVE_LOW_RISK", "DISABLED")
MFA = ("MFA_OPTIONAL", "MFA_REQUIRED", "MFA_PROVIDER_MANAGED")
CONTROLS = ("antivirus", "realtime_protection", "firewall", "secure_boot", "tpm")


class PolicyStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


@dataclass(frozen=True)
class F:
    """Field: type (bool, int, float, enum, version, list_enum, list_version), range, default."""

    type: str
    default: Any
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    doc: str = ""


SCHEMAS: dict[str, dict[str, F]] = {
    "remediation": {
        "auto_remediation_enabled": F("bool", False, doc="LOW, no-change actions may be auto-approved"),
        "auto_min_confidence": F("float", 0.75, 0.5, 1.0),
        "low_mode": F("enum", "MANUAL_APPROVAL", choices=MODES),
        "medium_mode": F("enum", "MANUAL_APPROVAL", choices=("MANUAL_APPROVAL", "DISABLED")),
        "high_mode": F("enum", "MANUAL_APPROVAL", choices=("MANUAL_APPROVAL", "DISABLED")),
        "critical_mode": F("enum", "DISABLED", choices=("MANUAL_APPROVAL", "DISABLED")),
        "four_eyes_min_risk": F("enum", "HIGH", choices=RISKS),
        "approval_ttl_s": F("int", 1800, 60, 86400),
        "kill_switch": F("bool", False, doc="no new remediation may begin in this scope"),
    },
    "diagnosis": {
        "enabled": F("bool", True),
        "auto_min_severity": F("enum", "HIGH", choices=SEVERITIES),
    },
    "notifications": {
        "max_per_user_hour": F("int", 30, 1, 1000),
        "allowed_channels": F(
            "list_enum",
            ["in_app", "browser", "windows", "email", "webhook"],
            choices=("in_app", "browser", "windows", "email", "webhook"),
        ),
    },
    "security": {
        "mfa": F("enum", "MFA_OPTIONAL", choices=MFA),
        "session_ttl_minutes": F("int", 480, 5, 1440),
        "reauth_minutes": F("int", 15, 1, 240, doc="recent sign-in required for sensitive administration"),
        "local_login_allowed": F("bool", True),
    },
    "agent": {
        "minimum_version": F("version", "1.4.0"),
        "recommended_version": F("version", "1.6.0"),
        "deprecated_versions": F("list_version", []),
        "blocked_versions": F("list_version", []),
        "reject_blocked": F("bool", False, doc="refuse telemetry from blocked agent versions"),
        "telemetry_interval_ms": F("int", 5000, 1000, 60000),
    },
    "enrollment": {
        "token_max_ttl_hours": F("int", 24, 1, 168),
        "allow_multi_use": F("bool", False),
        "credential_ttl_days": F("int", 90, 7, 730),
    },
    "retention": {
        "notifications_days": F(
            "int", 90, 7, 3650, doc="delivered notifications; capped by the platform setting"
        ),
        "diagnoses_days": F("int", 90, 7, 3650),
        "alerts_days": F("int", 365, 30, 3650, doc="closed alerts; capped by the platform setting"),
        "raw_telemetry_days": F("int", 30, 1, 3650, doc="raw samples; capped by RETENTION_DAYS"),
    },
    "compliance": {
        "required_controls": F(
            "list_enum", ["antivirus", "realtime_protection", "firewall"], choices=CONTROLS
        ),
        "max_telemetry_age_s": F("int", 900, 60, 7 * 86400),
        "exempt": F("bool", False),
    },
}
KINDS = tuple(SCHEMAS)
#: kinds that only make sense organisation-wide (identity is per organisation)
ORG_ONLY = {"security", "enrollment", "retention"}


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int("".join(c for c in p if c.isdigit()) or 0) for p in str(v).split(".")[:3])


def _valid_version(v: Any) -> bool:
    return (
        isinstance(v, str)
        and 1 <= len(v) <= 32
        and all(p.isdigit() for p in v.split("."))
        and v.count(".") <= 3
    )


def check_value(kind: str, name: str, value: Any) -> str | None:
    f = SCHEMAS[kind].get(name)
    if f is None:
        return f"{kind}.{name} is not a policy field"
    t = f.type
    if t == "bool" and not isinstance(value, bool):
        return f"{name} must be true or false"
    if t in ("int", "float"):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or (t == "int" and not float(value).is_integer())
        ):
            return f"{name} must be a{'n integer' if t == 'int' else ' number'}"
        if (f.lo is not None and value < f.lo) or (f.hi is not None and value > f.hi):
            return f"{name} must be between {f.lo:g} and {f.hi:g}"
    if t == "enum" and value not in f.choices:
        return f"{name} must be one of {', '.join(f.choices)}"
    if t == "list_enum" and (not isinstance(value, list) or any(v not in f.choices for v in value)):
        return f"{name} must be a list of {', '.join(f.choices)}"
    if t == "version" and not _valid_version(value):
        return f"{name} must be a version like 1.6.0"
    if t == "list_version" and (
        not isinstance(value, list) or len(value) > 50 or not all(_valid_version(v) for v in value)
    ):
        return f"{name} must be a list of versions"
    return None


@dataclass
class Policy:
    policy_id: str  # stable id of the policy (all versions share it)
    org_id: str
    scope_type: str
    scope_id: str
    kind: str
    version: int
    status: PolicyStatus
    body: dict[str, Any]
    locked: list[str] = field(default_factory=list)
    created_by: str = ""
    created_at: datetime | None = None
    updated_by: str | None = None
    updated_at: datetime | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    note: str = ""

    def active(self, now: datetime) -> bool:
        return (
            self.status == PolicyStatus.PUBLISHED
            and (self.effective_from is None or self.effective_from <= now)
            and (self.effective_until is None or now < self.effective_until)
        )

    def public(self) -> dict[str, Any]:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        return {
            "policy_id": self.policy_id,
            "org_id": self.org_id,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "kind": self.kind,
            "version": self.version,
            "status": self.status.value,
            "body": self.body,
            "locked": self.locked,
            "created_by": self.created_by,
            "created_at": iso(self.created_at),
            "updated_by": self.updated_by,
            "updated_at": iso(self.updated_at),
            "effective_from": iso(self.effective_from),
            "effective_until": iso(self.effective_until),
            "note": self.note,
        }


def validate(p: Policy, published: list[Policy]) -> tuple[list[str], list[str]]:
    """-> (errors, warnings). ``published`` = currently active policies of the organisation (same kind)."""
    errors: list[str] = []
    warnings: list[str] = []
    if p.kind not in SCHEMAS:
        return [f"unknown policy kind {p.kind}"], []
    if p.scope_type not in SCOPES:
        errors.append(f"unknown scope {p.scope_type}")
    if p.kind in ORG_ONLY and p.scope_type != "organization":
        errors.append(f"{p.kind} policies apply to the whole organization only")
    if not isinstance(p.body, dict) or len(p.body) > 50:
        return [*errors, "body must be an object"], warnings
    for name, value in p.body.items():
        msg = check_value(p.kind, name, value)
        if msg:
            errors.append(msg)
    for name in p.locked:
        if name not in SCHEMAS[p.kind]:
            errors.append(f"cannot lock unknown field {name}")
    # locks above this scope (organization locks apply to every lower scope, etc.)
    for other in published:
        if other.kind != p.kind or SCOPE_RANK.get(other.scope_type, 99) >= SCOPE_RANK.get(p.scope_type, -1):
            continue
        clash = sorted(set(other.locked) & set(p.body))
        if clash:
            errors.append(f"{', '.join(clash)} locked by the {other.scope_type} policy (v{other.version})")
    if errors:
        return errors, warnings
    b = p.body
    if p.kind == "remediation":
        if b.get("critical_mode") == "MANUAL_APPROVAL":
            warnings.append(
                "CRITICAL actions become approvable: only organization administrators can approve them"
            )
        if (
            b.get("auto_remediation_enabled")
            and b.get("low_mode", "MANUAL_APPROVAL") != "AUTO_APPROVE_LOW_RISK"
        ):
            warnings.append("auto_remediation_enabled has no effect unless low_mode is AUTO_APPROVE_LOW_RISK")
        if b.get("four_eyes_min_risk") in ("HIGH", "CRITICAL") and b.get("high_mode") == "MANUAL_APPROVAL":
            pass
    if p.kind == "agent":
        mn, rec = b.get("minimum_version"), b.get("recommended_version")
        if mn and rec and version_tuple(mn) > version_tuple(rec):
            errors.append("minimum_version must not be newer than recommended_version")
        if rec and rec in (b.get("blocked_versions") or []):
            errors.append("the recommended version cannot be blocked")
        if b.get("reject_blocked") and b.get("blocked_versions"):
            warnings.append("devices on blocked versions will stop sending telemetry until upgraded")
    if p.kind == "security":
        if b.get("mfa") == "MFA_REQUIRED" and b.get("local_login_allowed", True):
            warnings.append(
                "local accounts without an authenticator app must enroll one before signing in again"
            )
        if b.get("local_login_allowed") is False:
            warnings.append("only identity-provider sign-in will work; keep a break-glass owner account")
    if p.kind == "compliance" and b.get("exempt"):
        warnings.append("devices in this scope will be reported as EXEMPT")
    return errors, warnings


def platform_default(kind: str) -> dict[str, Any]:
    return {
        k: (list(f.default) if isinstance(f.default, list) else f.default) for k, f in SCHEMAS[kind].items()
    }


def effective(kind: str, chain: list[Policy]) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge platform default with ``chain`` (ordered least -> most specific). -> (values, provenance)."""
    values = platform_default(kind)
    source = {k: "platform" for k in values}
    locked: set[str] = set()
    for p in chain:
        for name, value in p.body.items():
            if name in locked or name not in values or check_value(kind, name, value):
                continue  # locked above, unknown, or invalid: ignored
            values[name] = value
            source[name] = f"{p.scope_type}:{p.scope_id} v{p.version}"
        locked |= set(p.locked)
    return values, source
