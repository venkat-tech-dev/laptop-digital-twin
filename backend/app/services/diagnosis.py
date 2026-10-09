"""DiagnosisService (Phase 7): explain alerts, anomalies and predictions from evidence.

    trigger (HIGH/CRITICAL alert, or an explicit request)
      -> bounded job queue (back-pressure, de-duplicated, per-trigger cooldown)
      -> DiagnosticContext (twin + 60 min of 1-minute history + Phase 4 baselines/anomalies + Phase 5
         predictions + Phase 6 alerts + process snapshots + security findings + agent events;
         minimised and sanitised)
      -> fingerprint cache -> evidence -> rule hypotheses -> platform confidence
      -> optional local model (resource gate, timeout, cancellation, cooldown) -> validation
      -> versioned diagnosis (previous version SUPERSEDED, never overwritten)
      -> diagnosis.* events, twin "diagnoses.*", timeline entry, metrics

It reads; it never acts. No code path here can change the endpoint (no commands, no process
control, no configuration changes) and model output is text that is validated before display.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.core.metrics import (
    DIAGNOSES_TOTAL,
    DIAGNOSIS_CACHE_HITS,
    DIAGNOSIS_DROPPED,
    DIAGNOSIS_LATENCY,
    DIAGNOSIS_MODEL_FAILURES,
    DIAGNOSIS_QUEUE,
    DIAGNOSIS_REJECTED_CLAIMS,
)
from app.core.supervisor import watch
from app.domain.diagnosis import composer, prompts, validation
from app.domain.diagnosis.context import DiagnosticContext, ProcessFigure, SeriesSummary, clean
from app.domain.diagnosis.evidence import temporal
from app.domain.diagnosis.models import Diagnosis, DiagnosisStatus, DiagnosisType
from app.domain.events.events import AlertChanged, DiagnosisChanged, DomainEvent, TwinMessage
from app.domain.twin.projection import project_fields
from app.repositories.base import SystemEventRecord
from app.repositories.diagnoses import DiagnosisFilter, DiagnosisRepository
from app.services.diagnosis_providers import DiagnosisModel, ModelUnavailableError, RuleBasedProvider
from app.services.insights import attribute_processes

log = structlog.get_logger("diagnosis")

WINDOW_S = 3600
PROCESS_WINDOW_S = 15 * 60
#: practical margin over the early-window median while a device has no learned baseline yet
WINDOW_MARGIN = {"cpu": 25.0, "memory": 10.0, "temperature": 10.0, "disk_active": 30.0, "net_latency": 50.0,
                 "gpu": 30.0, "disk_usage": 5.0, "packet_loss": 3.0}  # fmt: skip
SEVERITY_ORDER = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
FEEDBACK_VERDICTS = ("HELPFUL", "NOT_HELPFUL", "CORRECT", "INCORRECT", "PARTIALLY_CORRECT")
TRIGGER_KINDS = ("alert", "anomaly", "prediction")

#: signal -> (twin field, label, unit, warning, critical, direction)
SERIES: dict[str, tuple[str, str, str, float | None, float | None, str]] = {
    "cpu": ("performance.cpu.usage_percent", "CPU", "%", 90, 95, "up"),
    "memory": ("performance.memory.usage_percent", "Memory", "%", 90, 95, "up"),
    "temperature": ("thermal.temperature_c", "Temperature", "°C", 90, 98, "up"),
    "disk_active": ("performance.disk.active_time_percent", "Drive activity", "%", 90, 98, "up"),
    "disk_usage": ("performance.disk.usage_percent", "Drive space used", "%", 90, 95, "up"),
    "net_latency": ("network.gateway_latency_ms", "Gateway latency", "ms", 150, 500, "up"),
    "packet_loss": ("network.packet_loss_percent", "Packet loss", "%", 5, 20, "up"),
    "battery": ("battery.charge_percent", "Battery charge", "%", 20, 10, "down"),
    "gpu": ("performance.gpu.usage_percent", "GPU", "%", 90, 95, "up"),
}
PREDICTION_SIGNAL = {
    "disk": "disk_usage",
    "memory": "memory",
    "battery": "battery",
    "temperature": "temperature",
    "cpu": "cpu",
}
METRIC_PREFIX_SIGNAL = (
    ("cpu.usage", "cpu"),
    ("cpu.temperature", "temperature"),
    ("thermal", "temperature"),
    ("memory", "memory"),
    ("disk.active", "disk_active"),
    ("disk.usage", "disk_usage"),
    ("disk.free", "disk_usage"),
    ("disk", "disk_active"),
    ("network.gateway_latency", "net_latency"),
    ("network.packet_loss", "packet_loss"),
    ("network", "net_latency"),
    ("battery", "battery"),
    ("gpu", "gpu"),
)


def signal_for(metric: str | None, signal_id: str | None = None, target: str | None = None) -> str | None:
    if target:
        return PREDICTION_SIGNAL.get(target, target)
    if signal_id:
        return {"disk_write": "disk_active", "disk_read": "disk_active"}.get(signal_id, signal_id)
    m = (metric or "").lower()
    for prefix, sig in METRIC_PREFIX_SIGNAL:
        if prefix in m:
            return sig
    return None


class QueueFullError(RuntimeError):
    pass


class TriggerNotFoundError(LookupError):
    pass


@dataclass
class Job:
    job_id: str
    device_id: str
    trigger_kind: str
    trigger_id: str | None
    requested_by: str
    force: bool = False
    status: str = "QUEUED"  # QUEUED | RUNNING | DONE | FAILED | CACHED | CANCELLED
    diagnosis_id: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "device_id": self.device_id,
            "trigger_kind": self.trigger_kind,
            "trigger_id": self.trigger_id,
            "status": self.status,
            "diagnosis_id": self.diagnosis_id,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class DiagnosisService:
    def __init__(
        self,
        settings: Any,
        twins: Any,
        twin_state: Any,
        telemetry_repo: Any,
        event_repo: Any,
        repo: DiagnosisRepository,
        process_history: Any,
        publish: Callable[[list[DomainEvent]], Awaitable[None]],
        record: Callable[[SystemEventRecord], None],
        provider: DiagnosisModel | None = None,
        on_twin_changed: Callable[[str], Awaitable[Any]] | None = None,
        intelligence: Any = None,
        forecasts: Any = None,
        alerts: Any = None,
        tenant_id: str = "default",
    ) -> None:
        self._s = settings
        self.timeout_s = float(settings.diagnosis_timeout_s)
        self.memory_gate = float(settings.diagnosis_memory_gate_percent)
        self._twins = twins
        self._twin_state = twin_state
        self._telemetry = telemetry_repo
        self._events = event_repo
        self.repo = repo
        self._procs = process_history
        self._publish = publish
        self._record = record
        self.provider: DiagnosisModel = provider or RuleBasedProvider()
        if settings.diagnosis_model_host_device_id and hasattr(self.provider, "set_memory_probe"):
            # the model host is an enrolled device: judge its free memory from its own telemetry
            self.provider.set_memory_probe(self._host_memory_available_mb)
        self._on_twin_changed = on_twin_changed
        self.intelligence = intelligence
        self.forecasts = forecasts
        self.alerts = alerts
        self.tenant_id = tenant_id
        # Phase 9 hooks (set by the container): device -> organisation, effective policy value, quota check
        self.tenant_of: Callable[[str], str] = lambda _d: self.tenant_id
        self.policy_value: Callable[..., Any] | None = None
        self.quota: Callable[[str], bool] = lambda _d: True
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=settings.diagnosis_queue_max)
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self._pending: dict[tuple[str, str, str | None], Job] = {}
        self._last_run: dict[tuple[str, str | None], float] = {}
        self._running = False
        self._active: dict[str, asyncio.Task[Any]] = {}
        self.stats: dict[str, Any] = {
            "requested": 0,
            "completed": 0,
            "failed": 0,
            "cached": 0,
            "dropped": 0,
            "cooldown": 0,
            "model_used": 0,
            "model_failed": 0,
            "model_skipped_resource": 0,
            "rejected_claims": 0,
            "ms_total": 0.0,
        }

    # ------------------------------------------------------------------ triggers
    async def on_event(self, event: DomainEvent) -> None:
        """EventBus subscriber: HIGH/CRITICAL alerts are diagnosed automatically (enqueue only)."""
        if not isinstance(event, AlertChanged) or event.kind not in ("created", "escalated"):
            return
        floor = self._s.diagnosis_auto_min_severity
        if self.policy_value is not None:  # organisation / group / device policy (Phase 9)
            if not self.policy_value("diagnosis", "enabled", event.device_id):
                return
            floor = self.policy_value("diagnosis", "auto_min_severity", event.device_id)
        if not self.quota(event.device_id):
            return  # organisation over its diagnosis quota: alerting continues, diagnosis waits
        sev = str(event.alert.get("severity") or "")
        if (
            floor == "OFF"
            or sev not in SEVERITY_ORDER
            or SEVERITY_ORDER.index(sev) < SEVERITY_ORDER.index(floor)
        ):
            return
        with contextlib.suppress(QueueFullError):  # counted in request(); alerting is never blocked
            self.request("alert", str(event.alert.get("alert_id")), event.device_id, "system")

    def request(self, kind: str, trigger_id: str | None, device_id: str, by: str, force: bool = False) -> Job:
        """Enqueue a diagnosis job (never runs inline). Duplicate requests share the pending job."""
        if kind not in (*TRIGGER_KINDS, "manual"):
            raise ValueError("unknown trigger kind")
        self.stats["requested"] += 1
        key = (device_id, kind, trigger_id)
        pending = self._pending.get(key)
        if pending is not None:
            return pending
        mono = time.monotonic()
        last = self._last_run.get((device_id, trigger_id))
        if not force and by == "system" and last is not None and mono - last < self._s.diagnosis_cooldown_s:
            self.stats["cooldown"] += 1
            DIAGNOSIS_DROPPED.labels("cooldown").inc()
            job = Job(uuid.uuid4().hex, device_id, kind, trigger_id, by, status="CANCELLED", error="cooldown")
            return self._remember(job)
        job = Job(uuid.uuid4().hex, device_id, kind, trigger_id, by, force)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull as exc:
            self.stats["dropped"] += 1
            DIAGNOSIS_DROPPED.labels("queue_full").inc()
            raise QueueFullError("diagnosis queue is full, try again later") from exc
        DIAGNOSIS_QUEUE.set(self._queue.qsize())
        self._pending[key] = job
        return self._remember(job)

    def _remember(self, job: Job) -> Job:
        self.jobs[job.job_id] = job
        while len(self.jobs) > 1000:
            self.jobs.popitem(last=False)
        return job

    # ------------------------------------------------------------------ workers
    async def run(self, stop: asyncio.Event) -> None:
        self._running = True
        tasks = [
            asyncio.create_task(self._worker(stop), name=f"diagnosis_worker_{i}")
            for i in range(self._s.diagnosis_concurrency)
        ]
        tasks.append(asyncio.create_task(self._maintenance(stop), name="diagnosis_maintenance"))
        try:
            await watch(tasks, stop)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            job = await self._queue.get()
            DIAGNOSIS_QUEUE.set(self._queue.qsize())
            await self.process(job)

    async def drain(self) -> None:
        """Process every queued job inline (tests / tools without the background workers)."""
        while not self._queue.empty():
            await self.process(self._queue.get_nowait())

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        task = self._active.get(job_id)
        if task is not None:
            task.cancel()
            return True
        if job.status == "QUEUED":
            job.status = "CANCELLED"
            self._pending.pop((job.device_id, job.trigger_kind, job.trigger_id), None)
            return True
        return False

    async def process(self, job: Job) -> Diagnosis | None:
        if job.status == "CANCELLED":
            return None
        job.status = "RUNNING"
        task = asyncio.current_task()
        if task is not None:
            self._active[job.job_id] = task
        try:
            d = await self._diagnose(job)
            job.diagnosis_id = d.diagnosis_id if d else None
            job.status = "CACHED" if d is not None and job.status == "CACHED" else "DONE"
            return d
        except asyncio.CancelledError:
            job.status, job.error = "CANCELLED", "cancelled"
            if task is not None and task.cancelling():
                raise
            return None
        except TriggerNotFoundError as exc:
            job.status, job.error = "FAILED", str(exc)
            return None
        except Exception as exc:
            job.status, job.error = "FAILED", type(exc).__name__
            self.stats["failed"] += 1
            log.warning("diagnosis_failed", device_id=job.device_id, error=str(exc)[:300])
            return None
        finally:
            job.finished_at = datetime.now(UTC)
            self._active.pop(job.job_id, None)
            self._pending.pop((job.device_id, job.trigger_kind, job.trigger_id), None)
            self._last_run[(job.device_id, job.trigger_id)] = time.monotonic()

    # ------------------------------------------------------------------ pipeline
    async def _diagnose(self, job: Job) -> Diagnosis | None:
        started = time.perf_counter()
        now = datetime.now(UTC)
        trigger = await self.resolve_trigger(job.trigger_kind, job.trigger_id, job.device_id)
        timings: dict[str, float] = {}
        t0 = time.perf_counter()
        ctx = await self.build_context(job.device_id, trigger, now)
        timings["context_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        fp = ctx.fingerprint()
        if not job.force:
            cached = await self.repo.by_fingerprint(
                job.device_id, fp, now - timedelta(seconds=self._s.diagnosis_ttl_s)
            )
            if cached is not None and cached.trigger_id == job.trigger_id:
                self.stats["cached"] += 1
                DIAGNOSIS_CACHE_HITS.inc()
                job.status = "CACHED"
                return cached
        prev = await self._previous(job)
        d = Diagnosis(
            diagnosis_id=uuid.uuid4().hex,
            series_id=prev.series_id if prev else uuid.uuid4().hex,
            version=(prev.version + 1) if prev else 1,
            tenant_id=self.tenant_of(job.device_id),
            device_id=job.device_id,
            trigger_kind=job.trigger_kind,
            trigger_id=job.trigger_id,
            alert_id=trigger.get("alert_id"),
            anomaly_id=trigger.get("anomaly_id"),
            prediction_id=trigger.get("prediction_id"),
            status=DiagnosisStatus.GENERATING,
            diagnosis_type=DiagnosisType.UNKNOWN,
            category=str(trigger.get("signal") or "unknown"),
            severity=trigger.get("severity"),
            summary="Diagnosis in progress.",
            likely_cause=None,
            confidence=0.0,
            confidence_level="INSUFFICIENT",
            hypotheses=[],
            evidence=[],
            explanation={},
            related_processes=[],
            related_events=[],
            reasoning_model="rules",
            model_version="rules-1",
            prompt_version=None,
            context_fingerprint=fp,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self._s.diagnosis_ttl_s),
            supersedes=prev.diagnosis_id if prev else None,
        )
        await self._save(d)
        await self._emit(d, "started")
        try:
            t0 = time.perf_counter()
            draft = composer.prepare(ctx)
            timings["rules_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            model_out, reasoning, model_version, notices = None, "rules", "rules-1", []
            use_model, why = self._model_allowed(ctx)
            if use_model:
                # progressive: the deterministic result is visible while the model works
                self._apply(d, composer.finish(ctx, composer.prepare(ctx), None, "rules"), "rules", "rules-1")
                d.notices = ["AI explanation is being generated; showing deterministic evidence."]
                await self._save(d)
                await self._emit(d, "updated")
                t0 = time.perf_counter()
                try:
                    res = await asyncio.wait_for(
                        self.provider.diagnose(ctx, draft.index, draft.hypotheses),
                        self.timeout_s,
                    )
                    timings["model_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                    if res is not None:
                        names = [p.name for p in ctx.processes]
                        model_out = validation.validate(
                            res.raw, draft.index, [h.code for h in draft.hypotheses], names
                        )
                        reasoning = f"{self.provider.name}:{res.model}"
                        model_version = res.model_version
                        self.stats["model_used"] += 1
                        if model_out.summary is None and not model_out.claims and model_out.rejected:
                            notices.append("AI output failed validation. Showing deterministic evidence.")
                            DIAGNOSIS_MODEL_FAILURES.labels("invalid_output").inc()
                except TimeoutError:
                    self._model_failed("timeout")
                    notices.append(composer.FALLBACK_NOTICE)
                    if hasattr(self.provider, "record_timeout"):
                        self.provider.record_timeout()
                except ModelUnavailableError as exc:
                    self._model_failed("unavailable")
                    notices.append(composer.FALLBACK_NOTICE)
                    log.info("diagnosis_model_unavailable", reason=str(exc)[:160])
            elif self.provider.uses_model:
                self.stats["model_skipped_resource"] += 1
                notices.append(f"{composer.FALLBACK_NOTICE} ({why})")
            else:
                notices.append(composer.FALLBACK_NOTICE)
            final = composer.finish(ctx, draft, model_out, reasoning)
            self._apply(d, final, reasoning, model_version)
            d.prompt_version = prompts.PROMPT_VERSION if model_out is not None else None
            d.notices = notices
            for r in final.rejected_claims:
                DIAGNOSIS_REJECTED_CLAIMS.labels(str(r.get("reason"))).inc()
            self.stats["rejected_claims"] += len(final.rejected_claims)
        except Exception as exc:
            d.status = DiagnosisStatus.FAILED
            d.summary = "Diagnosis could not be completed."
            d.notices = [f"Diagnosis failed ({type(exc).__name__}); the alert and evidence remain available."]
            log.warning("diagnosis_pipeline_failed", device_id=d.device_id, error=str(exc)[:300])
        total = time.perf_counter() - started
        timings["total_ms"] = round(total * 1000, 1)
        d.timings = timings
        d.updated_at = datetime.now(UTC)
        await self._save(d)
        if prev is not None and d.status is not DiagnosisStatus.FAILED:
            prev.status = DiagnosisStatus.SUPERSEDED
            prev.updated_at = d.updated_at
            await self._save(prev)
        kind = "failed" if d.status is DiagnosisStatus.FAILED else "available"
        await self._emit(d, kind)
        await self._timeline(d, kind)
        await self._update_twin(d.device_id)
        reason_label = d.reasoning_model.split(":")[0]
        DIAGNOSES_TOTAL.labels(d.status.value, reason_label).inc()
        DIAGNOSIS_LATENCY.labels(reason_label).observe(total)
        self.stats["completed"] += 1
        self.stats["ms_total"] += total * 1000
        log.info(
            "diagnosis_" + kind,
            device_id=d.device_id,
            diagnosis_id=d.diagnosis_id,
            version=d.version,
            type=d.diagnosis_type.value,
            confidence=d.confidence,
            reasoning=d.reasoning_model,
            ms=timings["total_ms"],
        )
        return d

    @staticmethod
    def _apply(d: Diagnosis, draft: composer.Draft, reasoning: str, model_version: str) -> None:
        d.status = draft.status
        d.diagnosis_type = draft.diagnosis_type
        d.summary = draft.summary
        d.likely_cause = draft.likely_cause
        d.confidence = draft.confidence
        d.confidence_level = draft.confidence_level
        d.hypotheses = draft.hypotheses
        d.evidence = draft.evidence
        d.explanation = draft.explanation
        d.related_processes = draft.related_processes
        d.related_events = draft.related_events
        d.rejected_claims = draft.rejected_claims
        d.reasoning_model = reasoning
        d.model_version = model_version
        if d.diagnosis_type is not DiagnosisType.UNKNOWN:
            d.category = d.diagnosis_type.value.lower()

    def _model_failed(self, reason: str) -> None:
        self.stats["model_failed"] += 1
        DIAGNOSIS_MODEL_FAILURES.labels(reason).inc()

    def _host_memory(self) -> tuple[float | None, float | None]:
        """(memory in use %, total bytes) of the model host device from its live twin state."""
        host = self._s.diagnosis_model_host_device_id
        engine = getattr(self._twin_state, "engine", None)
        doc = engine.docs.get(host) if host and engine is not None else None
        state = doc.state if doc is not None else {}

        def value(key: str) -> float | None:
            v = (state.get(key) or {}).get("value")
            return float(v) if isinstance(v, (int, float)) else None

        return value("performance.memory.usage_percent"), value("performance.memory.total_bytes")

    def _host_memory_available_mb(self) -> float | None:
        """Free memory of the model host; unknown counts as none (fail closed: no model)."""
        pct, total = self._host_memory()
        if pct is None or total is None:
            return 0.0
        return total * max(0.0, 100.0 - pct) / 100.0 / 1024**2

    def _model_allowed(self, ctx: DiagnosticContext) -> tuple[bool, str]:
        """Resource-aware: no inference while the device hosting the model is short of memory."""
        if not self.provider.uses_model:
            return False, "rules only"
        host = self._s.diagnosis_model_host_device_id
        if host is None or host == ctx.device_id:
            mem = ctx.series.get("memory")
            cur = mem.current if mem else None
        else:
            cur = self._host_memory()[0]
            if cur is None:
                return False, "memory of the model host is unknown; AI reasoning deferred"
        if cur is not None and cur >= self.memory_gate:
            return False, f"memory at {cur:.0f}% on the model host; AI reasoning deferred"
        return True, "ok"

    async def _previous(self, job: Job) -> Diagnosis | None:
        f = {"alert": "alert_id", "anomaly": "anomaly_id", "prediction": "prediction_id"}.get(
            job.trigger_kind
        )
        if f is None or job.trigger_id is None:
            return None
        flt = DiagnosisFilter(
            current_only=True,
            alert_id=job.trigger_id if f == "alert_id" else None,
            anomaly_id=job.trigger_id if f == "anomaly_id" else None,
            prediction_id=job.trigger_id if f == "prediction_id" else None,
        )
        rows = await self.repo.search(job.device_id, flt, 1)
        return rows[0] if rows else None

    async def _save(self, d: Diagnosis) -> None:
        try:
            await self.repo.save(d)
        except Exception as exc:
            log.warning("diagnosis_persist_failed", diagnosis_id=d.diagnosis_id, error=str(exc)[:200])

    async def _emit(self, d: Diagnosis, kind: str) -> None:
        await self._publish(
            [DiagnosisChanged(device_id=d.device_id, kind=kind, diagnosis=d.to_dict(full=False))]
        )

    async def _timeline(self, d: Diagnosis, kind: str) -> None:
        if kind == "available":
            msg = f"Diagnosis ({d.confidence_level.lower()} confidence): {d.likely_cause or d.summary}"
        elif kind == "expired":
            msg = f"Diagnosis expired: {d.likely_cause or d.summary}"
        else:
            msg = "Diagnosis could not be completed"
        sev = "warning" if kind == "available" and d.confidence_level in ("HIGH", "MEDIUM") else "info"
        now = datetime.now(UTC)
        data = {"diagnosis_id": d.diagnosis_id, "type": d.diagnosis_type.value, "alert_id": d.alert_id}
        with contextlib.suppress(Exception):
            self._record(SystemEventRecord(d.device_id, now, f"diagnosis.{kind}", sev, msg[:300], data))
        await self._publish(
            [
                TwinMessage(
                    device_id=d.device_id,
                    kind="twin.event.created",
                    body={
                        "timeline_event": {
                            "event_id": f"diagnosis:{d.diagnosis_id}:{kind}",
                            "device_id": d.device_id,
                            "type": f"diagnosis.{kind}",
                            "severity": sev,
                            "timestamp": now.isoformat(),
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
        try:
            rows = await self.repo.search(device_id, DiagnosisFilter(current_only=True), 20)
        except Exception:
            return
        live = [r for r in rows if r.status.value in ("AVAILABLE", "LOW_CONFIDENCE", "INSUFFICIENT_EVIDENCE")]
        latest = live[0] if live else None
        twin.diagnoses = {
            "active_count": len(live),
            "latest": latest.to_dict(full=False) if latest else None,
            "generating": any(r.status is DiagnosisStatus.GENERATING for r in rows),
        }
        if self._on_twin_changed is not None:
            with contextlib.suppress(Exception):
                await self._on_twin_changed(device_id)

    # ------------------------------------------------------------------ maintenance
    async def _maintenance(self, stop: asyncio.Event) -> None:
        last_purge = 0.0
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 60)
            try:
                await self.expire_due(datetime.now(UTC))
                # cooldown memory: one entry per (device, trigger) ever diagnosed; keep only live cooldowns
                horizon = time.monotonic() - self._s.diagnosis_cooldown_s
                for key in [k for k, t in self._last_run.items() if t < horizon]:
                    del self._last_run[key]
                if time.monotonic() - last_purge > 86400:
                    last_purge = time.monotonic()
                    cutoff = datetime.now(UTC) - timedelta(days=self._s.diagnosis_retention_days)
                    await self.repo.purge_older_than(cutoff)
            except Exception as exc:
                log.warning("diagnosis_maintenance_failed", error=str(exc)[:200])

    async def expire_due(self, now: datetime) -> int:
        rows = await self.repo.due_expiry(now)
        devices = set()
        for d in rows:
            d.status = DiagnosisStatus.EXPIRED
            d.updated_at = now
            await self._save(d)
            await self._emit(d, "expired")
            devices.add(d.device_id)
        for dev in devices:
            await self._update_twin(dev)
        return len(rows)

    # ------------------------------------------------------------------ triggers -> context
    async def _alert(self, alert_id: str) -> Any:
        if self.alerts is None:
            return None
        a = next((x for x in self.alerts.engine.open_alerts() if x.alert_id == alert_id), None)
        return a if a is not None else await self.alerts.repo.get_alert(alert_id)

    async def trigger_device(self, kind: str, trigger_id: str) -> str | None:
        """Device of an alert / anomaly / prediction id (for server-side authorization), or None."""
        if kind == "alert":
            a = await self._alert(trigger_id)
            return a.device_id if a is not None else None
        if kind == "anomaly":
            an = self.intelligence.find_active(trigger_id) if self.intelligence is not None else None
            if an is None:
                with contextlib.suppress(Exception):
                    an = await self._events.get_anomaly(trigger_id)
            return an.device_id if an is not None else None
        if kind == "prediction" and self.forecasts is not None:
            p = self.forecasts.find_active(trigger_id) or await self.forecasts.repo.get(trigger_id)
            return p.device_id if p is not None else None
        return None

    async def resolve_trigger(self, kind: str, trigger_id: str | None, device_id: str) -> dict[str, Any]:
        """Trigger metadata (and its device); raises TriggerNotFoundError for an unknown id."""
        if kind == "alert":
            a = (
                await self.alerts.repo.get_alert(trigger_id)
                if self.alerts is not None and trigger_id
                else None
            )
            if a is None or a.device_id != device_id:
                raise TriggerNotFoundError("unknown alert")
            md = a.metadata or {}
            ev = md.get("evidence") or {}
            return {
                "kind": "alert",
                "id": a.alert_id,
                "alert_id": a.alert_id,
                "title": clean(a.title, 160),
                "severity": a.severity,
                "metric": md.get("metric"),
                "signal": "security" if a.category == "security" else signal_for(md.get("metric")),
                "anomaly_id": ev.get("anomaly_id"),
                "prediction_id": ev.get("prediction_id"),
                "observed": _num(md.get("observed")),
                "expected": _num(md.get("expected")),
                "started_at": a.first_detected_at.isoformat(),
            }
        if kind == "anomaly":
            an = self.intelligence.find_active(trigger_id) if self.intelligence and trigger_id else None
            if an is None and trigger_id:
                with contextlib.suppress(Exception):
                    an = await self._events.get_anomaly(trigger_id)
            if an is None or an.device_id != device_id:
                raise TriggerNotFoundError("unknown anomaly")
            return {
                "kind": "anomaly",
                "id": an.anomaly_id,
                "anomaly_id": an.anomaly_id,
                "title": clean(an.title, 160),
                "severity": an.effective_level.value,
                "metric": an.metric_key,
                "signal": signal_for(an.metric_key, an.signal_id),
                "started_at": an.started_at.isoformat(),
            }
        if kind == "prediction":
            p = None
            if self.forecasts is not None and trigger_id:
                p = self.forecasts.find_active(trigger_id) or await self.forecasts.repo.get(trigger_id)
            if p is None or p.device_id != device_id:
                raise TriggerNotFoundError("unknown prediction")
            return {
                "kind": "prediction",
                "id": p.prediction_id,
                "prediction_id": p.prediction_id,
                "title": clean(p.statement, 160),
                "severity": p.severity,
                "metric": p.metric_field,
                "signal": signal_for(None, None, p.target_id),
                "started_at": p.created_at.isoformat(),
            }
        return {"kind": "manual", "id": None, "title": "Device check", "severity": None, "signal": None}

    async def build_context(
        self, device_id: str, trigger: dict[str, Any], now: datetime
    ) -> DiagnosticContext:
        twin = self._twins.get(device_id)
        ctx = DiagnosticContext(device_id=device_id, generated_at=now.timestamp(), trigger=trigger)
        if twin is None:
            ctx.data_quality = {"coverage": 0.0, "stale": True}
            return ctx
        doc = getattr(self._twin_state, "engine", None)
        state = doc.docs[device_id].state if doc is not None and device_id in doc.docs else {}
        ctx.device = {
            "model": clean(state.get("identity.model") or twin.device.model or "", 60),
            "os": clean((state.get("operating_system.name") or {}).get("value") or "", 60)
            if isinstance(state.get("operating_system.name"), dict)
            else None,
        }
        # ---- series: persisted 1-minute history, topped up with live samples
        fields = project_fields(twin.components, None, False)
        keys: dict[str, str] = {}
        for sig, (fld, *_rest) in SERIES.items():
            fv = fields.get(fld)
            if fv is not None and fv.reading is not None and fv.reading.available:
                keys[sig] = fv.reading.key
        start = now - timedelta(seconds=WINDOW_S)
        hist: dict[str, Any] = {}
        if keys:
            try:
                hist = await self._telemetry.history(device_id, list(keys.values()), start, now, 60)
            except Exception as exc:
                log.debug("diagnosis_history_failed", error=str(exc)[:200])
        bounds = self.intelligence.baseline_bounds(device_id, now) if self.intelligence is not None else {}
        expected_minutes = 0
        present_minutes = 0
        for sig, key in keys.items():
            _fld, label, unit, warn, crit, direction = SERIES[sig]
            per_min: dict[int, float] = {int(p.time.timestamp() // 60): p.avg for p in hist.get(key, [])}
            live: dict[int, list[float]] = {}
            for t, v in twin.window.values_since(key, start.timestamp()):
                live.setdefault(int(t // 60), []).append(v)
            for m, vs in live.items():
                per_min[m] = sum(vs) / len(vs)  # live samples are the freshest truth for recent minutes
            points = [(m * 60.0 + 30.0, round(v, 3)) for m, v in sorted(per_min.items())]
            b = bounds.get(sig)
            high = round(b[1], 2) if b and b[2] != "COLD" else None
            source = "device"
            if high is None and direction == "up" and len(points) >= 6:
                # cold device: "typical" = median of the earlier half of this hour + a practical margin
                early = sorted(v for _, v in points[: len(points) // 2])
                high = round(early[len(early) // 2] + WINDOW_MARGIN.get(sig, 10.0), 2)
                if warn is not None:
                    high = min(high, float(warn))
                source = "window"
            ctx.series[sig] = SeriesSummary(
                sig,
                label,
                unit,
                points,
                baseline_median=round(b[0], 2) if b else None,
                baseline_high=high,
                warning=float(warn) if warn is not None else None,
                critical=float(crit) if crit is not None else None,
                direction=direction,
                baseline_source=source,
            )
            if sig in ("cpu", "memory", "temperature"):
                expected_minutes += WINDOW_S // 60
                present_minutes += len(points)
        # ---- other phases' signals
        from app.services.intelligence import active_anomalies

        recent_cut = now - timedelta(hours=2)
        anomalies = [
            *active_anomalies(twin),
            *[a for a in twin.anomalies_recent if a.started_at >= recent_cut],
        ]
        seen: set[str] = set()
        for a in anomalies[:12]:
            if a.anomaly_id in seen:
                continue
            seen.add(a.anomaly_id)
            ctx.anomalies.append(
                {
                    "id": a.anomaly_id,
                    "signal": signal_for(a.metric_key, a.signal_id),
                    "metric": a.metric_key,
                    "title": clean(a.title, 120),
                    "level": a.effective_level.value,
                    "observed": _num(a.value),
                    "expected": _num(a.expected_value),
                    "unit": None,
                    "confidence": a.confidence,
                    "started_at": a.started_at.isoformat(),
                    "status": a.status,
                }
            )
        if self.forecasts is not None:
            for p in self.forecasts.tracker.active(device_id):
                ctx.predictions.append(
                    {
                        "id": p.prediction_id,
                        "target": PREDICTION_SIGNAL.get(p.target_id, p.target_id),
                        "metric": p.metric_field,
                        "current": p.current_value,
                        "threshold": p.threshold,
                        "statement": clean(p.statement, 200),
                        "confidence_band": p.confidence_band,
                        "eta_s": p.time_to_threshold_s,
                        "created_at": p.created_at.isoformat(),
                        "status": p.status,
                    }
                )
        if self.alerts is not None:
            for a in self.alerts.engine.open_alerts():
                if a.device_id == device_id:
                    ctx.alerts.append(
                        {
                            "id": a.alert_id,
                            "severity": a.severity,
                            "status": a.status.value,
                            "title": clean(a.title, 120),
                        }
                    )
        # ---- processes (names only where the enterprise policy shows them)
        allowed = bool(self._s.twin_show_process_names)
        ctx.process_names_allowed = allowed
        t_start = trigger.get("started_at")
        onset = datetime.fromisoformat(t_start) if isinstance(t_start, str) else now - timedelta(minutes=5)
        trig_series = ctx.series.get(str(trigger.get("signal")))
        if trig_series is not None:  # the measured onset beats the (later) alert time for "before"
            measured = temporal(trig_series).onset
            if measured is not None:
                onset = min(onset, datetime.fromtimestamp(measured, UTC))
        w_start = max(onset - timedelta(minutes=1), now - timedelta(seconds=PROCESS_WINDOW_S))
        during = self._procs.window(device_id, w_start, now)
        before = self._procs.window(device_id, w_start - timedelta(minutes=10), w_start)
        ctx.process_window = {
            "start": w_start.isoformat(),
            "end": now.isoformat(),
            "snapshots": len(during),
            "before": len(before),
        }
        if during:
            by_cpu = attribute_processes(during, before, by_memory=False)
            by_mem = attribute_processes(during, before, by_memory=True)
            merged: dict[str, dict[str, Any]] = {}
            for row in [*by_cpu, *by_mem]:
                merged.setdefault(row["process"], row)
            for i, (name, row) in enumerate(merged.items()):
                shown = clean(name, 60) if allowed else f"process #{i + 1}"
                mb = row["memory_bytes_before"]
                ctx.processes.append(
                    ProcessFigure(
                        shown,
                        float(row["cpu_percent_during"]),
                        row["cpu_percent_before"],
                        row["memory_bytes_during"] / 1024**2,
                        mb / 1024**2 if mb is not None else None,
                    )
                )
        # ---- security posture findings (twin health reasons) and recent agent events
        health = state.get("health") or {}
        findings = [
            clean(r.get("message"), 120)
            for r in health.get("reasons") or []
            if r.get("rule") in ("security", "posture") and r.get("state") in ("WARNING", "CRITICAL")
        ]
        ctx.security = {"posture": state.get("security.posture"), "findings": findings}
        for e in list(twin.recent_events)[:50]:
            ts = str(e.get("timestamp") or "")
            if ts and ts >= recent_cut.isoformat()[:19]:
                ctx.timeline.append(
                    {
                        "event_id": clean(e.get("event_id"), 64),
                        "type": clean(e.get("type"), 64),
                        "severity": e.get("severity"),
                        "time": ts[:25],
                        "message": clean(e.get("message"), 160),
                    }
                )
        last_seen = twin.device.last_seen
        stale = last_seen is None or (now - last_seen).total_seconds() > 300
        ctx.data_quality = {
            "coverage": round(present_minutes / expected_minutes, 2) if expected_minutes else 0.0,
            "stale": stale,
            "baseline": "device" if bounds else "none",
            "process_snapshots": len(during),
        }
        return ctx

    # ------------------------------------------------------------------ read side / feedback
    async def detail(self, diagnosis_id: str) -> dict[str, Any] | None:
        d = await self.repo.get(diagnosis_id)
        if d is None:
            return None
        out = d.to_dict(full=True)
        versions = await self.repo.versions(d.series_id)
        out["versions"] = [
            {
                "diagnosis_id": v.diagnosis_id,
                "version": v.version,
                "status": v.status.value,
                "created_at": v.created_at.isoformat(),
                "likely_cause": v.likely_cause,
                "confidence_level": v.confidence_level,
                "reasoning_model": v.reasoning_model,
            }
            for v in versions
        ]
        out["feedback"] = [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in f.items() if k != "created_by"}
            for f in await self.repo.feedback_for([d.diagnosis_id])
        ]
        return out

    async def add_feedback(
        self, d: Diagnosis, verdict: str, actual_cause: str | None, note: str | None, by: str
    ) -> dict[str, Any]:
        if verdict not in FEEDBACK_VERDICTS:
            raise ValueError("unknown verdict")
        item = {
            "id": uuid.uuid4().hex,
            "diagnosis_id": d.diagnosis_id,
            "series_id": d.series_id,
            "device_id": d.device_id,
            "diagnosis_type": d.diagnosis_type.value,
            "verdict": verdict,
            "actual_cause": clean(actual_cause, 500) if actual_cause else None,
            "note": clean(note, 1000) if note else None,
            "created_by": by,
            "created_at": datetime.now(UTC),
        }
        await self.repo.add_feedback(item)
        log.info("diagnosis_feedback", diagnosis_id=d.diagnosis_id, verdict=verdict)  # no free text in logs
        return {
            k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in item.items() if k != "created_by"
        }

    async def status(self) -> dict[str, Any]:
        try:
            health = await self.provider.health()
        except Exception as exc:
            health = {"provider": self.provider.name, "available": False, "error": type(exc).__name__}
        done = max(1, self.stats["completed"])
        return {
            "enabled": True,
            "mode": self._s.diagnosis_mode,
            "provider": health,
            "queue_depth": self._queue.qsize(),
            "queue_max": self._s.diagnosis_queue_max,
            "concurrency": self._s.diagnosis_concurrency,
            "running": len(self._active),
            "auto_min_severity": self._s.diagnosis_auto_min_severity,
            "timeout_s": self.timeout_s,
            "ttl_s": self._s.diagnosis_ttl_s,
            "memory_gate_percent": self.memory_gate,
            "prompt_version": prompts.PROMPT_VERSION,
            "stats": {**self.stats, "mean_ms": round(self.stats["ms_total"] / done, 1)},
            "feedback": await self.repo.feedback_stats(),
        }


def _num(v: Any) -> float | None:
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None
