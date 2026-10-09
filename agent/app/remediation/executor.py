"""Validate and execute one signed action envelope; produce the reports sent back to the platform.

Local allowlist (endpoint owner's control, configuration only):
    AGENT_REMEDIATION_ACTIONS             default REFRESH_TELEMETRY,REQUEST_SYSTEM_RESCAN,RECONNECT_AGENT
                                          (nothing on the system changes); RESTART_KNOWN_APPLICATION must be
                                          added explicitly
    AGENT_RESTARTABLE_APPLICATIONS        application ids allowed for RESTART_KNOWN_APPLICATION, from the
                                          built-in registry (or "id=exe1.exe|exe2.exe" entries)
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from app.remediation.apps import AppController, wait_running
from app.remediation.envelope import Envelope, EnvelopeError, parse_and_verify
from app.remediation.ledger import ExecutionLedger

log = structlog.get_logger("agent.remediation")

CLOCK_SKEW_S = 120
APP_ID = re.compile(r"^[a-z0-9]+(\.[a-z0-9-]+){1,3}$")
EXE_NAME = re.compile(r"^[a-z0-9][a-z0-9._ -]{0,62}\.exe$")
#: actions this agent version implements, with the envelope action versions it accepts
IMPLEMENTED: dict[str, tuple[int, ...]] = {
    "REFRESH_TELEMETRY": (1,),
    "REQUEST_SYSTEM_RESCAN": (1,),
    "RECONNECT_AGENT": (1,),
    "RESTART_KNOWN_APPLICATION": (1,),
}
DEFAULT_ACTIONS = ("REFRESH_TELEMETRY", "REQUEST_SYSTEM_RESCAN", "RECONNECT_AGENT")
#: same curated registry as the platform (both sides must agree for a restart to happen)
KNOWN_APPLICATIONS: dict[str, tuple[str, ...]] = {
    "microsoft.teams": ("ms-teams.exe", "teams.exe"),
    "microsoft.onedrive": ("onedrive.exe",),
    "slack.desktop": ("slack.exe",),
    "zoom.client": ("zoom.exe",),
}
CLOSE_TIMEOUT_S = 20.0
RELAUNCH_TIMEOUT_S = 30.0

Report = tuple[str, str, dict[str, Any]]  # (phase, detail, data)


def parse_applications(entries: list[str]) -> dict[str, tuple[str, ...]]:
    """'microsoft.teams' (built-in) or 'vendor.app=app.exe|helper.exe' (custom); invalid ones are ignored."""
    out: dict[str, tuple[str, ...]] = {}
    for raw in entries:
        app_id, _, exes = raw.strip().partition("=")
        app_id = app_id.strip().lower()
        if not APP_ID.match(app_id):
            continue
        names = (
            tuple(x.strip().lower() for x in exes.split("|") if x.strip())
            if exes
            else KNOWN_APPLICATIONS.get(app_id, ())
        )
        names = tuple(n for n in names if EXE_NAME.match(n))
        if names:
            out[app_id] = names
    return out


@dataclass
class Hooks:
    refresh: Callable[[], Awaitable[dict[str, Any]]]
    rescan: Callable[[], Awaitable[dict[str, Any]]]
    reconnect: Callable[[], Awaitable[dict[str, Any]]]
    run_mode: str = "console"


@dataclass
class ActionExecutor:
    device_id: str
    public_key: Callable[[], str | None]
    ledger: ExecutionLedger
    hooks: Hooks
    allowed_actions: tuple[str, ...] = DEFAULT_ACTIONS
    applications: dict[str, tuple[str, ...]] = field(default_factory=dict)
    apps: AppController | None = None
    running: set[str] = field(default_factory=set)  # lock keys in use

    async def handle(self, data: Any) -> list[tuple[str, Report]]:
        """-> [(execution_id, report), ...] in order. Never raises."""
        execution_id = str(data.get("execution_id", ""))[:64] if isinstance(data, dict) else ""
        try:
            env = self._validate(data)
        except EnvelopeError as exc:
            log.warning("action_rejected", execution_id=execution_id, reason=str(exc))
            return [(execution_id, ("rejected", str(exc), {}))] if execution_id else []
        previous = self.ledger.get(env.execution_id)
        if previous is not None:  # idempotency: never run twice; repeat the earlier outcome
            phase = previous.get("phase")
            if phase == "running":
                return [
                    (
                        env.execution_id,
                        (
                            "failed",
                            "the agent stopped while this action was running; it was not "
                            "retried automatically",
                            {"interrupted": True},
                        ),
                    )
                ]
            return [
                (
                    env.execution_id,
                    (str(phase), str(previous.get("detail", "")), dict(previous.get("data") or {})),
                )
            ]
        lock = self._lock_key(env)
        if lock in self.running:
            return [(env.execution_id, ("rejected", "a conflicting action is running on this device", {}))]
        pre = self._preconditions(env)
        if pre:
            return [(env.execution_id, ("rejected", pre, {}))]
        self.ledger.begin(env.execution_id, env.nonce, env.action_id)
        out: list[tuple[str, Report]] = [(env.execution_id, ("accepted", "validated; executing", {}))]
        self.running.add(lock)
        try:
            if env.dry_run:
                report: Report = (
                    "completed",
                    f"dry run: {self._describe(env)}; nothing was changed",
                    {"dry_run": True},
                )
            else:
                report = await asyncio.wait_for(self._execute(env), timeout=max(5, env.execution_timeout_s))
        except TimeoutError:
            report = ("failed", f"execution timed out after {env.execution_timeout_s}s", {})
        except Exception as exc:
            report = ("failed", f"{type(exc).__name__}: {str(exc)[:200]}", {})
        finally:
            self.running.discard(lock)
        self.ledger.finish(env.execution_id, *report)
        log.info(
            "action_finished",
            execution_id=env.execution_id,
            action=env.action_id,
            phase=report[0],
            dry_run=env.dry_run,
        )
        out.append((env.execution_id, report))
        return out

    # ------------------------------------------------------------------ validation
    def _validate(self, data: Any) -> Envelope:
        key = self.public_key()
        if not key:
            raise EnvelopeError("no pinned platform key: refusing all actions")
        env = parse_and_verify(data, key)
        if env.device_id != self.device_id:
            raise EnvelopeError("envelope is for another device")
        now = datetime.now(UTC)
        try:
            issued, expires = datetime.fromisoformat(env.issued_at), datetime.fromisoformat(env.expires_at)
        except ValueError as exc:
            raise EnvelopeError("malformed timestamps") from exc
        if issued > now + timedelta(seconds=CLOCK_SKEW_S):
            raise EnvelopeError("envelope issued in the future")
        if expires <= now:
            raise EnvelopeError("envelope expired")
        if expires - issued > timedelta(hours=1):
            raise EnvelopeError("envelope validity too long")
        if env.action_id not in IMPLEMENTED or env.action_version not in IMPLEMENTED[env.action_id]:
            raise EnvelopeError(f"unsupported action {env.action_id} v{env.action_version}")
        if env.action_id not in self.allowed_actions:
            raise EnvelopeError(f"{env.action_id} is not allowed on this device (local policy)")
        if self.ledger.nonce_used(env.nonce, env.execution_id):
            raise EnvelopeError("replayed nonce")
        self._check_params(env)
        return env

    def _check_params(self, env: Envelope) -> None:
        p = env.parameters
        if env.action_id == "RESTART_KNOWN_APPLICATION":
            if set(p) != {"application_id"} or not isinstance(p["application_id"], str):
                raise EnvelopeError("invalid parameters")
            if p["application_id"] not in self.applications:
                raise EnvelopeError("application is not on this device's restart allowlist")
        elif p:
            raise EnvelopeError("this action takes no parameters")

    def _lock_key(self, env: Envelope) -> str:
        return "device"  # one action at a time per device (connection, applications and agent all conflict)

    def _preconditions(self, env: Envelope) -> str | None:
        if env.action_id == "RESTART_KNOWN_APPLICATION" and not env.dry_run:
            if self.hooks.run_mode == "service":
                return (
                    "the agent runs as a service (session 0); desktop applications are restarted only by an "
                    "agent running in the user's session"
                )
            if self.apps is None:
                return "application control is not available on this platform"
            exes = self.applications[env.parameters["application_id"]]
            if not self.apps.find(exes):
                return "the application is not running in this user session"
        return None

    def _describe(self, env: Envelope) -> str:
        if env.action_id == "RESTART_KNOWN_APPLICATION":
            exes = self.applications.get(env.parameters.get("application_id", ""), ())
            running = len(self.apps.find(exes)) if self.apps is not None else 0
            return f"would ask {', '.join(exes)} ({running} process(es) running) to close and start it again"
        return {
            "REFRESH_TELEMETRY": "would collect every sensor now",
            "REQUEST_SYSTEM_RESCAN": "would re-run inventory discovery",
            "RECONNECT_AGENT": "would reconnect to the platform",
        }[env.action_id]

    # ------------------------------------------------------------------ execution
    async def _execute(self, env: Envelope) -> Report:
        if env.action_id == "REFRESH_TELEMETRY":
            return (
                "completed",
                "collected every sensor and queued a full snapshot",
                await self.hooks.refresh(),
            )
        if env.action_id == "REQUEST_SYSTEM_RESCAN":
            return ("completed", "inventory rediscovered and queued", await self.hooks.rescan())
        if env.action_id == "RECONNECT_AGENT":
            return ("completed", "connections reset; reconnecting", await self.hooks.reconnect())
        return await asyncio.to_thread(self._restart_app, env.parameters["application_id"])

    def _restart_app(self, app_id: str) -> Report:
        assert self.apps is not None
        exes = self.applications[app_id]
        procs = self.apps.find(exes)
        if not procs:
            return ("failed", "the application was not running", {"running_after": False})
        paths = {
            p.exe_path
            for p in procs
            if p.exe_path and os.path.isabs(p.exe_path) and Path(p.exe_path).name.lower() in exes
        }
        main = next((x for x in sorted(paths) if Path(x).name.lower() == exes[0]), None) or (
            sorted(paths)[0] if paths else None
        )
        if main is None or not os.path.isfile(main):
            return ("failed", "could not determine the application's program file; nothing was changed", {})
        old = {p.pid for p in procs}
        if not self.apps.close_gracefully(sorted(old), CLOSE_TIMEOUT_S):
            return (
                "failed",
                f"the application did not close within {CLOSE_TIMEOUT_S:.0f}s; it was not forced and "
                "is still running",
                {"running_after": True, "closed": False},
            )
        self.apps.launch(main)
        fresh = wait_running(self.apps, exes, old, RELAUNCH_TIMEOUT_S)
        if not fresh:
            return (
                "failed",
                "the application closed but did not start again",
                {"running_after": False, "closed": True},
            )
        return (
            "completed",
            f"{Path(main).name} closed and restarted",
            {"running_after": True, "closed": True, "new_processes": len(fresh)},
        )
