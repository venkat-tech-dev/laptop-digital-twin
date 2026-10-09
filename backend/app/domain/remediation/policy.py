"""Remediation policy and permissions. Conservative by default: every action needs human approval,
auto-remediation is off, CRITICAL is disabled, nothing runs while a kill switch is on.

Approval mode resolution (most specific wins): device override > device-group override > action override
> per-risk default. ``AUTO_APPROVE_LOW_RISK`` only ever applies to LOW actions that change nothing on
the endpoint, are marked auto-eligible in the catalog, while auto-remediation is enabled and the diagnosis
confidence reaches the threshold; otherwise it degrades to manual approval.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.remediation.catalog import (
    APP_ID,
    EXE_NAME,
    KNOWN_APPLICATIONS,
    ActionDefinition,
    KnownApplication,
    Risk,
)

MODES = ("MANUAL_APPROVAL", "AUTO_APPROVE_LOW_RISK", "MANUAL_APPROVAL_HIGHER_RISK", "DISABLED")

#: permission sets per role (server-side; the UI only mirrors them)
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"remediation.view"}),
    "employee": frozenset({"remediation.view", "remediation.request"}),
    "operator": frozenset(
        {
            "remediation.view",
            "remediation.request",
            "remediation.approve",
            "remediation.execute",
            "remediation.cancel",
        }
    ),
    "admin": frozenset(
        {
            "remediation.view",
            "remediation.request",
            "remediation.approve",
            "remediation.execute",
            "remediation.cancel",
            "remediation.rollback",
            "remediation.manage_policy",
            "remediation.manage_actions",
        }
    ),
}
#: highest risk each role may approve / request
MAX_APPROVE_RISK = {"operator": Risk.MEDIUM, "admin": Risk.CRITICAL}
MAX_REQUEST_RISK = {"employee": Risk.LOW, "operator": Risk.MEDIUM, "admin": Risk.CRITICAL}


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


@dataclass
class MaintenanceWindow:
    group: str = "*"  # device group (workspace / department) or "*"
    days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])  # Monday = 0
    start: str = "22:00"
    end: str = "23:00"
    timezone: str = "UTC"

    def contains(self, at: datetime) -> bool:
        try:
            local = at.astimezone(ZoneInfo(self.timezone))
        except ZoneInfoNotFoundError:
            local = at
        s, e = time.fromisoformat(self.start), time.fromisoformat(self.end)
        t = local.time()
        if s <= e:
            return local.weekday() in self.days and s <= t < e
        # crosses midnight: the part after midnight belongs to the previous day's window
        if t >= s:
            return local.weekday() in self.days
        return t < e and (local.weekday() - 1) % 7 in self.days


@dataclass
class KillSwitches:
    global_: bool = False
    tenants: list[str] = field(default_factory=list)
    device_groups: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)

    def blocking(self, tenant: str, group: str | None, action: str, device: str) -> str | None:
        if self.global_:
            return "GLOBAL_REMEDIATION_KILL_SWITCH"
        if tenant in self.tenants:
            return "TENANT_REMEDIATION_KILL_SWITCH"
        if group and group in self.device_groups:
            return "DEVICE_GROUP_KILL_SWITCH"
        if action in self.actions:
            return "ACTION_TYPE_KILL_SWITCH"
        if device in self.devices:
            return "DEVICE_KILL_SWITCH"
        return None


@dataclass
class RemediationPolicy:
    version: int = 0
    risk_modes: dict[str, str] = field(
        default_factory=lambda: {
            "LOW": "MANUAL_APPROVAL",
            "MEDIUM": "MANUAL_APPROVAL",
            "HIGH": "MANUAL_APPROVAL",
            "CRITICAL": "DISABLED",
        }
    )
    action_modes: dict[str, str] = field(default_factory=dict)
    group_modes: dict[str, dict[str, str]] = field(default_factory=dict)  # group -> {action|"*": mode}
    device_modes: dict[str, dict[str, str]] = field(default_factory=dict)  # device -> {action|"*": mode}
    auto_remediation_enabled: bool = False
    auto_min_confidence: float = 0.75
    four_eyes_min_risk: str = "HIGH"  # requester may not approve own request at/above this risk
    approval_ttl_s: int = 1800
    envelope_ttl_s: int = 120
    offline_reapproval_s: int = 3600  # approved, then offline longer than this -> needs a new approval
    device_daily_budget: int = 10
    circuit_failures: int = 3
    circuit_window_s: int = 86400
    circuit_open_s: int = 86400
    fleet_max_concurrent: int = 20
    maintenance_windows: list[MaintenanceWindow] = field(default_factory=list)
    critical_bypasses_maintenance: bool = False
    kill_switches: KillSwitches = field(default_factory=KillSwitches)
    extra_applications: list[dict[str, Any]] = field(default_factory=list)
    disabled_applications: list[str] = field(default_factory=list)
    updated_at: str | None = None
    updated_by: str | None = None

    # ------------------------------------------------------------------ serialisation
    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["kill_switches"]["global"] = d["kill_switches"].pop("global_")
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RemediationPolicy:
        """Validated construction (admin input). Raises ValueError on anything unexpected."""
        raw = dict(raw)
        ks = dict(raw.pop("kill_switches", None) or {})
        if "global" in ks:
            ks["global_"] = ks.pop("global")
        windows = [MaintenanceWindow(**w) for w in raw.pop("maintenance_windows", None) or []]
        known = set(cls.__dataclass_fields__)
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"unknown policy keys: {sorted(unknown)}")
        p = cls(**raw, kill_switches=KillSwitches(**ks), maintenance_windows=windows)
        p.check()
        return p

    def check(self) -> None:
        for mode in [
            *self.risk_modes.values(),
            *self.action_modes.values(),
            *(m for d in (*self.group_modes.values(), *self.device_modes.values()) for m in d.values()),
        ]:
            if mode not in MODES:
                raise ValueError(f"unknown approval mode {mode}")
        if set(self.risk_modes) != {r.value for r in Risk}:
            raise ValueError("risk_modes must define LOW, MEDIUM, HIGH and CRITICAL")
        if self.four_eyes_min_risk not in {r.value for r in Risk}:
            raise ValueError("four_eyes_min_risk must be a risk level")
        for name, lo, hi in (
            ("approval_ttl_s", 60, 86400),
            ("envelope_ttl_s", 30, 3600),
            ("offline_reapproval_s", 60, 7 * 86400),
            ("device_daily_budget", 1, 1000),
            ("circuit_failures", 1, 100),
            ("circuit_window_s", 60, 30 * 86400),
            ("circuit_open_s", 60, 30 * 86400),
            ("fleet_max_concurrent", 1, 10_000),
        ):
            v = getattr(self, name)
            if not lo <= v <= hi:
                raise ValueError(f"{name} must be between {lo} and {hi}")
        if not 0 <= self.auto_min_confidence <= 1:
            raise ValueError("auto_min_confidence must be between 0 and 1")
        for w in self.maintenance_windows:
            time.fromisoformat(w.start)
            time.fromisoformat(w.end)
            if not w.days or any(d not in range(7) for d in w.days):
                raise ValueError("maintenance window days must be 0-6 (Monday = 0)")
        for a in self.extra_applications:
            self._application(a)

    @staticmethod
    def _application(a: dict[str, Any]) -> KnownApplication:
        app_id, exes = str(a.get("application_id", "")), [str(x).lower() for x in a.get("executables") or []]
        if not APP_ID.match(app_id):
            raise ValueError(f"invalid application_id {app_id!r}")
        if not exes or any(not EXE_NAME.match(x) for x in exes):
            raise ValueError("executables must be plain image names such as 'example.exe' (no paths)")
        return KnownApplication(
            app_id,
            str(a.get("name") or app_id)[:80],
            tuple(exes),
            str(a.get("impact") or "The application restarts.")[:200],
        )

    # ------------------------------------------------------------------ decisions
    def applications(self) -> dict[str, KnownApplication]:
        apps = dict(KNOWN_APPLICATIONS)
        for a in self.extra_applications:
            app = self._application(a)
            apps[app.application_id] = app
        for app_id in self.disabled_applications:
            apps.pop(app_id, None)
        return apps

    def mode_for(self, action: ActionDefinition, device_id: str, group: str | None) -> str:
        for scope in (self.device_modes.get(device_id), self.group_modes.get(group or "")):
            if scope:
                m = scope.get(action.action_id) or scope.get("*")
                if m:
                    return m
        return self.action_modes.get(action.action_id) or self.risk_modes[action.risk.value]

    def approval(
        self, action: ActionDefinition, device_id: str, group: str | None, diagnosis_confidence: float | None
    ) -> tuple[str, bool, str]:
        """-> (mode, requires_approval, reason). Mode DISABLED means it may not be proposed at all."""
        if not action.enabled:
            return "DISABLED", True, action.disabled_reason or "action disabled"
        mode = self.mode_for(action, device_id, group)
        if (
            action.risk == Risk.CRITICAL
            and mode != "DISABLED"
            and self.risk_modes["CRITICAL"] == "DISABLED"
            and self.action_modes.get(action.action_id) is None
        ):
            mode = "DISABLED"  # CRITICAL needs an explicit per-action enablement
        if mode == "DISABLED":
            return mode, True, "disabled by remediation policy"
        auto = (
            mode == "AUTO_APPROVE_LOW_RISK"
            and self.auto_remediation_enabled
            and action.risk == Risk.LOW
            and action.auto_eligible
            and not action.changes_state
            and diagnosis_confidence is not None
            and diagnosis_confidence >= self.auto_min_confidence
        )
        if auto:
            return mode, False, "auto-approved: low risk, no state change, explicit policy, high confidence"
        if mode == "AUTO_APPROVE_LOW_RISK":
            return "MANUAL_APPROVAL", True, "auto-approval conditions not met (needs manual approval)"
        return mode, True, "human approval required"

    def four_eyes(self, action: ActionDefinition) -> bool:
        return action.risk.rank >= Risk(self.four_eyes_min_risk).rank

    def in_maintenance(self, group: str | None, at: datetime) -> bool:
        windows = [w for w in self.maintenance_windows if w.group in ("*", group)]
        return any(w.contains(at) for w in windows)


def can_approve(
    role: str,
    subject: str,
    action: ActionDefinition,
    requested_by: str,
    policy: RemediationPolicy,
    *,
    permissions: frozenset[str] | None = None,
    org_role: str | None = None,
) -> str | None:
    """None if allowed, else the reason (server-side; separation of duties).

    Phase 9: with ``permissions`` the organisation's permission model decides (remediation.approve) and the
    organisation role sets the risk ceiling (``APPROVE_CEILING``; platform super-admins: CRITICAL)."""
    if permissions is not None:
        from app.domain.tenancy.permissions import APPROVE_CEILING

        if "remediation.approve" not in permissions:
            return "your role may not approve remediation"
        ceiling = (
            Risk.CRITICAL if org_role == "platform" else Risk(APPROVE_CEILING.get(org_role or "", "LOW"))
        )
        if action.risk.rank > ceiling.rank:
            return f"{action.risk.value} risk actions need a higher administrator's approval"
    else:
        if not has_permission(role, "remediation.approve"):
            return "your role may not approve remediation"
        if action.risk.rank > MAX_APPROVE_RISK.get(role, Risk.LOW).rank:
            return f"{action.risk.value} risk actions need an administrator's approval"
    if policy.four_eyes(action) and requested_by == subject:
        return "four-eyes rule: the requester may not approve this risk level"
    return None


def can_request(role: str, action: ActionDefinition) -> str | None:
    if not has_permission(role, "remediation.request"):
        return "your role may not request remediation"
    if action.risk.rank > MAX_REQUEST_RISK.get(role, Risk.LOW).rank:
        return f"your role may not request {action.risk.value} risk actions"
    return None
