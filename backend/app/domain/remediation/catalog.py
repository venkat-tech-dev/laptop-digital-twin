"""The Action Catalog: the complete list of what remediation can ever do.

Each action is declared here with its risk, permissions, typed parameters, preconditions, timeouts,
verification, rollback and cooldown. Actions that cannot (yet) be implemented safely are listed with
``enabled=False`` and an explicit reason so that the UI and API are honest about them; they can never
be proposed, approved or executed.

Known applications are identified by an id from a curated registry. Clients and AI only ever pass the
id; the executable name lives here and on the endpoint's local allowlist, never in a request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class Risk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return ("LOW", "MEDIUM", "HIGH", "CRITICAL").index(self.value)


ROLLBACK_NOT_AVAILABLE = "NOT_AVAILABLE"  # the change cannot be undone
ROLLBACK_NOT_NEEDED = "NOT_NEEDED"  # nothing on the endpoint changes
APP_ID = re.compile(r"^[a-z0-9]+(\.[a-z0-9-]+){1,3}$")
EXE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,62}\.exe$")


@dataclass(frozen=True)
class KnownApplication:
    application_id: str
    name: str
    executables: tuple[str, ...]  # lower-case image names (no paths)
    impact: str

    def public(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "name": self.name,
            "executables": list(self.executables),
            "impact": self.impact,
        }


#: curated default registry (administrators can add entries through the remediation policy)
KNOWN_APPLICATIONS: dict[str, KnownApplication] = {
    a.application_id: a
    for a in (
        KnownApplication(
            "microsoft.teams",
            "Microsoft Teams",
            ("ms-teams.exe", "teams.exe"),
            "Calls and meetings in progress are dropped; chats are kept.",
        ),
        KnownApplication(
            "microsoft.onedrive",
            "Microsoft OneDrive",
            ("onedrive.exe",),
            "File synchronisation pauses for a few seconds and resumes.",
        ),
        KnownApplication("slack.desktop", "Slack", ("slack.exe",), "Slack is unavailable for a few seconds."),
        KnownApplication("zoom.client", "Zoom", ("zoom.exe",), "A meeting in progress is left."),
    )
}


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RestartApplicationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application_id: str = Field(min_length=3, max_length=64)

    @field_validator("application_id")
    @classmethod
    def _shape(cls, v: str) -> str:
        if not APP_ID.match(v):
            raise ValueError("application_id must be a registry id such as 'microsoft.teams'")
        return v


@dataclass(frozen=True)
class Verification:
    """How success is decided from telemetry after the agent reports completion."""

    checks: tuple[str, ...]  # codes evaluated by verification.py
    timeout_s: int
    target_signal: str | None = None  # condition that should improve (e.g. "cpu")
    target_below: float | None = None  # ... to below this value (%), within timeout_s
    description: str = ""


@dataclass(frozen=True)
class ActionDefinition:
    action_id: str
    version: int
    name: str
    description: str
    risk: Risk
    changes_state: bool  # False: read-only / connection-level, nothing on the system changes
    required_permission: str
    supported_os: tuple[str, ...]
    min_agent_version: str
    params: type[BaseModel]
    preconditions: tuple[str, ...]  # codes checked by the server (and again by the agent)
    validation_timeout_s: int
    execution_timeout_s: int
    verification: Verification
    rollback: str  # ROLLBACK_NOT_AVAILABLE or a strategy id
    cooldown_s: int
    max_per_day: int
    impact: str
    estimated_duration_s: int
    lock_group: str  # actions sharing a lock group never run concurrently on one device
    auto_eligible: bool = False  # may be auto-approved when policy allows (LOW + no state change only)
    enabled: bool = True
    disabled_reason: str | None = None
    examples: dict[str, Any] = field(default_factory=dict)

    def validate_params(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        """Schema validation (allowlist and policy validation happen in policy/service)."""
        try:
            return self.params.model_validate(raw or {}).model_dump()
        except ValidationError as exc:
            msgs = "; ".join(
                f"{'.'.join(str(x) for x in e['loc']) or 'parameters'}: {e['msg']}" for e in exc.errors()
            )
            raise ValueError(f"invalid parameters for {self.action_id}: {msgs}") from exc

    def lock_key(self, params: dict[str, Any]) -> str:
        return self.lock_group.format(**params)

    def public(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "risk_level": self.risk.value,
            "changes_state": self.changes_state,
            "required_permissions": [self.required_permission],
            "supported_os_versions": list(self.supported_os),
            "supported_agent_versions": f">= {self.min_agent_version}",
            "parameter_schema": self.params.model_json_schema(),
            "required_parameters": list(self.params.model_json_schema().get("required", [])),
            "preconditions": list(self.preconditions),
            "timeouts_s": {
                "validation": self.validation_timeout_s,
                "execution": self.execution_timeout_s,
                "verification": self.verification.timeout_s,
            },
            "verification": {
                "checks": list(self.verification.checks),
                "description": self.verification.description,
            },
            "rollback": self.rollback,
            "reversible": self.rollback not in (ROLLBACK_NOT_AVAILABLE, ROLLBACK_NOT_NEEDED),
            "cooldown_s": self.cooldown_s,
            "max_per_day": self.max_per_day,
            "impact": self.impact,
            "estimated_duration_s": self.estimated_duration_s,
            "auto_eligible": self.auto_eligible,
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "dry_run_supported": True,
        }


COMMON_PRE = (
    "device_online",
    "agent_healthy",
    "agent_version",
    "os_supported",
    "approval_valid",
    "policy_allows",
    "not_running",
    "cooldown",
    "daily_budget",
    "circuit_closed",
    "kill_switch",
)

ACTIONS: tuple[ActionDefinition, ...] = (
    ActionDefinition(
        "REFRESH_TELEMETRY",
        1,
        "Refresh telemetry",
        "The agent collects every sensor immediately and sends a complete snapshot.",
        Risk.LOW,
        False,
        "remediation.request",
        ("windows",),
        "1.6.0",
        NoParams,
        COMMON_PRE,
        30,
        60,
        Verification(
            ("agent_completed", "fresh_telemetry"),
            120,
            description="New telemetry arrives after the action completed.",
        ),
        ROLLBACK_NOT_NEEDED,
        300,
        24,
        "None: the agent only collects data sooner than scheduled.",
        10,
        "agent_connection",
        auto_eligible=True,
    ),
    ActionDefinition(
        "REQUEST_SYSTEM_RESCAN",
        1,
        "Rescan hardware and software inventory",
        "The agent re-runs inventory discovery (hardware, OS, security posture) and sends it.",
        Risk.LOW,
        False,
        "remediation.request",
        ("windows",),
        "1.6.0",
        NoParams,
        COMMON_PRE,
        30,
        120,
        Verification(
            ("agent_completed", "fresh_telemetry"),
            180,
            description="A fresh inventory and telemetry arrive after the rescan.",
        ),
        ROLLBACK_NOT_NEEDED,
        900,
        12,
        "A short burst of agent CPU while discovery runs.",
        30,
        "agent_connection",
        auto_eligible=True,
    ),
    ActionDefinition(
        "RECONNECT_AGENT",
        1,
        "Reconnect the agent",
        "The agent closes its connections to the platform, reconnects and resends a full snapshot.",
        Risk.LOW,
        False,
        "remediation.request",
        ("windows",),
        "1.6.0",
        NoParams,
        COMMON_PRE,
        30,
        60,
        Verification(
            ("agent_completed", "fresh_telemetry"),
            120,
            description="The agent is connected and telemetry is fresh again.",
        ),
        ROLLBACK_NOT_NEEDED,
        600,
        12,
        "Telemetry pauses for a few seconds.",
        15,
        "agent_connection",
        auto_eligible=True,
    ),
    ActionDefinition(
        "RESTART_KNOWN_APPLICATION",
        1,
        "Restart an approved application",
        "The agent asks the application to close (like clicking the close button), waits for it to exit and "
        "starts it again from the same program file. It never force-kills: if the application does not close, "  # noqa: E501
        "nothing else is done.",
        Risk.MEDIUM,
        True,
        "remediation.request",
        ("windows",),
        "1.6.0",
        RestartApplicationParams,
        (*COMMON_PRE, "application_known", "application_running", "interactive_session"),
        30,
        120,
        Verification(
            ("agent_completed", "fresh_telemetry", "application_running", "target_improved"),
            300,
            "cpu",
            70.0,
            "The application is running again and CPU drops below 70 % within 5 minutes.",
        ),
        ROLLBACK_NOT_AVAILABLE,
        1800,
        3,
        "The application closes and restarts; unsaved work in it may be lost.",
        45,
        "app:{application_id}",
        examples={"application_id": "microsoft.teams"},
    ),
    ActionDefinition(
        "RESTART_AGENT",
        1,
        "Restart the agent",
        "The agent process restarts.",
        Risk.MEDIUM,
        True,
        "remediation.request",
        ("windows",),
        "1.6.0",
        NoParams,
        COMMON_PRE,
        30,
        120,
        Verification(("fresh_telemetry",), 180),
        ROLLBACK_NOT_AVAILABLE,
        3600,
        2,
        "Telemetry pauses while the agent restarts.",
        60,
        "agent_connection",
        enabled=False,
        disabled_reason="Not implemented: a reliable restart needs a supervisor (the Windows service recovery "  # noqa: E501
        "policy); RECONNECT_AGENT covers connectivity problems without restarting.",
    ),
    ActionDefinition(
        "RESTART_KNOWN_WINDOWS_SERVICE",
        1,
        "Restart an approved Windows service",
        "Restart a Windows service from an approved list.",
        Risk.HIGH,
        True,
        "remediation.request",
        ("windows",),
        "1.6.0",
        NoParams,
        COMMON_PRE,
        30,
        120,
        Verification(("agent_completed",), 300),
        ROLLBACK_NOT_AVAILABLE,
        3600,
        2,
        "Dependent features stop briefly.",
        60,
        "service",
        enabled=False,
        disabled_reason="Not implemented: needs administrator rights; the agent runs least-privileged in the user "  # noqa: E501
        "session and is not granted service-control rights for remediation.",
    ),
    ActionDefinition(
        "CLEAR_APPLICATION_CACHE",
        1,
        "Clear a known application cache",
        "Delete an approved application's cache folder.",
        Risk.MEDIUM,
        True,
        "remediation.request",
        ("windows",),
        "1.6.0",
        NoParams,
        COMMON_PRE,
        30,
        120,
        Verification(("agent_completed",), 300),
        ROLLBACK_NOT_AVAILABLE,
        3600,
        2,
        "Cached data is rebuilt; first start is slower.",
        60,
        "files",
        enabled=False,
        disabled_reason="Not implemented: file deletion is irreversible and cache locations differ per version; "  # noqa: E501
        "deferred until verified per application.",
    ),
    ActionDefinition(
        "CLEAN_KNOWN_TEMPORARY_DATA",
        1,
        "Clean known temporary data",
        "Delete known temporary files.",
        Risk.MEDIUM,
        True,
        "remediation.request",
        ("windows",),
        "1.6.0",
        NoParams,
        COMMON_PRE,
        30,
        300,
        Verification(("agent_completed",), 300),
        ROLLBACK_NOT_AVAILABLE,
        86400,
        1,
        "Temporary files are removed permanently.",
        120,
        "files",
        enabled=False,
        disabled_reason="Not implemented: deletion is irreversible; Windows Storage Sense already provides this "  # noqa: E501
        "under the user's control.",
    ),
)
CATALOG: dict[str, ActionDefinition] = {a.action_id: a for a in ACTIONS}


def version_tuple(v: str | None) -> tuple[int, ...]:
    out = []
    for part in (v or "0").split(".")[:3]:
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits or 0))
    return tuple(out)


def get(action_id: str) -> ActionDefinition:
    a = CATALOG.get(action_id)
    if a is None:
        raise KeyError(f"{action_id} is not in the action catalog")
    return a
