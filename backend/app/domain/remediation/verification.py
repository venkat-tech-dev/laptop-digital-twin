"""Post-action verification from real telemetry.

An action is never successful because the agent said "done". Each catalog action lists checks:

    agent_completed      the agent executed the action (signed envelope accepted, no local error)  [hard]
    fresh_telemetry      telemetry newer than the agent's completion time has arrived               [hard]
    application_running  the restarted application is running again (agent report, then telemetry) [hard]
    target_improved      the condition that triggered it improved: the 1-minute mean of the target
                         signal is below ``target_below`` or at least 25 % lower than before         [soft]

Outcome: every check passes -> SUCCEEDED; a hard check fails -> FAILED; at the verification timeout,
hard checks passed but a soft one did not -> PARTIALLY_SUCCEEDED, a hard check still pending -> FAILED.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.remediation.catalog import ActionDefinition

HARD = ("agent_completed", "fresh_telemetry", "application_running")


@dataclass
class Observation:
    agent_outcome: str | None  # completed | failed | rejected | None (no report yet)
    agent_detail: str | None
    fresh_after_completion: bool
    application_running: bool | None  # None: unknown (no process data)
    target_before: float | None
    target_now: float | None
    elapsed_s: float


def evaluate(action: ActionDefinition, obs: Observation) -> tuple[str | None, list[dict[str, Any]]]:
    """-> (final status or None to keep waiting, checks)."""
    v = action.verification
    checks: list[dict[str, Any]] = []
    for code in v.checks:
        state, detail = "PENDING", ""
        if code == "agent_completed":
            if obs.agent_outcome == "completed":
                state, detail = "PASS", "the agent executed the action"
            elif obs.agent_outcome in ("failed", "rejected"):
                state, detail = (
                    "FAIL",
                    f"the agent reported {obs.agent_outcome}: {obs.agent_detail or 'no detail'}",
                )
            else:
                detail = "waiting for the agent's report"
        elif code == "fresh_telemetry":
            state = "PASS" if obs.fresh_after_completion else "PENDING"
            detail = (
                "telemetry received after completion"
                if obs.fresh_after_completion
                else "waiting for telemetry"
            )
        elif code == "application_running":
            if obs.application_running is True:
                state, detail = "PASS", "the application is running again"
            elif obs.application_running is False and obs.agent_outcome == "completed":
                state, detail = "FAIL", "the application is not running after the restart"
            else:
                detail = "waiting for process data"
        elif code == "target_improved":
            before, now = obs.target_before, obs.target_now
            if now is None:
                detail = f"no {v.target_signal} data yet"
            elif (v.target_below is not None and now < v.target_below) or (
                before is not None and now <= before * 0.75
            ):
                state = "PASS"
                detail = f"{v.target_signal} {_fmt(before)} -> {_fmt(now)}"
                if before is not None and v.target_below is not None and before < v.target_below:
                    detail += f" (already below {_fmt(v.target_below)} before: nothing to improve)"
            else:
                detail = (
                    f"{v.target_signal} {_fmt(before)} -> {_fmt(now)} (target below {_fmt(v.target_below)})"
                )
        checks.append({"check": code, "state": state, "detail": detail, "hard": code in HARD})
    if any(c["state"] == "FAIL" and c["hard"] for c in checks):
        return "FAILED", checks
    if all(c["state"] == "PASS" for c in checks):
        return "SUCCEEDED", checks
    if obs.elapsed_s >= v.timeout_s:
        hard_ok = all(c["state"] == "PASS" for c in checks if c["hard"])
        for c in checks:
            if c["state"] == "PENDING":
                c["state"], c["detail"] = "FAIL", (c["detail"] + " (verification timed out)").strip()
        return ("PARTIALLY_SUCCEEDED" if hard_ok else "FAILED"), checks
    return None, checks


def _fmt(v: float | None) -> str:
    return "?" if v is None else f"{v:.0f}%"
