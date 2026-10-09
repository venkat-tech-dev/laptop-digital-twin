"""Remediation recommendations: deterministic mapping from diagnoses / alerts to catalog actions.

AI output is an untrusted suggestion: it may name an ``action_type`` and parameters, which pass the same
catalog, schema, allowlist and policy validation as anything else, and is otherwise discarded. Nothing
here produces commands, paths or scripts.

Three numbers are kept apart:
    diagnosis_confidence          how sure the platform is about the cause (Phase 7, from evidence)
    action_confidence             how likely this action addresses that cause (diagnosis x applicability)
    expected_success_probability  how often this action executes and verifies successfully (history,
                                  smoothed with a conservative prior until enough attempts exist)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.remediation.catalog import CATALOG, ActionDefinition, KnownApplication

#: hypothesis code -> (action, applicability): a restart addresses process-driven pressure well, leaks less so
APPLICABILITY = {"cpu.process": 0.9, "memory.process_growth": 0.85, "memory.leak_like": 0.7}
PRIOR_SUCCESS = {
    "REFRESH_TELEMETRY": (0.9, 10),
    "REQUEST_SYSTEM_RESCAN": (0.9, 10),
    "RECONNECT_AGENT": (0.85, 10),
    "RESTART_KNOWN_APPLICATION": (0.6, 10),
}  # (prior rate, prior weight in attempts)


@dataclass
class Candidate:
    action_id: str
    parameters: dict[str, Any]
    reason: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    diagnosis_confidence: float | None = None
    action_confidence: float | None = None
    source: str = "rules"
    rejected: str | None = None  # why an AI suggestion was not accepted


def match_application(process: str | None, apps: dict[str, KnownApplication]) -> KnownApplication | None:
    name = (process or "").strip().lower()
    return next((a for a in apps.values() if name in a.executables), None) if name else None


def expected_success(action_id: str, successes: int, attempts: int) -> float:
    rate, weight = PRIOR_SUCCESS.get(action_id, (0.5, 10))
    return round((successes + rate * weight) / (attempts + weight), 3)


def from_diagnosis(d: dict[str, Any], apps: dict[str, KnownApplication]) -> list[Candidate]:
    """Diagnosis (to_dict(full=True)) -> candidates, best first."""
    out: list[Candidate] = []
    if d.get("status") not in ("AVAILABLE", "LOW_CONFIDENCE", "INSUFFICIENT_EVIDENCE"):
        return out
    ev = {e["evidence_id"]: e for e in d.get("evidence") or []}
    hyps = d.get("hypotheses") or []
    primary = hyps[0] if hyps else None
    if (
        primary
        and primary.get("code") in APPLICABILITY
        and primary.get("confidence_level") in ("HIGH", "MEDIUM")
    ):
        procs = [ev[i] for i in primary.get("supporting") or [] if i in ev and ev[i].get("type") == "PROCESS"]
        for e in procs:
            app = match_application(e.get("process"), apps)
            if app is None:
                continue
            conf = float(primary.get("confidence") or 0)
            out.append(
                Candidate(
                    "RESTART_KNOWN_APPLICATION",
                    {"application_id": app.application_id},
                    f"{d.get('likely_cause') or primary.get('cause')}. {app.name} is an approved application that "  # noqa: E501
                    "can be restarted.",
                    [
                        {"evidence_id": i, "statement": ev[i]["statement"]}
                        for i in primary.get("supporting") or []
                        if i in ev
                    ],
                    conf,
                    round(conf * APPLICABILITY[primary["code"]], 3),
                )
            )
            break
    missing = " ".join((d.get("explanation") or {}).get("missing") or []).lower()
    if d.get("status") == "INSUFFICIENT_EVIDENCE" and (
        "not available" in missing or "no process snapshots" in missing or "no recent" in missing
    ):
        out.append(
            Candidate(
                "REFRESH_TELEMETRY",
                {},
                "The diagnosis lacks telemetry; a fresh snapshot gives it more evidence.",
                [],
                float(d.get("confidence") or 0),
                0.5,
            )
        )
    sug = (d.get("explanation") or {}).get("suggested_action")
    if isinstance(sug, dict):
        c = from_ai(sug, apps, d)
        if c.rejected is None and all(
            x.action_id != c.action_id or x.parameters != c.parameters for x in out
        ):
            out.append(c)
    return out


def from_alert(a: dict[str, Any]) -> list[Candidate]:
    """Agent-health alerts -> reconnect (LOW, nothing on the system changes)."""
    if a.get("alert_type") == "agent_health" and a.get("status") in ("OPEN", "ONGOING"):
        return [
            Candidate(
                "RECONNECT_AGENT",
                {},
                f"{a.get('title')}: {a.get('summary') or ''}".strip(": "),
                [],
                a.get("confidence"),
                0.6,
            )
        ]
    return []


def from_ai(raw: dict[str, Any], apps: dict[str, KnownApplication], d: dict[str, Any]) -> Candidate:
    """Validate an untrusted AI suggestion against the catalog. Never raises; sets ``rejected``."""
    action_id = str(raw.get("action_type") or "")[:64]
    params = raw.get("parameters") if isinstance(raw.get("parameters"), dict) else {}
    cand = Candidate(
        action_id,
        {},
        "Suggested by the local AI model and validated against the action catalog.",
        source="ai",
        diagnosis_confidence=d.get("confidence"),
    )
    action: ActionDefinition | None = CATALOG.get(action_id)
    if action is None:
        cand.rejected = "not in the action catalog"
        return cand
    if not action.enabled:
        cand.rejected = f"action disabled: {action.disabled_reason}"
        return cand
    try:
        cand.parameters = action.validate_params(params)
    except ValueError as exc:
        cand.rejected = str(exc)[:200]
        return cand
    app_id = cand.parameters.get("application_id")
    if app_id is not None and app_id not in apps:
        cand.rejected = "application is not on the approved list"
        return cand
    cand.action_confidence = round(float(d.get("confidence") or 0) * 0.6, 3)  # AI-only: discounted
    return cand
