"""Phase 9 - domain units: permissions / role grants, policy validation / inheritance / locks / versions,
compliance, lifecycle, enrollment tokens, quotas, audit chain + scrubbing, TOTP."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.governance import compliance as comp
from app.domain.governance import policies as pol
from app.domain.governance.audit import AuditEvent, scrub
from app.domain.governance.policies import Policy, PolicyStatus
from app.domain.tenancy.models import (
    LIFECYCLE_TRANSITIONS,
    EnrollmentToken,
    Lifecycle,
    Organization,
    OrgStatus,
    QuotaMode,
)
from app.domain.tenancy.permissions import LEGACY_RANK, ROLES, can_grant, permissions_for
from app.repositories.governance import MemoryGovernanceRepository
from app.services.identity import totp, totp_check
from app.services.tenancy import RateWindow

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ permissions
def test_roles_are_least_privilege_bundles() -> None:
    assert "remediation.approve" not in ROLES["security_admin"]  # security admin cannot execute remediation
    assert "security.manage" in ROLES["security_admin"]
    assert "remediation.approve" in ROLES["it_operator"] and "user.manage" not in ROLES["it_operator"]
    assert ROLES["auditor"] >= {"audit.view", "audit.export"} and "device.manage" not in ROLES["auditor"]
    assert "platform.manage" not in ROLES["org_owner"]
    assert "platform.manage" in permissions_for("read_only", platform_admin=True)
    assert set(LEGACY_RANK) == set(ROLES)
    assert ROLES["employee"] <= ROLES["read_only"] | {"diagnosis.request", "remediation.request"}


@pytest.mark.parametrize(
    ("granter", "role", "ok"),
    [
        ("org_owner", "org_owner", True),
        ("org_admin", "org_owner", False),
        ("org_admin", "org_admin", True),
        ("it_admin", "org_admin", False),
        ("it_operator", "employee", True),
        ("read_only", "employee", True),
        ("employee", "read_only", False),
        (None, "employee", False),
        ("org_admin", "bogus", False),
    ],
)
def test_no_one_grants_above_their_level(granter: str | None, role: str, ok: bool) -> None:
    assert can_grant(granter, False, role) is ok
    assert can_grant(None, True, "org_owner")  # platform admin


# ------------------------------------------------------------------ policies
def _p(
    scope: str,
    sid: str,
    body: dict,
    locked: list[str] | None = None,
    version: int = 1,
    kind: str = "remediation",
) -> Policy:
    return Policy(
        f"{scope}-{sid}", "acme", scope, sid, kind, version, PolicyStatus.PUBLISHED, body, locked or []
    )


def test_policy_validation_rejects_bad_values() -> None:
    errs, _ = pol.validate(
        _p("organization", "acme", {"approval_ttl_s": 5, "low_mode": "YOLO", "nope": 1}), []
    )
    assert len(errs) == 3
    errs, _ = pol.validate(_p("device_group", "g", {"mfa": "MFA_REQUIRED"}, kind="security"), [])
    assert any("whole organization" in e for e in errs)
    errs, _ = pol.validate(
        _p(
            "organization", "acme", {"minimum_version": "2.0.0", "recommended_version": "1.6.0"}, kind="agent"
        ),
        [],
    )
    assert any("minimum_version" in e for e in errs)
    errs, _ = pol.validate(
        _p(
            "organization",
            "acme",
            {"blocked_versions": ["1.6.0"], "recommended_version": "1.6.0"},
            kind="agent",
        ),
        [],
    )
    assert any("cannot be blocked" in e for e in errs)
    errs, _ = pol.validate(_p("organization", "acme", {"minimum_version": "1.x"}, kind="agent"), [])
    assert errs
    _, warns = pol.validate(_p("organization", "acme", {"critical_mode": "MANUAL_APPROVAL"}), [])
    assert warns


def test_precedence_and_locks() -> None:
    org = _p(
        "organization",
        "acme",
        {"auto_remediation_enabled": False, "approval_ttl_s": 900},
        locked=["approval_ttl_s"],
    )
    grp_low = _p("device_group", "g1", {"auto_remediation_enabled": True})
    dev = _p(
        "device", "d1", {"four_eyes_min_risk": "MEDIUM", "approval_ttl_s": 60}
    )  # tries to override a lock
    values, source = pol.effective("remediation", [org, grp_low, dev])
    assert values["auto_remediation_enabled"] is True and source["auto_remediation_enabled"].startswith(
        "device_group"
    )
    assert values["four_eyes_min_risk"] == "MEDIUM"
    assert values["approval_ttl_s"] == 900  # locked at organization level: the device override is ignored
    errs, _ = pol.validate(dev, [org])
    assert any("locked" in e for e in errs)  # ... and refused at publication
    assert pol.effective("remediation", [])[0] == pol.platform_default("remediation")


def test_policy_effective_dates() -> None:
    p = _p("organization", "acme", {"auto_remediation_enabled": True})
    p.effective_from = NOW + timedelta(hours=1)
    assert not p.active(NOW) and p.active(NOW + timedelta(hours=2))
    p.effective_until = NOW + timedelta(hours=3)
    assert not p.active(NOW + timedelta(hours=4))


# ------------------------------------------------------------------ compliance
def _facts(**kw: object) -> comp.DeviceFacts:
    base = dict(
        device_id="d",
        lifecycle="ACTIVE",
        enrolled=True,
        credential_valid=True,
        agent_version="1.6.0",
        os_name="Microsoft Windows 11 Pro",
        telemetry_age_s=10.0,
        controls={
            "antivirus": True,
            "realtime_protection": True,
            "firewall": True,
            "secure_boot": False,
            "tpm": None,
        },
    )
    base.update(kw)
    return comp.DeviceFacts(**base)  # type: ignore[arg-type]


def test_compliance_never_fakes_unknowns() -> None:
    agent, cp = pol.platform_default("agent"), pol.platform_default("compliance")
    r = comp.evaluate(_facts(), agent, cp)
    assert r["status"] == "COMPLIANT"  # secure boot off is not a required control by default
    states = {c["check"]: c["state"] for c in r["checks"]}
    assert states["disk_encryption"] == "NOT_SUPPORTED" and states["control_tpm"] == "UNKNOWN"
    assert comp.evaluate(_facts(agent_version="1.3.0"), agent, cp)["status"] == "NON_COMPLIANT"
    reasons = comp.evaluate(_facts(agent_version="1.3.0"), agent, cp)["reasons"]
    assert any("required agent version 1.4.0, installed 1.3.0" in x for x in reasons)
    assert comp.evaluate(_facts(controls={}), agent, cp)["status"] == "PARTIALLY_COMPLIANT"
    assert comp.evaluate(_facts(credential_valid=None), agent, cp)["status"] == "PARTIALLY_COMPLIANT"
    assert comp.evaluate(_facts(), agent, {**cp, "exempt": True})["status"] == "EXEMPT"
    strict = {**cp, "required_controls": ["secure_boot"]}
    assert comp.evaluate(_facts(), agent, strict)["status"] == "NON_COMPLIANT"
    assert (
        comp.evaluate(_facts(agent_version="1.5.0"), {**agent, "blocked_versions": ["1.5.0"]}, cp)["status"]
        == "NON_COMPLIANT"
    )


def test_controls_from_twin_state() -> None:
    state = {
        "security.firewall_enabled": {"value": True},
        "security.secure_boot": {"value": "off"},
        "security.antivirus_enabled": {"value": None},
    }
    c = comp.controls_from_state(state)
    assert c["firewall"] is True and c["secure_boot"] is False and c["antivirus"] is None and c["tpm"] is None


# ------------------------------------------------------------------ lifecycle / tokens / quotas
def test_lifecycle_rules() -> None:
    assert Lifecycle.ACTIVE not in LIFECYCLE_TRANSITIONS[Lifecycle.RETIRED]
    assert LIFECYCLE_TRANSITIONS[Lifecycle.DECOMMISSIONED] == frozenset()
    assert Lifecycle.DECOMMISSIONED in LIFECYCLE_TRANSITIONS[Lifecycle.RETIRED]


def test_enrollment_token_usability() -> None:
    t = EnrollmentToken("t", "acme", "h", "alice", NOW, NOW + timedelta(hours=1))
    assert t.usable(NOW) is None
    assert t.usable(NOW + timedelta(hours=2)) == "enrollment token expired"
    t.uses = 1
    assert t.usable(NOW) == "enrollment token already used" and t.status == "USED"
    t.revoked_at = NOW
    assert t.usable(NOW) == "enrollment token revoked"


def test_quota_windows() -> None:
    w = RateWindow(60)
    assert all(w.hit("k", 3, 100.0)[0] for _ in range(3))
    ok, retry = w.hit("k", 3, 110.0)
    assert not ok and 49 <= retry <= 51
    assert w.hit("k", 3, 161.0)[0]  # next window
    org = Organization(
        "acme", "Acme", OrgStatus.ACTIVE, NOW, quotas={"max_devices": {"limit": 5, "mode": "REJECT"}}
    )
    assert org.quota("max_devices") == (5, QuotaMode.REJECT) and org.quota("max_users")[0] == 500


# ------------------------------------------------------------------ audit
def test_audit_chain_and_scrub() -> None:
    repo = MemoryGovernanceRepository()
    events = [
        AuditEvent(str(i), NOW, "acme", "alice", "user", "policy.published", "policy") for i in range(3)
    ]
    asyncio.run(repo.append_audit(events))
    assert asyncio.run(repo.verify_audit())["ok"]
    repo.audit[1].actor_id = "mallory"
    bad = asyncio.run(repo.verify_audit())
    assert not bad["ok"] and bad["first_bad"] == 2
    s = scrub(
        {
            "password": "hunter2",
            "client_secret": "x",
            "nested": {"api_token": "y", "ok": 1},
            "n": 3,
            "long": "z" * 999,
        }
    )
    assert (
        s["password"] == "[redacted]"
        and s["client_secret"] == "[redacted]"
        and s["nested"]["api_token"] == "[redacted]"
    )
    assert s["nested"]["ok"] == 1 and len(s["long"]) == 500


# ------------------------------------------------------------------ TOTP (RFC 6238 test vector)
def test_totp_rfc6238_vector_and_replay() -> None:
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp(secret, 59 // 30, digits=8) == "94287082"  # RFC 6238 appendix B (SHA-1, T=59)
    step = 1_700_000_000 // 30
    code = totp(secret, step)
    assert totp_check(secret, code, 0, now=1_700_000_000) == step
    assert totp_check(secret, code, step, now=1_700_000_000) is None  # replay of the same code
    assert totp_check(secret, "000000", 0, now=1_700_000_000) in (None, step - 1, step, step + 1)
    assert totp_check(secret, "12ab56", 0) is None
