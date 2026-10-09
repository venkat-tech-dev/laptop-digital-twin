"""RemediationService (Phase 8): recommend -> risk/policy -> approval -> validate -> agent -> verify -> audit.

    DiagnosisChanged(available) / AlertChanged(agent health)  --bounded queue-->  recommend (catalog only)
    POST /remediations (user request)                          ------------------>  propose
      propose: catalog + schema + allowlist + policy (mode, kill switch, circuit) -> PENDING_APPROVAL
               (or APPROVED by an explicit LOW-risk auto policy)
      approve: permission + risk ceiling + four-eyes + approval still valid + diagnosis unchanged
      worker tick (2 s): expiry, scheduling / maintenance windows, server preconditions, fleet limit,
               per-device lock -> signed envelope (VALIDATING) -> agent pulls it -> agent reports
               accepted (EXECUTING) / rejected / completed -> VERIFYING -> telemetry checks -> result
      circuit breaker: repeated failures for a device + action stop proposals and automation

It never runs in a request handler or on the telemetry path, and nothing here can produce a command:
the agent receives an action id with typed parameters and validates it again on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.core.metrics import (
    REMEDIATION_CIRCUIT_OPEN,
    REMEDIATION_EVENTS,
    REMEDIATION_LATENCY,
    REMEDIATION_PRECONDITION_WAITS,
)
from app.domain.events.events import (
    AlertChanged,
    DiagnosisChanged,
    DomainEvent,
    RemediationChanged,
    TwinMessage,
)
from app.domain.remediation import recommend, verification
from app.domain.remediation.catalog import CATALOG, ActionDefinition, Risk, version_tuple
from app.domain.remediation.envelope import Signer, issue
from app.domain.remediation.models import IN_FLIGHT, AuditEntry, Mode, Remediation, Status
from app.domain.remediation.policy import RemediationPolicy, can_approve, can_request
from app.repositories.base import SystemEventRecord
from app.repositories.remediation import RemediationRepository

log = structlog.get_logger("remediation")

POLICY_KEY = "remediation_policy"
STALE_TELEMETRY_S = 180
TICK_S = 2.0
HISTORY_DAYS = 7
REPORT_PHASES = ("accepted", "rejected", "completed", "failed")
TARGET_KEYS = {"cpu": "cpu.usage_percent", "memory": "memory.usage_percent"}


class RemediationError(ValueError):
    """A request that must not proceed; ``code`` is machine-readable, the message is for people."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class RemediationService:
    def __init__(
        self,
        settings: Any,
        twins: Any,
        presence: Any,
        repo: RemediationRepository,
        store: Any,  # settings store (admin repository)
        publish: Callable[[list[DomainEvent]], Awaitable[None]],
        record: Callable[[SystemEventRecord], None],
        signer: Signer | None,
        assignments: Any = None,
        diagnoses: Any = None,  # DiagnosisService (read-only use)
        on_twin_changed: Callable[[str], Awaitable[Any]] | None = None,
        tenant_id: str = "default",
    ) -> None:
        self._s = settings
        self._twins = twins
        self._presence = presence
        self.repo = repo
        self._store = store
        self._publish = publish
        self._record = record
        self.signer = signer
        self._assignments = assignments
        self._diagnoses = diagnoses
        self._on_twin_changed = on_twin_changed
        self.tenant_id = tenant_id
        # Phase 9 hooks (set by the container)
        self.tenant_of: Callable[[str], str] = lambda _d: self.tenant_id
        self.governance: Any = None  # PolicyService: per-organisation / group / device remediation policy
        self.lifecycle_of: Callable[[str], Any] = lambda _d: None
        self.policy = RemediationPolicy()
        self.items: dict[str, Remediation] = {}
        self._by_device: dict[str, list[str]] = {}  # device -> remediation ids (index; no fleet-wide scans)
        self._outcomes: dict[str, list[int]] = {}  # action -> [successes, attempts] (expected success)
        self._in_flight = 0  # recomputed once per tick, updated on dispatch
        self._last_prune = 0.0
        self._by_execution: dict[str, str] = {}  # execution id -> remediation id (agent reports)
        self.circuits: dict[tuple[str, str], datetime] = {}  # (device, action) -> open until
        self._queue: asyncio.Queue[DomainEvent] = asyncio.Queue(maxsize=5000)
        self._running = False
        self._lock = asyncio.Lock()
        self.stats: dict[str, int] = {
            "proposed": 0,
            "deduplicated": 0,
            "auto_approved": 0,
            "dispatched": 0,
            "events_dropped": 0,
            "rejected_reports": 0,
        }

    # ------------------------------------------------------------------ lifecycle
    async def load(self) -> None:
        try:
            stored = await self._store.get_setting(POLICY_KEY)
            if stored:
                self.policy = RemediationPolicy.from_dict(stored)
        except Exception as exc:
            log.warning("remediation_policy_load_failed", error=str(exc)[:200])
        try:
            for r in await self.repo.recent(datetime.now(UTC) - timedelta(days=HISTORY_DAYS)):
                self._index(r)
        except Exception as exc:
            log.warning("remediation_state_load_failed", error=str(exc)[:200])
        self._rebuild_circuits(datetime.now(UTC))

    async def run(self, stop: asyncio.Event) -> None:
        self._running = True
        events = asyncio.create_task(self._event_loop(stop), name="remediation_events")
        try:
            while not stop.is_set():
                if events.done():  # the event consumer died: let the supervisor restart the service
                    raise RuntimeError(f"remediation_events stopped: {events.exception()!r}")
                try:
                    await self.tick(datetime.now(UTC))
                except Exception:
                    log.exception("remediation_tick_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), TICK_S)
        finally:
            events.cancel()
            await asyncio.gather(events, return_exceptions=True)

    async def on_event(self, event: DomainEvent) -> None:
        """EventBus subscriber: enqueue only (never blocks the bus)."""
        if not isinstance(event, (DiagnosisChanged, AlertChanged)):
            return
        if self._running:
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                self.stats["events_dropped"] += 1
            return
        await self.process_event(event)

    async def _event_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.process_event(await self._queue.get())

    async def drain(self) -> None:
        while not self._queue.empty():
            await self.process_event(self._queue.get_nowait())

    async def process_event(self, event: DomainEvent) -> None:
        try:
            if isinstance(event, DiagnosisChanged) and event.kind == "available":
                await self._on_diagnosis(event.device_id, event.diagnosis)
            elif isinstance(event, AlertChanged) and event.kind == "created":
                for c in recommend.from_alert(event.alert):
                    await self._propose_system(event.device_id, c, alert_id=event.alert.get("alert_id"))
        except Exception as exc:
            log.warning("remediation_event_failed", error=str(exc)[:300])

    async def _on_diagnosis(self, device_id: str, summary: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        # a newer version of a diagnosis invalidates approvals based on a materially different earlier one
        for r in self._open(device_id):
            if (
                r.status in (Status.PENDING_APPROVAL, Status.APPROVED, Status.QUEUED)
                and r.correlation_id == summary.get("series_id")
                and r.diagnosis_id != summary.get("diagnosis_id")
            ):
                old_type = (r.verification_rules or {}).get("diagnosis_type")
                if old_type and old_type != summary.get("diagnosis_type"):
                    await self._finish(
                        r,
                        Status.EXPIRED,
                        now,
                        "system",
                        "invalidated",
                        reason="the diagnosis changed materially; a new approval is required",
                    )
        if self._diagnoses is None or summary.get("status") not in (
            "AVAILABLE",
            "LOW_CONFIDENCE",
            "INSUFFICIENT_EVIDENCE",
        ):
            return
        detail = await self._diagnoses.detail(summary["diagnosis_id"])
        if detail is None:
            return
        for c in recommend.from_diagnosis(detail, self.policy.applications()):
            if c.action_id == "REFRESH_TELEMETRY" and not self.telemetry_stale(device_id):
                continue  # missing evidence alone is no reason to act; only a stalled pipeline is
            await self._propose_system(device_id, c, diagnosis=detail)

    async def _propose_system(
        self,
        device_id: str,
        c: recommend.Candidate,
        diagnosis: dict[str, Any] | None = None,
        alert_id: str | None = None,
    ) -> Remediation | None:
        try:
            return await self.propose(
                device_id,
                c.action_id,
                c.parameters,
                "system",
                candidate=c,
                diagnosis=diagnosis,
                alert_id=alert_id,
            )
        except RemediationError as exc:
            log.info("remediation_not_proposed", device_id=device_id, action=c.action_id, code=exc.code)
            return None

    # ------------------------------------------------------------------ helpers
    def _index(self, r: Remediation) -> None:
        self._by_execution[r.execution_id] = r.remediation_id
        if r.remediation_id not in self.items:
            self._by_device.setdefault(r.device_id, []).append(r.remediation_id)
            if r.result and not r.dry_run:
                o = self._outcomes.setdefault(r.action_type, [0, 0])
                o[0] += r.result == "SUCCEEDED"
                o[1] += 1
        self.items[r.remediation_id] = r

    def _device_items(self, device_id: str) -> list[Remediation]:
        return [self.items[i] for i in self._by_device.get(device_id, ()) if i in self.items]

    def _open(self, device_id: str | None = None) -> list[Remediation]:
        rows = self.items.values() if device_id is None else self._device_items(device_id)
        return [r for r in rows if r.is_open]

    def _prune(self, now: datetime) -> None:
        """Keep only open and recent records in memory (everything stays in the database)."""
        cutoff = now - timedelta(days=HISTORY_DAYS)
        old = [k for k, r in self.items.items() if not r.is_open and r.updated_at < cutoff]
        for k in old:
            r = self.items.pop(k)
            ids = self._by_device.get(r.device_id)
            if ids is not None and k in ids:
                ids.remove(k)
            if r.execution_id:
                self._by_execution.pop(r.execution_id, None)

    def _group(self, device_id: str) -> str | None:
        deps = self._assignments.departments(device_id) if self._assignments is not None else []
        return deps[0] if deps else None

    def telemetry_stale(self, device_id: str, now: datetime | None = None) -> bool:
        """The agent is alive (recent heartbeat) but telemetry batches stopped arriving."""
        now = now or datetime.now(UTC)
        p = self._presence.get(device_id)
        if p is None or p.last_heartbeat_at is None or (now - p.last_heartbeat_at).total_seconds() > 120:
            return False
        return p.last_batch_at is None or (now - p.last_batch_at).total_seconds() > STALE_TELEMETRY_S

    def policy_for(self, device_id: str) -> RemediationPolicy:
        """Global Phase 8 policy overlaid with what organisation / group / device policies *explicitly* set
        (Phase 9). Platform defaults of the governance schema never override the global policy."""
        if self.governance is None:
            return self.policy
        values, source = self.governance.effective("remediation", self.tenant_of(device_id), device_id)
        v = {k: val for k, val in values.items() if source.get(k) != "platform"}
        if not v:
            return self.policy
        base = self.policy
        modes = dict(base.risk_modes)
        for field_name, risk in (
            ("low_mode", "LOW"),
            ("medium_mode", "MEDIUM"),
            ("high_mode", "HIGH"),
            ("critical_mode", "CRITICAL"),
        ):
            if field_name in v:
                modes[risk] = v[field_name]
        from dataclasses import replace

        return replace(
            base,
            risk_modes=modes,
            auto_remediation_enabled=v.get("auto_remediation_enabled", base.auto_remediation_enabled),
            auto_min_confidence=v.get("auto_min_confidence", base.auto_min_confidence),
            four_eyes_min_risk=v.get("four_eyes_min_risk", base.four_eyes_min_risk),
            approval_ttl_s=v.get("approval_ttl_s", base.approval_ttl_s),
        )

    def org_kill_switch(self, device_id: str) -> bool:
        if self.governance is None:
            return False
        return bool(
            self.governance.effective("remediation", self.tenant_of(device_id), device_id)[0]["kill_switch"]
        )

    def kill_switch(self, action_id: str, device_id: str) -> str | None:
        if getattr(self._s, "remediation_kill_switch", False):
            return "GLOBAL_REMEDIATION_KILL_SWITCH (environment)"
        if self.org_kill_switch(device_id):
            return "ORGANIZATION_REMEDIATION_KILL_SWITCH (policy)"
        return self.policy.kill_switches.blocking(
            self.tenant_of(device_id), self._group(device_id), action_id, device_id
        )

    def _history(self, device_id: str, action_id: str, since: datetime) -> list[Remediation]:
        return [
            r
            for r in self._device_items(device_id)
            if r.device_id == device_id
            and r.action_type == action_id
            and r.created_at >= since
            and not r.dry_run
        ]

    def success_stats(self, action_id: str) -> tuple[int, int]:
        o = self._outcomes.get(action_id, [0, 0])
        return o[0], o[1]

    def _rebuild_circuits(self, now: datetime) -> None:
        p = self.policy
        since = now - timedelta(seconds=p.circuit_window_s)
        fails: dict[tuple[str, str], list[datetime]] = {}
        for r in self.items.values():
            if r.status == Status.FAILED and r.failed_at and r.failed_at >= since and not r.dry_run:
                fails.setdefault((r.device_id, r.action_type), []).append(r.failed_at)
        for key, times in fails.items():
            if len(times) >= p.circuit_failures:
                self.circuits[key] = max(times) + timedelta(seconds=p.circuit_open_s)

    def circuit_open(self, device_id: str, action_id: str, now: datetime) -> bool:
        until = self.circuits.get((device_id, action_id))
        if until is None:
            return False
        if until <= now:
            self.circuits.pop((device_id, action_id), None)
            return False
        return True

    # ------------------------------------------------------------------ propose
    async def propose(
        self,
        device_id: str,
        action_id: str,
        parameters: dict[str, Any] | None,
        requested_by: str,
        *,
        role: str = "system",
        candidate: recommend.Candidate | None = None,
        diagnosis: dict[str, Any] | None = None,
        alert_id: str | None = None,
        mode: Mode = Mode.IMMEDIATE,
        scheduled_at: datetime | None = None,
        dry_run: bool = False,
    ) -> Remediation:
        now = datetime.now(UTC)
        action = CATALOG.get(action_id)
        if action is None:
            raise RemediationError("unknown_action", f"{action_id[:64]} is not in the action catalog", 422)
        if not action.enabled:
            raise RemediationError(
                "action_disabled", f"{action.name} is disabled: {action.disabled_reason}", 422
            )
        if role != "system":
            why = can_request(role, action) if not dry_run else None
            if why:
                raise RemediationError("forbidden", why, 403)
        try:
            params = action.validate_params(parameters)  # schema
        except ValueError as exc:
            raise RemediationError("invalid_parameters", str(exc), 422) from exc
        apps = self.policy.applications()
        if "application_id" in params and params["application_id"] not in apps:  # allowlist
            raise RemediationError("not_allowlisted", "the application is not on the approved list", 422)
        if self._twins.get(device_id) is None:
            raise RemediationError("unknown_device", "unknown device", 404)
        diag_conf = candidate.diagnosis_confidence if candidate else (diagnosis or {}).get("confidence")
        pmode, needs_approval, why = self.policy_for(device_id).approval(
            action, device_id, self._group(device_id), diag_conf
        )
        if pmode == "DISABLED" and not dry_run:
            raise RemediationError("policy_disabled", f"{action.name}: {why}", 409)
        if self.circuit_open(device_id, action_id, now) and not dry_run:
            raise RemediationError(
                "circuit_open",
                "stopped after repeated failures on this device; an administrator "
                "must review before it is proposed again",
                409,
            )
        if mode == Mode.SCHEDULED and (scheduled_at is None or scheduled_at <= now):
            raise RemediationError("invalid_schedule", "scheduled_at must be in the future", 422)
        for r in self._open(device_id):  # one open proposal per device + action + parameters
            if r.action_type == action_id and r.parameters == params and r.dry_run == dry_run:
                self.stats["deduplicated"] += 1
                r.note(
                    now, requested_by, "duplicate_request", source=candidate.source if candidate else "user"
                )
                await self._save(r)
                return r
        successes, attempts = self.success_stats(action_id)
        diag = diagnosis or {}
        app = apps.get(str(params.get("application_id"))) if "application_id" in params else None
        r = Remediation(
            remediation_id=uuid.uuid4().hex,
            tenant_id=self.tenant_of(device_id),
            device_id=device_id,
            action_type=action_id,
            action_version=action.version,
            action_name=action.name + (f": {app.name}" if app else ""),
            description=action.description,
            risk_level=action.risk.value,
            requires_approval=needs_approval and not dry_run,
            approval_policy=pmode if not dry_run else "DRY_RUN",
            parameters=params,
            preconditions=list(action.preconditions),
            verification_rules={
                "checks": list(action.verification.checks),
                "timeout_s": action.verification.timeout_s,
                "target_signal": action.verification.target_signal,
                "target_below": action.verification.target_below,
                "description": action.verification.description,
                "diagnosis_type": diag.get("diagnosis_type"),
            },
            rollback_strategy=action.rollback,
            status=Status.PROPOSED,
            requested_by=requested_by,
            created_at=now,
            updated_at=now,
            correlation_id=str(diag.get("series_id") or alert_id or uuid.uuid4().hex),
            execution_id=uuid.uuid4().hex,
            alert_id=alert_id or diag.get("alert_id"),
            diagnosis_id=diag.get("diagnosis_id"),
            prediction_id=diag.get("prediction_id"),
            reason=(candidate.reason if candidate else "") or diag.get("summary") or "Requested by a user",
            evidence=candidate.evidence if candidate else [],
            diagnosis_confidence=diag_conf,
            action_confidence=candidate.action_confidence if candidate else None,
            expected_success_probability=recommend.expected_success(action_id, successes, attempts),
            recommendation_source=candidate.source if candidate else "user",
            mode=mode,
            scheduled_at=scheduled_at,
            policy_version=self.policy.version,
            dry_run=dry_run,
        )
        if app is not None:
            r.verification_rules["application"] = app.public()
        r.audit.append(
            AuditEntry(
                now,
                requested_by,
                "proposed",
                None,
                Status.PROPOSED.value,
                {
                    "source": r.recommendation_source,
                    "policy": pmode,
                    "policy_version": self.policy.version,
                    "why": why,
                    "parameters": params,
                },
            )
        )
        self._index(r)
        self._by_execution[r.execution_id] = r.remediation_id
        self.stats["proposed"] += 1
        REMEDIATION_EVENTS.labels("proposed", action_id, action.risk.value).inc()
        await self._emit(r, "proposed")
        if dry_run or not needs_approval:
            actor = requested_by if dry_run else "policy"
            r.transition(
                Status.APPROVED,
                now,
                actor,
                "auto_approved" if not dry_run else "dry_run",
                reason=why if not dry_run else "dry run: no changes are made",
            )
            r.approved_by, r.approval_at = actor, now
            r.approval_expires_at = self._execution_deadline(r, now)
            if not dry_run:
                self.stats["auto_approved"] += 1
            await self._queue_it(r, now, actor)
        else:
            r.approval_expires_at = now + timedelta(seconds=self.policy_for(device_id).approval_ttl_s)
            r.transition(
                Status.PENDING_APPROVAL,
                now,
                "system",
                "approval_required",
                reason=why,
                valid_until=r.approval_expires_at.isoformat(),
            )
            await self._save(r)
            await self._emit(r, "approval_required")
        await self._update_twin(device_id)
        return r

    def _execution_deadline(self, r: Remediation, now: datetime) -> datetime:
        ttl = timedelta(seconds=self.policy_for(r.device_id).approval_ttl_s)
        if r.mode == Mode.SCHEDULED and r.scheduled_at:
            return max(now, r.scheduled_at) + ttl
        if r.mode == Mode.MAINTENANCE_WINDOW:
            return now + max(ttl, timedelta(days=1))  # the next window within a day
        return now + ttl

    async def _queue_it(self, r: Remediation, now: datetime, actor: str) -> None:
        r.transition(
            Status.QUEUED,
            now,
            actor,
            "queued",
            mode=r.mode.value,
            scheduled_at=r.scheduled_at.isoformat() if r.scheduled_at else None,
        )
        await self._save(r)
        await self._emit(r, "queued")

    # ------------------------------------------------------------------ human decisions
    async def approve(
        self,
        r: Remediation,
        subject: str,
        role: str,
        note: str | None = None,
        permissions: frozenset[str] | None = None,
        org_role: str | None = None,
    ) -> Remediation:
        async with self._lock:
            now = datetime.now(UTC)
            action = CATALOG[r.action_type]
            if r.status != Status.PENDING_APPROVAL:
                raise RemediationError("not_pending", f"cannot approve: the remediation is {r.status.value}")
            if r.approval_expires_at and now >= r.approval_expires_at:
                await self._finish(
                    r, Status.EXPIRED, now, "system", "expired", reason="approval window elapsed"
                )
                raise RemediationError(
                    "expired", "the approval window has elapsed; a new proposal is required"
                )
            why = can_approve(
                role,
                subject,
                action,
                r.requested_by,
                self.policy_for(r.device_id),
                permissions=permissions,
                org_role=org_role,
            )
            if why:
                r.note(now, subject, "approval_denied", reason=why)
                await self._save(r)
                raise RemediationError("forbidden", why, 403)
            if r.diagnosis_id and self._diagnoses is not None:
                d = await self._diagnoses.repo.get(r.diagnosis_id)
                if d is not None and d.status.value in ("SUPERSEDED", "EXPIRED", "FAILED"):
                    await self._finish(
                        r,
                        Status.EXPIRED,
                        now,
                        "system",
                        "invalidated",
                        reason=f"the diagnosis is {d.status.value.lower()}; re-diagnose first",
                    )
                    raise RemediationError("stale_diagnosis", "the underlying diagnosis is no longer current")
            blocked = self.kill_switch(r.action_type, r.device_id)
            if blocked:
                raise RemediationError("kill_switch", f"remediation is stopped: {blocked}")
            r.transition(Status.APPROVED, now, subject, "approved", note=(note or "")[:300], role=role)
            r.approved_by, r.approval_at = subject, now
            # execution must begin within the approval TTL (from the scheduled start); else a fresh approval
            r.approval_expires_at = self._execution_deadline(r, now)
            REMEDIATION_EVENTS.labels("approved", r.action_type, r.risk_level).inc()
            REMEDIATION_LATENCY.labels("approval").observe((now - r.created_at).total_seconds())
            await self._emit(r, "approved")
            await self._queue_it(r, now, subject)
            await self._update_twin(r.device_id)
            return r

    async def reject(self, r: Remediation, subject: str, role: str, reason: str | None) -> Remediation:
        async with self._lock:
            if role not in ("operator", "admin"):
                raise RemediationError("forbidden", "your role may not reject remediation", 403)
            if r.status != Status.PENDING_APPROVAL:
                raise RemediationError("not_pending", f"cannot reject: the remediation is {r.status.value}")
            r.rejected_by = subject
            await self._finish(
                r, Status.REJECTED, datetime.now(UTC), subject, "rejected", reason=(reason or "")[:300]
            )
            return r

    async def cancel(self, r: Remediation, subject: str, reason: str | None) -> Remediation:
        async with self._lock:
            if r.status in (Status.EXECUTING, Status.VERIFYING):
                raise RemediationError(
                    "in_progress",
                    "the action is already running on the device and cannot be "
                    "interrupted safely; it will finish and be verified",
                )
            if r.status == Status.VALIDATING and r.execution.get("picked_up_at"):
                raise RemediationError("in_progress", "the device has already received the action")
            if not r.is_open:
                raise RemediationError("closed", f"the remediation is already {r.status.value}")
            await self._finish(
                r, Status.CANCELLED, datetime.now(UTC), subject, "cancelled", reason=(reason or "")[:300]
            )
            return r

    # ------------------------------------------------------------------ worker
    async def tick(self, now: datetime) -> None:
        async with self._lock:
            open_ = sorted(self._open(), key=lambda x: x.created_at)
            self._in_flight = sum(1 for x in open_ if x.status in IN_FLIGHT)
            if time.monotonic() - self._last_prune > 3600:  # hourly: bound memory to open + recent records
                self._last_prune = time.monotonic()
                self._prune(now)
            for r in open_:
                try:
                    await self._step(r, now)
                except Exception as exc:
                    log.warning(
                        "remediation_step_failed", remediation_id=r.remediation_id, error=str(exc)[:300]
                    )

    async def _step(self, r: Remediation, now: datetime) -> None:
        action = CATALOG[r.action_type]
        if (
            r.status in (Status.PENDING_APPROVAL, Status.APPROVED, Status.QUEUED)
            and r.approval_expires_at
            and now >= r.approval_expires_at
        ):
            await self._finish(
                r,
                Status.EXPIRED,
                now,
                "system",
                "expired",
                reason="approval expired before the action could start",
            )
            return
        if r.status == Status.QUEUED:
            await self._try_dispatch(r, action, now)
        elif r.status == Status.VALIDATING:
            ex = r.execution
            if not ex.get("picked_up_at") and now >= datetime.fromisoformat(ex["envelope"]["expires_at"]):
                r.transition(
                    Status.QUEUED,
                    now,
                    "system",
                    "envelope_expired",
                    reason="the device did not pick up the action in time; it will be re-issued",
                )
                await self._save(r)
            elif ex.get("picked_up_at") and now >= datetime.fromisoformat(ex["picked_up_at"]) + timedelta(
                seconds=action.validation_timeout_s
            ):
                await self._finish(
                    r,
                    Status.FAILED,
                    now,
                    "system",
                    "validation_timeout",
                    reason="the device did not confirm validation in time; nothing is known to have run",
                )
        elif r.status == Status.EXECUTING:
            started = r.started_at or now
            if now >= started + timedelta(seconds=action.execution_timeout_s + 30):
                await self._finish(
                    r,
                    Status.FAILED,
                    now,
                    "system",
                    "execution_timeout",
                    reason="no result from the device within the execution timeout (outcome unknown)",
                )
        elif r.status == Status.VERIFYING:
            await self._verify(r, action, now)

    def preconditions(self, r: Remediation, action: ActionDefinition, now: datetime) -> list[dict[str, Any]]:
        """Server-side checks. ``wait`` = transient (re-checked every tick), ``fail`` = cannot proceed."""
        out: list[dict[str, Any]] = []
        twin = self._twins.get(r.device_id)
        presence = self._presence.presence_of(r.device_id)
        hb = (self._presence.get(r.device_id).heartbeat or {}) if self._presence.get(r.device_id) else {}

        def add(code: str, ok: bool, detail: str, kind: str = "fail") -> None:
            out.append({"check": code, "ok": ok, "detail": detail, "kind": "ok" if ok else kind})

        add(
            "kill_switch",
            not self.kill_switch(r.action_type, r.device_id),
            self.kill_switch(r.action_type, r.device_id) or "no kill switch is active",
            "wait",
        )
        add("device_online", presence == "ONLINE", f"device is {presence}", "wait")
        agent = (twin.agent_health or {}) if twin else {}
        sync_fail = int(agent.get("sync_failures_consecutive") or 0)
        add("agent_healthy", sync_fail < 5, f"{sync_fail} consecutive sync failures", "wait")
        version = (hb.get("agent_version") or (twin.device.agent_version if twin else None)) or "0"
        add(
            "agent_version",
            version_tuple(version) >= version_tuple(action.min_agent_version),
            f"agent {version}, needs {action.min_agent_version}",
        )
        os_name = ((twin.device.os_name or "") if twin else "").lower()
        add(
            "os_supported",
            any(o in os_name for o in action.supported_os) or not os_name,
            os_name or "unknown OS",
        )
        add(
            "approval_valid",
            r.dry_run or (r.approved_by is not None and (r.approval_expires_at or now) > now),
            f"approved by {r.approved_by}" if r.approved_by else "not approved",
        )
        mode, _, why = self.policy_for(r.device_id).approval(
            action, r.device_id, self._group(r.device_id), r.diagnosis_confidence
        )
        life = self.lifecycle_of(r.device_id)
        if life is not None:  # Phase 9: quarantined / disabled / revoked / retired devices never act
            add("device_lifecycle", life.value == "ACTIVE", f"device lifecycle {life.value}")
        add("policy_allows", r.dry_run or mode != "DISABLED", why)
        busy = [
            x
            for x in self._open(r.device_id)
            if x.status in IN_FLIGHT and x.remediation_id != r.remediation_id
        ]
        add(
            "not_running",
            not busy,
            "another action is running on this device" if busy else "no action in flight",
            "wait",
        )
        last = max(
            (
                x.completed_at or x.failed_at or x.created_at
                for x in self._history(r.device_id, r.action_type, now - timedelta(seconds=action.cooldown_s))
                if x.status in (Status.SUCCEEDED, Status.FAILED, Status.PARTIALLY_SUCCEEDED)
                and x.started_at is not None  # only attempts that actually ran on the device
                and x.remediation_id != r.remediation_id
            ),
            default=None,
        )
        add(
            "cooldown",
            r.dry_run or last is None,
            "cooldown passed"
            if last is None
            else f"last run {last.isoformat()}; cooldown {action.cooldown_s}s",
            "wait",
        )
        today = [
            x
            for x in self._history(r.device_id, r.action_type, now - timedelta(days=1))
            if x.started_at is not None and x.remediation_id != r.remediation_id
        ]
        device_today = [
            x
            for x in self._device_items(r.device_id)
            if x.started_at and x.started_at >= now - timedelta(days=1) and not x.dry_run
        ]
        add(
            "daily_budget",
            r.dry_run
            or (len(today) < action.max_per_day and len(device_today) < self.policy.device_daily_budget),
            f"{len(today)}/{action.max_per_day} today for this action, {len(device_today)}/{self.policy.device_daily_budget} for the device",  # noqa: E501
        )
        add(
            "circuit_closed",
            r.dry_run or not self.circuit_open(r.device_id, r.action_type, now),
            "circuit breaker open after repeated failures"
            if self.circuit_open(r.device_id, r.action_type, now)
            else "closed",
        )
        local = hb.get("remediation_actions")
        if local is not None:  # the device advertises its own allowlist: never dispatch beyond it
            add(
                "device_allows",
                r.action_type in local or r.dry_run,
                "allowed by the device's local policy"
                if r.action_type in local
                else "not allowed by the device's local policy (AGENT_REMEDIATION_ACTIONS)",
            )
        apps_local = hb.get("restartable_applications")
        if "application_id" in r.parameters and apps_local is not None:
            add(
                "device_allows_application",
                r.parameters["application_id"] in apps_local or r.dry_run,
                "on the device's restart allowlist"
                if r.parameters["application_id"] in apps_local
                else "not on the device's restart allowlist (AGENT_RESTARTABLE_APPLICATIONS)",
            )
        if "application_known" in action.preconditions:
            add(
                "application_known",
                r.parameters.get("application_id") in self.policy.applications(),
                "on the approved list"
                if r.parameters.get("application_id") in self.policy.applications()
                else "removed from the approved list",
            )
        if "interactive_session" in action.preconditions:
            run_mode = hb.get("run_mode") or agent.get("run_mode")
            add(
                "interactive_session",
                run_mode != "service",
                f"agent runs as {run_mode or 'unknown'}; restarting a desktop application needs the user session",  # noqa: E501
            )
        if "application_running" in action.preconditions:
            running = self._app_running(r)
            add(
                "application_running",
                running is not False,
                "running"
                if running
                else "unknown (process names not available: the agent checks)"
                if running is None
                else "not running",
                "wait",
            )
        if r.mode == Mode.SCHEDULED and r.scheduled_at:
            add("schedule", now >= r.scheduled_at, f"scheduled for {r.scheduled_at.isoformat()}", "wait")
        if r.mode == Mode.MAINTENANCE_WINDOW:
            inside = self.policy.in_maintenance(self._group(r.device_id), now)
            bypass = action.risk == Risk.CRITICAL and self.policy.critical_bypasses_maintenance
            add(
                "maintenance_window",
                inside or bypass,
                "inside a maintenance window" if inside else "waiting for the next maintenance window",
                "wait",
            )
        if r.approval_at and not r.dry_run and action.risk.rank >= Risk.MEDIUM.rank:
            p = self._presence.get(r.device_id)
            offline_for = (
                (now - p.last_contact_at).total_seconds()
                if p and p.last_contact_at and presence != "ONLINE"
                else 0
            )
            add(
                "fresh_approval",
                offline_for < self.policy.offline_reapproval_s,
                f"device offline for {offline_for:.0f}s since approval",
            )
        return out

    def _app_running(self, r: Remediation) -> bool | None:
        twin = self._twins.get(r.device_id)
        exes = set((r.verification_rules.get("application") or {}).get("executables") or [])
        procs = (twin.processes or {}).get("processes") if twin else None
        if not exes or not procs or not self._s.twin_show_process_names:
            return None
        if any(str(p.get("name", "")).lower() in exes for p in procs):
            return True
        complete = int((twin.processes or {}).get("total_processes") or 0) <= len(procs)
        return False if complete else None  # a top-N snapshot without it does not prove it is not running

    async def _try_dispatch(self, r: Remediation, action: ActionDefinition, now: datetime) -> None:
        if self._in_flight >= self.policy.fleet_max_concurrent:  # cheap check first: nothing can start
            if r.execution.get("waiting_on") != "fleet_capacity":
                r.execution["waiting_on"] = "fleet_capacity"
                r.note(
                    now,
                    "system",
                    "waiting",
                    check="fleet_capacity",
                    detail=f"{self._in_flight} actions in flight",
                )
                await self._save(r)
            return
        checks = self.preconditions(r, action, now)
        failed = [c for c in checks if not c["ok"]]
        fatal = [c for c in failed if c["kind"] == "fail"]
        if fatal:
            reason = "; ".join(f"{c['check']}: {c['detail']}" for c in fatal)
            status = (
                Status.EXPIRED
                if any(c["check"] in ("fresh_approval", "approval_valid") for c in fatal)
                else Status.FAILED
            )
            await self._finish(r, status, now, "system", "precondition_failed", reason=reason, checks=checks)
            return
        if failed:
            first = failed[0]["check"]
            if r.execution.get("waiting_on") != first:
                r.execution["waiting_on"] = first
                r.note(now, "system", "waiting", check=first, detail=failed[0]["detail"])
                REMEDIATION_PRECONDITION_WAITS.labels(first).inc()
                await self._save(r)
                await self._emit(r, "progress")
            return
        in_flight = self._in_flight
        if in_flight >= self.policy.fleet_max_concurrent:
            if r.execution.get("waiting_on") != "fleet_capacity":
                r.execution["waiting_on"] = "fleet_capacity"
                r.note(
                    now, "system", "waiting", check="fleet_capacity", detail=f"{in_flight} actions in flight"
                )
                await self._save(r)
            return
        if self.signer is None:
            await self._finish(
                r,
                Status.FAILED,
                now,
                "system",
                "not_configured",
                reason="no signing key is configured (REMEDIATION_SIGNING_KEY); nothing was sent",
            )
            return
        env = issue(
            self.signer,
            execution_id=r.execution_id,
            remediation_id=r.remediation_id,
            action_id=r.action_type,
            action_version=r.action_version,
            device_id=r.device_id,
            requested_by=r.requested_by,
            approved_by=r.approved_by,
            policy_version=r.policy_version,
            parameters=r.parameters,
            execution_timeout_s=action.execution_timeout_s,
            ttl_s=self.policy.envelope_ttl_s,
            now=now,
            dry_run=r.dry_run,
        )
        r.execution.pop("waiting_on", None)
        r.execution.update(
            {
                "envelope": env.public(),
                "envelope_digest": env.digest(),
                "dispatched_at": now.isoformat(),
                "issues": int(r.execution.get("issues", 0)) + 1,
                "preconditions": checks,
                "picked_up_at": None,
            }
        )
        r.verification["baseline"] = self._target(r)
        r.transition(
            Status.VALIDATING,
            now,
            "system",
            "dispatched",
            envelope_digest=env.digest(),
            expires_at=env.expires_at,
        )
        self.stats["dispatched"] += 1
        self._in_flight += 1
        await self._save(r)
        await self._emit(r, "started" if r.execution["issues"] == 1 else "progress")

    def _target(self, r: Remediation) -> float | None:
        sig = r.verification_rules.get("target_signal")
        twin = self._twins.get(r.device_id)
        if not sig or twin is None:
            return None
        metric = TARGET_KEYS.get(sig)
        key = (
            next((k for k in twin.window.keys() if k == metric or k.startswith(f"{metric}|")), None)  # noqa: SIM118
            if metric
            else None
        )
        if key is None:
            return None
        pts = twin.window.values_since(key, time.time() - 60)
        return round(sum(v for _, v in pts) / len(pts), 2) if pts else None

    # ------------------------------------------------------------------ agent protocol
    def agent_pull(self, device_id: str, now: datetime) -> list[dict[str, Any]]:
        out = []
        for r in self._open(device_id):
            if r.status != Status.VALIDATING:
                continue
            env = r.execution.get("envelope") or {}
            if now >= datetime.fromisoformat(env["expires_at"]):
                continue
            if not r.execution.get("picked_up_at"):
                r.execution["picked_up_at"] = now.isoformat()
                r.note(
                    now, f"agent:{device_id}", "picked_up", envelope_digest=r.execution.get("envelope_digest")
                )
            out.append(env)
        return out

    async def agent_report(
        self, device_id: str, execution_id: str, phase: str, report: dict[str, Any]
    ) -> Remediation:
        async with self._lock:
            now = datetime.now(UTC)
            rid = self._by_execution.get(execution_id)
            r = self.items.get(rid) if rid else None
            if r is None or r.device_id != device_id:
                self.stats["rejected_reports"] += 1
                raise RemediationError("unknown_execution", "unknown execution", 404)
            if phase not in REPORT_PHASES:
                raise RemediationError("invalid_phase", "invalid phase", 422)
            actor = f"agent:{device_id}"
            detail = str(report.get("detail") or "")[:500]
            data = {
                k: v for k, v in (report.get("data") or {}).items() if isinstance(v, (str, int, float, bool))
            }  # flat only
            if not r.is_open or r.execution.get("report", {}).get("phase") in (
                "completed",
                "failed",
                "rejected",
            ):
                r.note(now, actor, "duplicate_report", phase=phase)  # idempotent: first final report wins
                await self._save(r)
                return r
            if phase == "accepted" and r.status == Status.VALIDATING:
                r.started_at = now
                r.transition(Status.EXECUTING, now, actor, "started", detail=detail)
                await self._save(r)
                await self._emit(r, "started")
                return r
            r.execution["report"] = {"phase": phase, "detail": detail, "data": data, "at": now.isoformat()}
            if phase == "rejected":
                await self._finish(
                    r, Status.FAILED, now, actor, "rejected_by_agent", reason=f"the device refused: {detail}"
                )
            elif phase == "failed":
                if r.status == Status.VALIDATING:
                    r.started_at = r.started_at or now
                    r.transition(Status.EXECUTING, now, actor, "started")
                await self._finish(
                    r, Status.FAILED, now, actor, "failed_on_device", reason=detail or "the action failed"
                )
            else:  # completed
                if r.status == Status.VALIDATING:
                    r.started_at = r.started_at or now
                    r.transition(Status.EXECUTING, now, actor, "started")
                r.verification["started_at"] = now.isoformat()
                r.transition(Status.VERIFYING, now, actor, "completed_on_device", detail=detail)
                REMEDIATION_LATENCY.labels("execution").observe((now - (r.started_at or now)).total_seconds())
                await self._save(r)
                await self._emit(r, "verifying")
                await self._verify(r, CATALOG[r.action_type], now)
            return r

    async def _verify(self, r: Remediation, action: ActionDefinition, now: datetime) -> None:
        if r.dry_run:
            r.verification.update(
                {"outcome": "DRY_RUN", "checks": [], "note": "dry run: nothing was changed"}
            )
            await self._finish(r, Status.SUCCEEDED, now, "system", "verified", outcome="dry run completed")
            return
        rep = r.execution.get("report") or {}
        completed_at = datetime.fromisoformat(rep["at"]) if rep.get("at") else now
        p = self._presence.get(r.device_id)
        fresh = bool(p and p.last_batch_at and p.last_batch_at > completed_at)
        running = self._app_running(r)
        if running is None and "application_running" in action.verification.checks:
            running = rep.get("data", {}).get("running_after") if isinstance(rep.get("data"), dict) else None
        started = datetime.fromisoformat(r.verification.get("started_at") or now.isoformat())
        obs = verification.Observation(
            agent_outcome=rep.get("phase")
            if rep.get("phase") in ("completed", "failed", "rejected")
            else None,
            agent_detail=rep.get("detail"),
            fresh_after_completion=fresh,
            application_running=running,
            target_before=r.verification.get("baseline"),
            target_now=self._target(r),
            elapsed_s=(now - started).total_seconds(),
        )
        outcome, checks = verification.evaluate(action, obs)
        r.verification["checks"] = checks
        r.verification["after"] = obs.target_now
        if outcome is None:
            return
        r.verification["outcome"] = outcome
        REMEDIATION_LATENCY.labels("verification").observe(obs.elapsed_s)
        await self._finish(
            r,
            Status(outcome),
            now,
            "system",
            "verified",
            checks=checks,
            before=obs.target_before,
            after=obs.target_now,
            reason=None
            if outcome != "FAILED"
            else "; ".join(c["detail"] for c in checks if c["state"] == "FAIL"),
        )

    async def _finish(
        self, r: Remediation, to: Status, now: datetime, actor: str, action: str, **detail: Any
    ) -> None:
        r.transition(to, now, actor, action, **{k: v for k, v in detail.items() if v is not None})
        if r.result and not r.dry_run:
            o = self._outcomes.setdefault(r.action_type, [0, 0])
            o[0] += r.result == "SUCCEEDED"
            o[1] += 1
        REMEDIATION_EVENTS.labels(to.value.lower(), r.action_type, r.risk_level).inc()
        if to == Status.FAILED and not r.dry_run:
            fails = [
                x
                for x in self._history(
                    r.device_id, r.action_type, now - timedelta(seconds=self.policy.circuit_window_s)
                )
                if x.status == Status.FAILED
            ]
            if len(fails) >= self.policy.circuit_failures and not self.circuit_open(
                r.device_id, r.action_type, now
            ):
                self.circuits[(r.device_id, r.action_type)] = now + timedelta(
                    seconds=self.policy.circuit_open_s
                )
                r.note(
                    now,
                    "system",
                    "circuit_opened",
                    failures=len(fails),
                    until=self.circuits[(r.device_id, r.action_type)].isoformat(),
                )
                REMEDIATION_CIRCUIT_OPEN.labels(r.action_type).inc()
                log.warning(
                    "remediation_circuit_open",
                    device_id=r.device_id,
                    action=r.action_type,
                    failures=len(fails),
                )
                await self._emit(r, "circuit_open")
        await self._save(r)
        kind = {
            Status.SUCCEEDED: "succeeded",
            Status.PARTIALLY_SUCCEEDED: "succeeded",
            Status.FAILED: "failed",
            Status.CANCELLED: "cancelled",
            Status.REJECTED: "rejected",
            Status.EXPIRED: "expired",
            Status.ROLLED_BACK: "rolled_back",
        }.get(to, "progress")
        await self._emit(r, kind)
        await self._timeline(r, kind)
        await self._update_twin(r.device_id)

    # ------------------------------------------------------------------ outputs
    async def _save(self, r: Remediation) -> None:
        try:
            await self.repo.save(r)
        except Exception as exc:
            log.warning("remediation_persist_failed", remediation_id=r.remediation_id, error=str(exc)[:200])

    async def _emit(self, r: Remediation, kind: str) -> None:
        await self._publish(
            [RemediationChanged(device_id=r.device_id, kind=kind, remediation=r.to_dict(full=False))]
        )

    async def _timeline(self, r: Remediation, kind: str) -> None:
        if kind not in ("succeeded", "failed", "rejected", "expired", "cancelled"):
            return
        msg = {
            "succeeded": f"Remediation {r.status.value.lower().replace('_', ' ')}: {r.action_name}",
            "failed": f"Remediation failed: {r.action_name} ({(r.failure_reason or '')[:120]})",
            "rejected": f"Remediation rejected by {r.rejected_by}: {r.action_name}",
            "expired": f"Remediation expired: {r.action_name}",
            "cancelled": f"Remediation cancelled: {r.action_name}",
        }[kind]
        sev = "warning" if kind == "failed" else "info"
        data = {"remediation_id": r.remediation_id, "action": r.action_type, "status": r.status.value}
        with contextlib.suppress(Exception):
            self._record(
                SystemEventRecord(r.device_id, r.updated_at, f"remediation.{kind}", sev, msg[:300], data)
            )
        await self._publish(
            [
                TwinMessage(
                    device_id=r.device_id,
                    kind="twin.event.created",
                    body={
                        "timeline_event": {
                            "event_id": f"remediation:{r.remediation_id}:{kind}",
                            "device_id": r.device_id,
                            "type": f"remediation.{kind}",
                            "severity": sev,
                            "timestamp": r.updated_at.isoformat(),
                            "message": msg[:300],
                            "data": data,
                        }
                    },
                )
            ]
        )

    async def _update_twin(self, device_id: str) -> None:
        twin = self._twins.get(device_id)
        if twin is None:
            return
        rows = sorted(
            (r for r in self._device_items(device_id) if not r.dry_run),
            key=lambda r: r.updated_at,
            reverse=True,
        )
        latest = rows[0] if rows else None
        pending = sum(1 for r in rows if r.status == Status.PENDING_APPROVAL)
        status = "NONE"
        if latest is not None:
            status = {
                "PROPOSED": "PROPOSED",
                "PENDING_APPROVAL": "PENDING_APPROVAL",
                "APPROVED": "PENDING_APPROVAL",
                "QUEUED": "EXECUTING",
                "VALIDATING": "EXECUTING",
                "EXECUTING": "EXECUTING",
                "VERIFYING": "VERIFYING",
                "SUCCEEDED": "SUCCEEDED",
                "PARTIALLY_SUCCEEDED": "SUCCEEDED",
                "FAILED": "FAILED",
            }.get(latest.status.value, "NONE")
        twin.remediation = {
            "status": status,
            "pending_count": pending,
            "latest": latest.to_dict(full=False) if latest else None,
        }
        if self._on_twin_changed is not None:
            with contextlib.suppress(Exception):
                await self._on_twin_changed(device_id)

    # ------------------------------------------------------------------ policy / read side
    async def set_policy(self, changes: dict[str, Any], by: str) -> RemediationPolicy:
        merged = {
            **self.policy.public(),
            **changes,
            "version": self.policy.version + 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "updated_by": by,
        }
        policy = RemediationPolicy.from_dict(merged)
        await self._store.set_setting(POLICY_KEY, policy.public(), by)
        self.policy = policy
        log.info("remediation_policy_changed", version=policy.version, by=by, keys=sorted(changes))
        return policy

    def dry_run_plan(self, r: Remediation) -> dict[str, Any]:
        """Server-side dry run: what would happen, with live precondition results. Changes nothing."""
        action = CATALOG[r.action_type]
        now = datetime.now(UTC)
        checks = self.preconditions(r, action, now)
        return {
            "dry_run": True,
            "remediation_id": r.remediation_id,
            "device_id": r.device_id,
            "action": action.public(),
            "parameters": r.parameters,
            "expected_effect": action.impact,
            "estimated_duration_s": action.estimated_duration_s,
            "preconditions": checks,
            "would_execute_now": all(c["ok"] or c["check"] == "approval_valid" for c in checks),
            "verification": r.verification_rules,
            "rollback": action.rollback,
            "note": "No changes were made. Approval, signing and the device's own checks still apply.",
        }

    def status(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for r in self.items.values():
            by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
        now = datetime.now(UTC)
        return {
            "signing_configured": self.signer is not None,
            "public_key": self.signer.public_key_b64 if self.signer else None,
            "key_id": self.signer.key_id if self.signer else None,
            "kill_switch_env": bool(getattr(self._s, "remediation_kill_switch", False)),
            "auto_remediation_enabled": self.policy.auto_remediation_enabled,
            "policy_version": self.policy.version,
            "in_flight": sum(1 for r in self.items.values() if r.status in IN_FLIGHT),
            "fleet_max_concurrent": self.policy.fleet_max_concurrent,
            "by_status": by_status,
            "open_circuits": [
                {"device_id": d, "action": a, "until": u.isoformat()}
                for (d, a), u in self.circuits.items()
                if u > now
            ],
            "stats": dict(self.stats),
        }
