"""Organisation, structure, membership, device registry / lifecycle, enrollment tokens, quotas."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

DEFAULT_ORG = "default"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
NAME_MAX = 120


class OrgStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"  # sign-in and ingestion refused, data kept
    ARCHIVED = "ARCHIVED"


class UnitKind(StrEnum):
    BUSINESS_UNIT = "business_unit"
    DEPARTMENT = "department"
    TEAM = "team"


UNIT_PARENT = {
    UnitKind.BUSINESS_UNIT: (None,),
    UnitKind.DEPARTMENT: (None, UnitKind.BUSINESS_UNIT),
    UnitKind.TEAM: (UnitKind.DEPARTMENT, UnitKind.BUSINESS_UNIT, None),
}


class Lifecycle(StrEnum):
    PENDING_ENROLLMENT = "PENDING_ENROLLMENT"
    ACTIVE = "ACTIVE"
    STALE = "STALE"  # derived from presence for display; stored state stays ACTIVE
    DISABLED = "DISABLED"  # ingestion refused, credential kept (re-enable possible)
    QUARANTINED = "QUARANTINED"  # telemetry accepted, every remediation / action refused
    REVOKED = "REVOKED"  # credential revoked; must re-enroll with a new token
    RETIRED = "RETIRED"  # out of service; history kept, ingestion refused
    DECOMMISSIONED = "DECOMMISSIONED"  # retired and device data deleted under a deletion workflow


LIFECYCLE_TRANSITIONS: dict[Lifecycle, frozenset[Lifecycle]] = {
    Lifecycle.PENDING_ENROLLMENT: frozenset({Lifecycle.ACTIVE, Lifecycle.REVOKED, Lifecycle.RETIRED}),
    Lifecycle.ACTIVE: frozenset(
        {Lifecycle.DISABLED, Lifecycle.QUARANTINED, Lifecycle.REVOKED, Lifecycle.RETIRED}
    ),
    Lifecycle.DISABLED: frozenset(
        {Lifecycle.ACTIVE, Lifecycle.QUARANTINED, Lifecycle.REVOKED, Lifecycle.RETIRED}
    ),
    Lifecycle.QUARANTINED: frozenset(
        {Lifecycle.ACTIVE, Lifecycle.DISABLED, Lifecycle.REVOKED, Lifecycle.RETIRED}
    ),
    Lifecycle.REVOKED: frozenset({Lifecycle.ACTIVE, Lifecycle.RETIRED}),  # ACTIVE only through re-enrollment
    Lifecycle.RETIRED: frozenset({Lifecycle.DECOMMISSIONED}),
    Lifecycle.DECOMMISSIONED: frozenset(),
}
INGEST_REFUSED = frozenset(
    {
        Lifecycle.DISABLED,
        Lifecycle.REVOKED,
        Lifecycle.RETIRED,
        Lifecycle.DECOMMISSIONED,
        Lifecycle.PENDING_ENROLLMENT,
    }
)
ACTIONS_REFUSED = INGEST_REFUSED | {Lifecycle.QUARANTINED}


class QuotaMode(StrEnum):
    ALLOW = "ALLOW"  # not enforced (counted only)
    THROTTLE = (
        "THROTTLE"  # rate quotas: 429 + Retry-After; nothing is discarded (agents retry from their queue)
    )
    REJECT = "REJECT"  # count quotas: the request is refused with a reason


#: quota -> (default limit, default mode, kind)
QUOTA_DEFAULTS: dict[str, tuple[int, QuotaMode, str]] = {
    "max_devices": (1000, QuotaMode.REJECT, "count"),
    "max_users": (500, QuotaMode.REJECT, "count"),
    "telemetry_batches_per_min": (12_000, QuotaMode.THROTTLE, "rate"),
    "api_requests_per_min": (6000, QuotaMode.THROTTLE, "rate"),
    "user_api_requests_per_min": (1200, QuotaMode.THROTTLE, "rate"),
    "websocket_connections": (500, QuotaMode.REJECT, "count"),
    "diagnosis_jobs_per_hour": (600, QuotaMode.THROTTLE, "rate"),
    "remediation_requests_per_day": (2000, QuotaMode.REJECT, "rate"),
    "exports_per_hour": (30, QuotaMode.THROTTLE, "rate"),
}


@dataclass
class Organization:
    org_id: str
    name: str
    status: OrgStatus
    created_at: datetime
    quotas: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> {limit, mode}
    settings: dict[str, Any] = field(default_factory=dict)

    def quota(self, name: str) -> tuple[int, QuotaMode]:
        limit, mode, _ = QUOTA_DEFAULTS[name]
        o = self.quotas.get(name) or {}
        return int(o.get("limit", limit)), QuotaMode(o.get("mode", mode))

    def public(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "name": self.name,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "quotas": {
                k: {"limit": self.quota(k)[0], "mode": self.quota(k)[1].value, "kind": v[2]}
                for k, v in QUOTA_DEFAULTS.items()
            },
        }


@dataclass
class OrgUnit:
    unit_id: str
    org_id: str
    kind: UnitKind
    name: str
    parent_id: str | None
    status: str = "ACTIVE"  # ACTIVE | ARCHIVED

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass
class DeviceGroup:
    group_id: str
    org_id: str
    name: str
    kind: str  # department | team | location | business_unit | os | environment | role | custom
    unit_id: str | None = None
    priority: int = 100  # policy precedence among a device's groups: lower number wins
    tags: list[str] = field(default_factory=list)
    status: str = "ACTIVE"  # ACTIVE | ARCHIVED

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Membership:
    org_id: str
    username: str
    role: str
    status: str = "ACTIVE"  # ACTIVE | DISABLED (SCIM / admin)
    group_scope: list[str] = field(default_factory=list)  # empty = whole organisation
    source: str = "local"  # local | oidc | saml | scim
    created_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat() if self.created_at else None
        return d


@dataclass
class DeviceRecord:
    """Authoritative ownership and lifecycle of a device (separate from the twin's live state)."""

    device_id: str
    org_id: str
    lifecycle: Lifecycle
    enrolled_at: datetime | None
    enrollment_id: str | None = None  # token id, or "legacy" for the shared enrollment key
    groups: list[str] = field(default_factory=list)
    updated_at: datetime | None = None
    updated_by: str | None = None
    reason: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "org_id": self.org_id,
            "lifecycle": self.lifecycle.value,
            "enrolled_at": self.enrolled_at.isoformat() if self.enrolled_at else None,
            "enrollment_id": self.enrollment_id,
            "groups": list(self.groups),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "updated_by": self.updated_by,
            "reason": self.reason,
        }


@dataclass
class EnrollmentToken:
    token_id: str
    org_id: str
    token_hash: str
    created_by: str
    created_at: datetime
    expires_at: datetime
    max_uses: int = 1
    uses: int = 0
    group_id: str | None = None
    revoked_at: datetime | None = None
    label: str = ""

    @property
    def status(self) -> str:
        if self.revoked_at:
            return "REVOKED"
        if self.uses >= self.max_uses:
            return "USED"
        if datetime.now(UTC) >= self.expires_at:
            return "EXPIRED"
        return "ACTIVE"

    def usable(self, now: datetime) -> str | None:
        """None when usable, else the reason (never says whether another organisation's token exists)."""
        if self.revoked_at:
            return "enrollment token revoked"
        if now >= self.expires_at:
            return "enrollment token expired"
        if self.uses >= self.max_uses:
            return "enrollment token already used"
        return None

    def public(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "org_id": self.org_id,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "max_uses": self.max_uses,
            "uses": self.uses,
            "group_id": self.group_id,
            "label": self.label,
            "status": self.status,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }


ENROLLMENT_PREFIX = "ldt_enr_"


def new_enrollment_secret() -> tuple[str, str]:
    """-> (secret shown once, sha256 hash stored)."""
    secret = ENROLLMENT_PREFIX + secrets.token_urlsafe(32)
    return secret, hash_secret(secret)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()
