"""Permissions, organisation roles and their mapping to the legacy rank model.

Authorization checks name permissions (``device.manage``), never roles. Roles are bundles of permissions;
scope (device groups, assigned devices) narrows *which* resources a permission applies to.

Legacy rank (employee < viewer < operator < admin) is derived from the organisation role so that the
Phase 1-8 rank checks keep their meaning; new and sensitive routes check permissions.
"""

from __future__ import annotations

PERMISSIONS: frozenset[str] = frozenset(
    {
        "platform.manage",
        "organization.view",
        "organization.manage",
        "identity.manage",
        "security.manage",
        "user.view",
        "user.manage",
        "user.disable",
        "device.view",
        "device.manage",
        "device.enroll",
        "device.remove",
        "group.view",
        "group.manage",
        "telemetry.view",
        "telemetry.export",
        "twin.view",
        "anomaly.view",
        "anomaly.manage",
        "prediction.view",
        "alert.view",
        "alert.manage",
        "notification.view",
        "diagnosis.view",
        "diagnosis.request",
        "remediation.view",
        "remediation.request",
        "remediation.approve",
        "remediation.execute",
        "remediation.cancel",
        "remediation.rollback",
        "remediation.manage_policy",
        "remediation.manage_actions",
        "policy.view",
        "policy.manage",
        "audit.view",
        "audit.export",
        "retention.manage",
        "usage.view",
    }
)

_VIEW = {
    "device.view",
    "group.view",
    "twin.view",
    "telemetry.view",
    "anomaly.view",
    "prediction.view",
    "alert.view",
    "diagnosis.view",
    "remediation.view",
    "notification.view",
}
_ORG_ALL = PERMISSIONS - {"platform.manage"}

ROLES: dict[str, frozenset[str]] = {
    "org_owner": frozenset(_ORG_ALL),
    "org_admin": frozenset(_ORG_ALL - {"identity.manage"}),
    "security_admin": frozenset(
        _VIEW
        | {
            "organization.view",
            "identity.manage",
            "security.manage",
            "user.view",
            "user.disable",
            "alert.manage",
            "policy.view",
            "policy.manage",
            "audit.view",
            "audit.export",
            "usage.view",
        }
    ),
    "it_admin": frozenset(
        _VIEW
        | {
            "organization.view",
            "device.manage",
            "device.enroll",
            "device.remove",
            "group.manage",
            "telemetry.export",
            "anomaly.manage",
            "alert.manage",
            "diagnosis.request",
            "remediation.request",
            "remediation.approve",
            "remediation.execute",
            "remediation.cancel",
            "remediation.manage_policy",
            "policy.view",
            "policy.manage",
            "user.view",
            "audit.view",
            "usage.view",
        }
    ),
    "it_operator": frozenset(
        _VIEW
        | {
            "anomaly.manage",
            "alert.manage",
            "diagnosis.request",
            "remediation.request",
            "remediation.approve",
            "remediation.execute",
            "remediation.cancel",
            "policy.view",
        }
    ),
    "manager": frozenset(_VIEW | {"usage.view"}),
    "analyst": frozenset(_VIEW | {"telemetry.export", "usage.view"}),
    "auditor": frozenset(
        {
            "organization.view",
            "device.view",
            "group.view",
            "remediation.view",
            "policy.view",
            "user.view",
            "audit.view",
            "audit.export",
            "usage.view",
        }
    ),
    "read_only": frozenset(_VIEW | {"policy.view"}),
    "employee": frozenset(
        {
            "device.view",
            "twin.view",
            "telemetry.view",
            "anomaly.view",
            "prediction.view",
            "alert.view",
            "diagnosis.view",
            "diagnosis.request",
            "remediation.view",
            "remediation.request",
            "notification.view",
        }
    ),
}
ROLE_LABELS = {
    "org_owner": "Organization Owner",
    "org_admin": "Organization Admin",
    "security_admin": "Security Admin",
    "it_admin": "IT Admin",
    "it_operator": "IT Operator",
    "manager": "Manager",
    "analyst": "Analyst",
    "auditor": "Auditor",
    "read_only": "Read Only",
    "employee": "Employee / Device User",
}
#: legacy rank role used by the Phase 1-8 checks (Admin / Operator / Staff dependencies)
LEGACY_RANK = {
    "org_owner": "admin",
    "org_admin": "admin",
    "it_admin": "admin",
    "security_admin": "operator",
    "it_operator": "operator",
    "manager": "viewer",
    "analyst": "viewer",
    "auditor": "viewer",
    "read_only": "viewer",
    "employee": "employee",
}
FROM_LEGACY = {"admin": "org_admin", "operator": "it_operator", "viewer": "read_only", "employee": "employee"}
#: roles an identity provider / SCIM / JIT provisioning may grant without an administrator
PROVISIONABLE_ROLES = frozenset({"read_only", "employee", "analyst", "manager", "auditor", "it_operator"})
#: roles that may grant a given role (no one grants above their own level; owners only by owners)
ROLE_LEVEL = {
    "employee": 0,
    "read_only": 1,
    "auditor": 1,
    "analyst": 1,
    "manager": 1,
    "it_operator": 2,
    "security_admin": 3,
    "it_admin": 3,
    "org_admin": 4,
    "org_owner": 5,
}
#: highest remediation risk each organisation role may approve (with remediation.approve)
APPROVE_CEILING = {
    "it_operator": "MEDIUM",
    "it_admin": "HIGH",
    "org_admin": "CRITICAL",
    "org_owner": "CRITICAL",
}


def permissions_for(org_role: str | None, platform_admin: bool = False) -> frozenset[str]:
    perms = ROLES.get(org_role or "", frozenset())
    return perms | {"platform.manage"} | _ORG_ALL if platform_admin else perms


def can_grant(granter_role: str | None, granter_platform_admin: bool, role: str) -> bool:
    if granter_platform_admin:
        return role in ROLES
    if granter_role is None or role not in ROLES:
        return False
    if role == "org_owner":
        return granter_role == "org_owner"
    return ROLE_LEVEL[role] <= ROLE_LEVEL.get(granter_role, -1)
