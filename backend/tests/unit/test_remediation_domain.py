# ruff: noqa: E501  (inline test payloads)
"""Phase 8 - remediation domain: catalog, parameters, risk, approval policy, four-eyes, kill switches,
maintenance windows, envelopes, verification, recommendation (incl. untrusted AI), lifecycle, audit chain."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.domain.remediation import recommend, verification
from app.domain.remediation.catalog import CATALOG, KNOWN_APPLICATIONS, ROLLBACK_NOT_AVAILABLE, Risk
from app.domain.remediation.envelope import Signer, issue, verify
from app.domain.remediation.models import InvalidTransitionError, Remediation, Status
from app.domain.remediation.policy import (
    KillSwitches,
    MaintenanceWindow,
    RemediationPolicy,
    can_approve,
    can_request,
    has_permission,
)
from app.repositories.remediation import MemoryRemediationRepository

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ catalog
def test_catalog_is_complete_and_honest() -> None:
    for a in CATALOG.values():
        pub = a.public()
        assert pub["timeouts_s"]["execution"] > 0 and pub["timeouts_s"]["verification"] > 0
        assert a.rollback and a.cooldown_s > 0 and a.max_per_day > 0 and a.impact
        if not a.enabled:
            assert a.disabled_reason
        if a.auto_eligible:
            assert a.risk == Risk.LOW and not a.changes_state
        assert pub["reversible"] is False  # nothing implemented can be undone; the catalog says so
    assert CATALOG["RESTART_KNOWN_APPLICATION"].rollback == ROLLBACK_NOT_AVAILABLE
    enabled = {a.action_id for a in CATALOG.values() if a.enabled}
    assert enabled == {
        "REFRESH_TELEMETRY",
        "REQUEST_SYSTEM_RESCAN",
        "RECONNECT_AGENT",
        "RESTART_KNOWN_APPLICATION",
    }


@pytest.mark.parametrize(
    "params",
    [
        {"application_id": "C:\\Windows\\System32\\cmd.exe"},
        {"application_id": "x"},
        {"application_id": "ms teams"},
        {"application_id": "slack.desktop", "path": "C:\\a.exe"},
        {},
    ],
)
def test_parameters_are_strictly_typed(params: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        CATALOG["RESTART_KNOWN_APPLICATION"].validate_params(params)
    with pytest.raises(ValueError):
        CATALOG["REFRESH_TELEMETRY"].validate_params({"cmd": "whoami"})
    assert CATALOG["RESTART_KNOWN_APPLICATION"].validate_params({"application_id": "slack.desktop"})


# ------------------------------------------------------------------ policy
def test_default_policy_is_conservative() -> None:
    p = RemediationPolicy()
    low, med = CATALOG["REFRESH_TELEMETRY"], CATALOG["RESTART_KNOWN_APPLICATION"]
    assert p.approval(low, "d", None, 0.99)[:2] == ("MANUAL_APPROVAL", True)
    assert p.approval(med, "d", None, 0.99)[:2] == ("MANUAL_APPROVAL", True)
    assert p.auto_remediation_enabled is False
    assert p.approval(CATALOG["RESTART_AGENT"], "d", None, 1.0)[0] == "DISABLED"  # disabled action


def test_auto_approval_needs_every_condition() -> None:
    p = RemediationPolicy(auto_remediation_enabled=True)
    p.risk_modes["LOW"] = "AUTO_APPROVE_LOW_RISK"
    p.risk_modes["MEDIUM"] = "AUTO_APPROVE_LOW_RISK"
    low = CATALOG["REFRESH_TELEMETRY"]
    assert p.approval(low, "d", None, 0.9)[1] is False
    assert p.approval(low, "d", None, 0.5)[1] is True  # confidence too low
    assert p.approval(low, "d", None, None)[1] is True
    assert (
        p.approval(CATALOG["RESTART_KNOWN_APPLICATION"], "d", None, 0.99)[1] is True
    )  # changes state: never auto
    p.auto_remediation_enabled = False
    assert p.approval(low, "d", None, 0.99)[1] is True


def test_mode_precedence_device_over_group_over_action_over_risk() -> None:
    p = RemediationPolicy(action_modes={"REFRESH_TELEMETRY": "DISABLED"},
                          group_modes={"lab": {"*": "MANUAL_APPROVAL"}},
                          device_modes={"d1": {"REFRESH_TELEMETRY": "DISABLED"}})  # fmt: skip
    a = CATALOG["REFRESH_TELEMETRY"]
    assert p.mode_for(a, "d1", "lab") == "DISABLED"
    assert p.mode_for(a, "d2", "lab") == "MANUAL_APPROVAL"
    assert p.mode_for(a, "d2", None) == "DISABLED"


def test_permissions_risk_ceilings_and_four_eyes() -> None:
    p = RemediationPolicy(four_eyes_min_risk="MEDIUM")
    med = CATALOG["RESTART_KNOWN_APPLICATION"]
    assert can_approve("viewer", "v", med, "system", p) is not None
    assert can_approve("employee", "e", med, "system", p) is not None
    assert can_approve("operator", "ops", med, "system", p) is None
    assert "four-eyes" in str(can_approve("operator", "ops", med, "ops", p))
    assert can_approve("operator", "ops2", med, "ops", p) is None
    high = CATALOG["RESTART_KNOWN_WINDOWS_SERVICE"]
    assert "administrator" in str(can_approve("operator", "ops", high, "system", p))
    assert can_request("employee", CATALOG["REFRESH_TELEMETRY"]) is None
    assert can_request("employee", med) is not None
    assert can_request("viewer", CATALOG["REFRESH_TELEMETRY"]) is not None
    assert not has_permission("operator", "remediation.manage_policy")
    assert has_permission("admin", "remediation.manage_policy")


def test_kill_switches() -> None:
    ks = KillSwitches(actions=["RESTART_KNOWN_APPLICATION"], device_groups=["lab"], devices=["d9"])
    assert ks.blocking("default", None, "REFRESH_TELEMETRY", "d1") is None
    assert ks.blocking("default", None, "RESTART_KNOWN_APPLICATION", "d1") == "ACTION_TYPE_KILL_SWITCH"
    assert ks.blocking("default", "lab", "REFRESH_TELEMETRY", "d1") == "DEVICE_GROUP_KILL_SWITCH"
    assert ks.blocking("default", None, "REFRESH_TELEMETRY", "d9") == "DEVICE_KILL_SWITCH"
    assert KillSwitches(global_=True).blocking("t", None, "X", "d") == "GLOBAL_REMEDIATION_KILL_SWITCH"
    assert KillSwitches(tenants=["t"]).blocking("t", None, "X", "d") == "TENANT_REMEDIATION_KILL_SWITCH"


def test_maintenance_windows_including_midnight() -> None:
    w = MaintenanceWindow(days=[0], start="22:00", end="02:00", timezone="UTC")  # Monday night
    monday = datetime(2026, 10, 5, 23, 0, tzinfo=UTC)
    assert w.contains(monday)
    assert w.contains(monday + timedelta(hours=2, minutes=30))  # Tuesday 01:30 belongs to Monday's window
    assert not w.contains(monday + timedelta(hours=4))
    assert not w.contains(monday - timedelta(hours=2))
    p = RemediationPolicy(maintenance_windows=[w])
    assert p.in_maintenance("any", monday) and not p.in_maintenance("any", monday + timedelta(hours=12))


@pytest.mark.parametrize(
    "raw",
    [
        {
            "risk_modes": {
                "LOW": "YOLO",
                "MEDIUM": "MANUAL_APPROVAL",
                "HIGH": "MANUAL_APPROVAL",
                "CRITICAL": "DISABLED",
            }
        },
        {"approval_ttl_s": 5},
        {"surprise": 1},
        {"four_eyes_min_risk": "NEVER"},
        {"extra_applications": [{"application_id": "evil.app", "executables": ["C:\\Windows\\cmd.exe"]}]},
        {"maintenance_windows": [{"days": [9]}]},
    ],
)
def test_policy_validation_rejects_bad_input(raw: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        RemediationPolicy.from_dict({**RemediationPolicy().public(), **raw})


def test_policy_roundtrip_and_extra_applications() -> None:
    p = RemediationPolicy.from_dict({**RemediationPolicy().public(), "extra_applications": [
        {"application_id": "vendor.tool", "name": "Tool", "executables": ["Tool.exe"]}],
        "disabled_applications": ["zoom.client"]})  # fmt: skip
    apps = p.applications()
    assert apps["vendor.tool"].executables == ("tool.exe",) and "zoom.client" not in apps
    assert RemediationPolicy.from_dict(p.public()).public() == p.public()


# ------------------------------------------------------------------ envelope
def _env(signer: Signer, **kw: Any) -> Any:
    args = {"execution_id": "e1", "remediation_id": "r1", "action_id": "REFRESH_TELEMETRY", "action_version": 1,
            "device_id": "d1", "requested_by": "a", "approved_by": "b", "policy_version": 1, "parameters": {},
            "execution_timeout_s": 60, "ttl_s": 120, "now": NOW}  # fmt: skip
    args.update(kw)
    return issue(signer, **args)


def test_envelope_sign_verify_and_tamper() -> None:
    s = Signer.from_b64(Signer.generate_b64())
    env = _env(s).public()
    assert verify(s.public_key_b64, env).execution_id == "e1"
    for k, v in (("device_id", "d2"), ("parameters", {"x": 1}), ("expires_at", "2030-01-01T00:00:00+00:00"),
                 ("dry_run", True)):  # fmt: skip
        with pytest.raises(ValueError):
            verify(s.public_key_b64, {**env, k: v})
    other = Signer.from_b64(Signer.generate_b64())
    with pytest.raises(ValueError):
        verify(other.public_key_b64, env)
    assert _env(s).nonce != _env(s).nonce  # fresh nonce per issue
    with pytest.raises(ValueError):
        Signer.from_b64("c2hvcnQ=")


# ------------------------------------------------------------------ verification
def _obs(**kw: Any) -> verification.Observation:
    base = {"agent_outcome": "completed", "agent_detail": None, "fresh_after_completion": True,
            "application_running": True, "target_before": 92.0, "target_now": 45.0, "elapsed_s": 30}  # fmt: skip
    base.update(kw)
    return verification.Observation(**base)


def test_verification_outcomes() -> None:
    restart = CATALOG["RESTART_KNOWN_APPLICATION"]
    assert verification.evaluate(restart, _obs())[0] == "SUCCEEDED"
    assert verification.evaluate(restart, _obs(target_now=88.0))[0] is None  # keep waiting
    assert verification.evaluate(restart, _obs(target_now=88.0, elapsed_s=400))[0] == "PARTIALLY_SUCCEEDED"
    assert verification.evaluate(restart, _obs(application_running=False))[0] == "FAILED"
    assert verification.evaluate(restart, _obs(agent_outcome="failed"))[0] == "FAILED"
    assert verification.evaluate(restart, _obs(fresh_after_completion=False, elapsed_s=400))[0] == "FAILED"
    assert verification.evaluate(restart, _obs(target_now=68.0))[0] == "SUCCEEDED"  # below 70 %
    # "the command returned" alone is never success
    assert verification.evaluate(CATALOG["REFRESH_TELEMETRY"], _obs(fresh_after_completion=False))[0] is None


# ------------------------------------------------------------------ recommendation
def _diag(process: str = "slack.exe", level: str = "HIGH", **over: Any) -> dict[str, Any]:
    d = {"diagnosis_id": "dg1", "series_id": "s1", "status": "AVAILABLE", "confidence": 0.82,
         "diagnosis_type": "CPU_PRESSURE", "likely_cause": "Sustained CPU usage by slack.exe is a likely contributor",
         "hypotheses": [{"code": "cpu.process", "cause": "c", "supporting": ["E1", "E2"], "confidence": 0.82,
                         "confidence_level": level}],
         "evidence": [{"evidence_id": "E1", "type": "OBSERVATION", "statement": "CPU is 92%"},
                      {"evidence_id": "E2", "type": "PROCESS", "statement": f"{process} 70%", "process": process}],
         "explanation": {"missing": []}}  # fmt: skip
    d.update(over)
    return d


def test_recommendation_maps_known_application_only() -> None:
    c = recommend.from_diagnosis(_diag(), KNOWN_APPLICATIONS)
    assert [(x.action_id, x.parameters) for x in c] == [
        ("RESTART_KNOWN_APPLICATION", {"application_id": "slack.desktop"})
    ]
    assert c[0].diagnosis_confidence == 0.82 and c[0].action_confidence == round(0.82 * 0.9, 3)
    assert c[0].action_confidence != c[0].diagnosis_confidence  # different concepts
    assert recommend.from_diagnosis(_diag("builder.exe"), KNOWN_APPLICATIONS) == []
    assert recommend.from_diagnosis(_diag(level="LOW"), KNOWN_APPLICATIONS) == []


def test_untrusted_ai_suggestions_go_through_the_catalog() -> None:
    d = _diag("builder.exe")
    for raw, why in (({"action_type": "RUN_POWERSHELL", "parameters": {"cmd": "rm -rf"}}, "catalog"),
                     ({"action_type": "RESTART_AGENT"}, "disabled"),
                     ({"action_type": "RESTART_KNOWN_APPLICATION", "parameters": {"application_id": "C:\\x.exe"}},
                      "invalid parameters"),
                     ({"action_type": "RESTART_KNOWN_APPLICATION", "parameters": {"application_id": "evil.app"}},
                      "approved list")):  # fmt: skip
        c = recommend.from_ai(raw, KNOWN_APPLICATIONS, d)
        assert c.rejected is not None and why in c.rejected
    ok = recommend.from_ai({"action_type": "REFRESH_TELEMETRY", "parameters": {}}, KNOWN_APPLICATIONS, d)
    assert ok.rejected is None and ok.source == "ai" and ok.action_confidence == round(0.82 * 0.6, 3)
    d2 = _diag("builder.exe", explanation={"suggested_action": {"action_type": "RUN_SHELL"}, "missing": []})
    assert recommend.from_diagnosis(d2, KNOWN_APPLICATIONS) == []


def test_expected_success_is_smoothed_history() -> None:
    assert recommend.expected_success("RESTART_KNOWN_APPLICATION", 0, 0) == 0.6
    assert recommend.expected_success("RESTART_KNOWN_APPLICATION", 0, 10) == 0.3
    assert recommend.expected_success("RESTART_KNOWN_APPLICATION", 90, 90) > 0.95


def test_alert_mapping_agent_health() -> None:
    c = recommend.from_alert({"alert_type": "agent_health", "status": "OPEN", "title": "Agent degraded"})
    assert [x.action_id for x in c] == ["RECONNECT_AGENT"]
    assert recommend.from_alert({"alert_type": "behavioral_anomaly", "status": "OPEN"}) == []


# ------------------------------------------------------------------ lifecycle + audit
def _rem() -> Remediation:
    return Remediation("r1", "default", "d1", "REFRESH_TELEMETRY", 1, "Refresh", "x", "LOW", True, "MANUAL_APPROVAL",
                       {}, [], {}, "NOT_NEEDED", Status.PROPOSED, "system", NOW, NOW, "c1", "e1")  # fmt: skip


def test_lifecycle_rules() -> None:
    r = _rem()
    with pytest.raises(InvalidTransitionError):
        r.transition(Status.EXECUTING, NOW, "x", "skip")  # cannot skip approval
    r.transition(Status.PENDING_APPROVAL, NOW, "system", "approval_required")
    r.transition(Status.APPROVED, NOW, "bob", "approved")
    r.transition(Status.QUEUED, NOW, "bob", "queued")
    r.transition(Status.VALIDATING, NOW, "system", "dispatched")
    r.transition(Status.EXECUTING, NOW, "agent", "started")
    r.transition(Status.VERIFYING, NOW, "agent", "completed")
    r.transition(Status.SUCCEEDED, NOW, "system", "verified")
    assert r.result == "SUCCEEDED" and not r.is_open
    with pytest.raises(InvalidTransitionError):
        r.transition(Status.FAILED, NOW, "x", "rewrite history")  # terminal records are immutable
    assert [a.action for a in r.audit] == ["approval_required", "approved", "queued", "dispatched", "started",
                                           "completed", "verified"]  # fmt: skip


def test_audit_chain_detects_tampering() -> None:
    repo = MemoryRemediationRepository()
    r = _rem()

    async def go() -> None:
        r.transition(Status.PENDING_APPROVAL, NOW, "system", "approval_required")
        await repo.save(r)
        r.transition(Status.REJECTED, NOW, "bob", "rejected", reason="no")
        await repo.save(r)
        await repo.save(r)  # saving again does not duplicate entries

    asyncio.run(go())
    ok = asyncio.run(repo.verify_audit())
    assert ok["ok"] and ok["rows"] == 2
    repo.audit_rows[0]["entry"].actor = "mallory"  # rewrite who did it
    bad = asyncio.run(repo.verify_audit())
    assert bad["ok"] is False and bad["first_bad"] == 0
