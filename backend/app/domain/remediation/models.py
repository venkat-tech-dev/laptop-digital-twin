"""Remediation record, lifecycle and audit entries.

A remediation is one proposed action for one device. Its lifecycle is a strict state machine; terminal
records are immutable (corrections are audit entries, never edits). The execution record (envelope
digest, agent report, verification) is written once.

    PROPOSED -> PENDING_APPROVAL -> APPROVED -> QUEUED -> VALIDATING -> EXECUTING -> VERIFYING
             -> SUCCEEDED | PARTIALLY_SUCCEEDED | FAILED            (ROLLED_BACK / ROLLBACK_FAILED reserved)
    PENDING_APPROVAL -> REJECTED | EXPIRED | CANCELLED
    APPROVED / QUEUED -> CANCELLED | EXPIRED (approval lapsed, diagnosis changed, offline too long)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    PARTIALLY_SUCCEEDED = "PARTIALLY_SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


TERMINAL = frozenset(
    {
        Status.REJECTED,
        Status.SUCCEEDED,
        Status.PARTIALLY_SUCCEEDED,
        Status.FAILED,
        Status.CANCELLED,
        Status.EXPIRED,
        Status.ROLLED_BACK,
        Status.ROLLBACK_FAILED,
    }
)
OPEN = frozenset(Status) - TERMINAL
IN_FLIGHT = frozenset({Status.VALIDATING, Status.EXECUTING, Status.VERIFYING})

TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.PROPOSED: frozenset(
        {Status.PENDING_APPROVAL, Status.APPROVED, Status.CANCELLED, Status.EXPIRED, Status.REJECTED}
    ),
    Status.PENDING_APPROVAL: frozenset({Status.APPROVED, Status.REJECTED, Status.CANCELLED, Status.EXPIRED}),
    Status.APPROVED: frozenset({Status.QUEUED, Status.CANCELLED, Status.EXPIRED}),
    Status.QUEUED: frozenset({Status.VALIDATING, Status.CANCELLED, Status.EXPIRED, Status.FAILED}),
    # VALIDATING -> QUEUED: the envelope was not picked up in time (re-issued with the same execution id)
    Status.VALIDATING: frozenset(
        {Status.EXECUTING, Status.FAILED, Status.QUEUED, Status.CANCELLED, Status.VERIFYING}
    ),
    Status.EXECUTING: frozenset({Status.VERIFYING, Status.FAILED}),
    Status.VERIFYING: frozenset({Status.SUCCEEDED, Status.PARTIALLY_SUCCEEDED, Status.FAILED}),
}


class InvalidTransitionError(ValueError):
    pass


class Mode(StrEnum):
    IMMEDIATE = "IMMEDIATE"
    SCHEDULED = "SCHEDULED"
    MAINTENANCE_WINDOW = "MAINTENANCE_WINDOW"


@dataclass(slots=True)
class AuditEntry:
    at: datetime
    actor: str  # user subject | "system" | "agent:<device>"
    action: str  # proposed | approval_required | approved | rejected | queued | dispatched | started ...
    from_status: str | None
    to_status: str | None
    detail: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["at"] = self.at.isoformat()
        return d


def chain_hash(prev: str, remediation_id: str, entry: AuditEntry) -> str:
    """Tamper evidence: every audit row hashes its content together with the previous row's hash."""
    body = json.dumps({"r": remediation_id, **entry.public()}, sort_keys=True, default=str)
    return hashlib.sha256((prev + "|" + body).encode()).hexdigest()


@dataclass(slots=True)
class Remediation:
    remediation_id: str
    tenant_id: str
    device_id: str
    action_type: str
    action_version: int
    action_name: str
    description: str
    risk_level: str
    requires_approval: bool
    approval_policy: str  # MANUAL_APPROVAL | AUTO_APPROVE_LOW_RISK | ...
    parameters: dict[str, Any]
    preconditions: list[str]
    verification_rules: dict[str, Any]
    rollback_strategy: str
    status: Status
    requested_by: str
    created_at: datetime
    updated_at: datetime
    correlation_id: str  # links proposal, envelope, agent report and audit (also the diagnosis series)
    execution_id: str  # unique per execution, stable across envelope re-issues (agent idempotency)
    alert_id: str | None = None
    diagnosis_id: str | None = None
    prediction_id: str | None = None
    reason: str = ""  # what is wrong (from the diagnosis)
    evidence: list[dict[str, Any]] = field(default_factory=list)  # evidence ids + statements
    diagnosis_confidence: float | None = None
    action_confidence: float | None = None
    expected_success_probability: float | None = None
    recommendation_source: str = "rules"  # rules | ai (validated against the catalog) | user
    mode: Mode = Mode.IMMEDIATE
    scheduled_at: datetime | None = None
    approval_expires_at: datetime | None = None
    approved_by: str | None = None
    approval_at: datetime | None = None
    rejected_by: str | None = None
    policy_version: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    result: str | None = None  # SUCCEEDED | PARTIALLY_SUCCEEDED | FAILED
    failure_reason: str | None = None
    dry_run: bool = False
    execution: dict[str, Any] = field(default_factory=dict)  # envelope digest, dispatch times, agent report
    verification: dict[str, Any] = field(default_factory=dict)  # baseline, checks, outcome
    audit: list[AuditEntry] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN

    def transition(self, to: Status, at: datetime, actor: str, action: str, **detail: Any) -> None:
        if to == self.status:
            return
        if to not in TRANSITIONS.get(self.status, frozenset()):
            raise InvalidTransitionError(f"{self.status} -> {to} is not allowed")
        self.audit.append(AuditEntry(at, actor, action, self.status.value, to.value, detail))
        self.status = to
        self.updated_at = at
        if to in (Status.SUCCEEDED, Status.PARTIALLY_SUCCEEDED):
            self.completed_at, self.result = at, to.value
        elif to == Status.FAILED:
            self.failed_at, self.result = at, to.value
            self.failure_reason = str(detail.get("reason") or self.failure_reason or "failed")[:500]

    def note(self, at: datetime, actor: str, action: str, **detail: Any) -> None:
        self.audit.append(AuditEntry(at, actor, action, self.status.value, self.status.value, detail))
        self.updated_at = at

    def to_dict(self, full: bool = True) -> dict[str, Any]:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        out: dict[str, Any] = {
            "id": self.remediation_id,
            "remediation_id": self.remediation_id,
            "tenant_id": self.tenant_id,
            "device_id": self.device_id,
            "alert_id": self.alert_id,
            "diagnosis_id": self.diagnosis_id,
            "prediction_id": self.prediction_id,
            "action_type": self.action_type,
            "action_version": self.action_version,
            "action_name": self.action_name,
            "description": self.description,
            "risk_level": self.risk_level,
            "requires_approval": self.requires_approval,
            "approval_policy": self.approval_policy,
            "parameters": self.parameters,
            "status": self.status.value,
            "requested_by": self.requested_by,
            "approved_by": self.approved_by,
            "approval_at": iso(self.approval_at),
            "approval_expires_at": iso(self.approval_expires_at),
            "rejected_by": self.rejected_by,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "scheduled_at": iso(self.scheduled_at),
            "mode": self.mode.value,
            "started_at": iso(self.started_at),
            "completed_at": iso(self.completed_at),
            "failed_at": iso(self.failed_at),
            "result": self.result,
            "failure_reason": self.failure_reason,
            "correlation_id": self.correlation_id,
            "execution_id": self.execution_id,
            "reason": self.reason,
            "diagnosis_confidence": self.diagnosis_confidence,
            "action_confidence": self.action_confidence,
            "expected_success_probability": self.expected_success_probability,
            "recommendation_source": self.recommendation_source,
            "rollback_strategy": self.rollback_strategy,
            "policy_version": self.policy_version,
            "dry_run": self.dry_run,
        }
        if full:
            out.update(
                {
                    "preconditions": self.preconditions,
                    "verification_rules": self.verification_rules,
                    "evidence": self.evidence,
                    "execution": self.execution,
                    "verification": self.verification,
                    "audit": [a.public() for a in self.audit],
                }
            )
        return out
