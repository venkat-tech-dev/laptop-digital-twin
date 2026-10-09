"""Composition root: builds services, wires the event pipeline, owns background tasks."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.core.config import Settings
from app.core.metrics import ANOMALIES_ACTIVE, REDIS_CONNECTED
from app.core.security import Authenticator, SlidingWindowRateLimiter, TokenBucketLimiter
from app.core.supervisor import Supervisor
from app.domain.events.bus import EventBus
from app.domain.events.events import DomainEvent
from app.domain.remediation.envelope import Signer
from app.infrastructure.database.engine import Database
from app.infrastructure.redis.client import RedisGateway
from app.infrastructure.websocket.manager import HIGH_VOLUME_EVENTS, ConnectionManager
from app.infrastructure.websocket.protocol import envelope, to_message
from app.repositories.admin import AdminRepository, MemoryAdminRepository, SqlAdminRepository
from app.repositories.alerting import MemoryAlertRepository, SqlAlertRepository
from app.repositories.base import DeviceRepository, EventRepository, TelemetryRepository
from app.repositories.diagnoses import MemoryDiagnosisRepository, SqlDiagnosisRepository
from app.repositories.governance import MemoryGovernanceRepository, SqlGovernanceRepository
from app.repositories.intelligence import MemoryIntelligenceRepository, SqlIntelligenceRepository
from app.repositories.memory import MemoryDeviceRepository, MemoryEventRepository, MemoryTelemetryRepository
from app.repositories.predictions import MemoryPredictionRepository, SqlPredictionRepository
from app.repositories.receipts import SqlReceiptRepository
from app.repositories.remediation import MemoryRemediationRepository, SqlRemediationRepository
from app.repositories.sql import SqlDeviceRepository, SqlEventRepository, SqlTelemetryRepository
from app.services.admin import AdminService
from app.services.alerting import AlertService
from app.services.analytics import AnalyticsService
from app.services.anomaly import AnomalyService
from app.services.assignments import AssignmentService
from app.services.audit import AuditService
from app.services.devices import BatchDeduper, DeviceAuthService
from app.services.diagnosis import DiagnosisService
from app.services.diagnosis_providers import build_provider
from app.services.digital_twin import DigitalTwinService
from app.services.fleet import FleetService
from app.services.forecasting import ForecastService
from app.services.geometry import GeometryResolver
from app.services.governance_jobs import DataGovernanceService
from app.services.identity import IdentityService, SecretStore
from app.services.ingest_pipeline import IngestPipeline, LatencyStats, ReceiptWriter
from app.services.insights import ProcessHistory
from app.services.intelligence import IntelligenceService
from app.services.notify_providers import (
    BrowserProvider,
    EmailProvider,
    InAppProvider,
    WebhookProvider,
    WindowsToastProvider,
)
from app.services.persistence import EventRecorder, RetentionTask, SamplePersister
from app.services.policies import PolicyService
from app.services.presence import PresenceService
from app.services.remediation import RemediationService
from app.services.sequences import SequenceTracker
from app.services.simulation import SimulationService
from app.services.sync import SyncService
from app.services.telemetry import RecentBuffer, TelemetryService
from app.services.tenancy import TenancyService
from app.services.twin_engine import TwinEngine, TwinPolicy
from app.services.twin_state import TwinStateService

log = structlog.get_logger("container")
VERSION = "1.0.0"


@dataclass
class Container:
    settings: Settings
    auth: Authenticator
    limiter: SlidingWindowRateLimiter
    bus: EventBus
    twin: DigitalTwinService
    ws: ConnectionManager
    device_repo: DeviceRepository
    telemetry_repo: TelemetryRepository
    event_repo: EventRepository
    persister: SamplePersister
    recorder: EventRecorder
    telemetry: TelemetryService
    analytics: AnalyticsService
    simulation: SimulationService
    anomalies: AnomalyService
    geometry: GeometryResolver
    admin_repo: AdminRepository
    admin: AdminService
    sync: SyncService
    process_history: ProcessHistory
    device_auth: DeviceAuthService
    deduper: BatchDeduper
    db: Database | None
    redis: RedisGateway | None
    presence: PresenceService = field(init=False)
    twin_state: TwinStateService = field(init=False)
    assignments: AssignmentService = field(init=False)
    ingest: IngestPipeline = field(init=False)
    agent_limiter: TokenBucketLimiter = field(init=False)
    receipt_repo: Any = None
    intelligence_repo: Any = None
    intelligence: IntelligenceService | None = field(init=False, default=None)
    prediction_repo: Any = None
    forecasts: ForecastService | None = field(init=False, default=None)
    alert_repo: Any = None
    alerts: AlertService | None = field(init=False, default=None)
    diagnosis_repo: Any = None
    diagnosis: DiagnosisService | None = field(init=False, default=None)
    remediation_repo: Any = None
    remediation: RemediationService | None = field(init=False, default=None)
    governance_repo: Any = None
    audit: AuditService = field(init=False)
    tenancy: TenancyService = field(init=False)
    policies: PolicyService = field(init=False)
    identity: IdentityService = field(init=False)
    governance_jobs: DataGovernanceService = field(init=False)
    started_monotonic: float = field(default_factory=time.monotonic)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    _background: set[asyncio.Task[Any]] = field(default_factory=set)
    _remote_interest: set[str] = field(default_factory=set)
    fanout_skipped: int = 0

    def __post_init__(self) -> None:
        self.supervisor = Supervisor(self._stop)
        self.fleet = FleetService(self)  # Phase 10: fleet intelligence (tenant-scoped)
        self.role = "active"  # active | standby | lost (leader election, app/main.py)
        self.started = False
        s = self.settings
        self.presence = PresenceService(s.presence_stale_after_s, s.presence_offline_after_s)
        latency = LatencyStats()
        self.ingest = IngestPipeline(
            self,
            SequenceTracker(),
            self.presence,
            ReceiptWriter(self.receipt_repo, s.receipt_retention_hours),
            latency,
        )
        self.agent_limiter = TokenBucketLimiter(s.ingest_rate_per_device_per_min, s.ingest_rate_burst)
        self.telemetry.hot_state_interval_s = s.hot_state_interval_s
        self.assignments = AssignmentService(self.admin_repo)
        gov = self.governance_repo or MemoryGovernanceRepository()
        self.audit = AuditService(gov)
        self.tenancy = TenancyService(gov, self.assignments, self.audit)
        self.policies = PolicyService(gov, self.tenancy, self.audit)
        self.identity = IdentityService(s, gov, SecretStore(s.secrets_dir or None))
        self.login_limiter = SlidingWindowRateLimiter(limit=10, window_s=60.0)  # brute-force guard per client
        self.mfa_limiter = SlidingWindowRateLimiter(limit=10, window_s=60.0)
        self.enroll_limiter = SlidingWindowRateLimiter(limit=20, window_s=60.0)
        self.governance_jobs = DataGovernanceService(
            self.db, self.tenancy, self.policies, self.audit, s, self.telemetry_repo
        )
        self.policies.agent_version_of = self._agent_version_of
        self.twin_state = TwinStateService(
            TwinEngine(
                TwinPolicy(
                    publish_wait_s=s.twin_publish_wait_s,
                    grace_s=s.twin_freshness_grace_s,
                    default_interval_s=s.twin_default_interval_s,
                    process_details_allowed=s.twin_show_process_names,
                )
            ),
            self.twin,
            self.presence,
            self.bus,
            self.telemetry.record_system_event,
            self.redis,
            extras=lambda device_id: self.assignments.extras(device_id),
            summary_interval_s=s.twin_summary_interval_s,
            persist_interval_s=s.hot_state_interval_s,
        )
        self.telemetry.twin_state = self.twin_state
        self.twin_state.latency = latency
        if s.anomaly_intelligence_enabled:
            self.intelligence = IntelligenceService(
                s,
                self.twin,
                self.presence,
                self.telemetry_repo,
                self.event_repo,
                self.intelligence_repo or MemoryIntelligenceRepository(),
                self.admin_repo,
                publish=self.bus.publish_all,
                record=self.telemetry.record_anomaly,
                on_twin_changed=self.twin_state.on_applied,
            )
            self.anomalies.intelligence = self.intelligence
        if s.prediction_enabled:
            self.forecasts = ForecastService(
                s,
                self.twin,
                self.presence,
                self.telemetry_repo,
                self.prediction_repo or MemoryPredictionRepository(),
                self.admin_repo,
                publish=self.bus.publish_all,
                on_twin_changed=self.twin_state.on_applied,
            )
        if s.alerting_enabled:
            self.alerts = self._build_alerts(s)
            self.alerts.tenancy = self.tenancy
        if s.diagnosis_enabled:
            self.diagnosis = DiagnosisService(
                s,
                self.twin,
                self.twin_state,
                self.telemetry_repo,
                self.event_repo,
                self.diagnosis_repo or MemoryDiagnosisRepository(),
                self.process_history,
                self.bus.publish_all,
                self.telemetry.record_system_event,
                provider=build_provider(s),
                on_twin_changed=self.twin_state.on_applied,
                intelligence=self.intelligence,
                forecasts=self.forecasts,
                alerts=self.alerts,
            )
            self.diagnosis.tenant_of = self._tenant_of
            self.diagnosis.policy_value = self.policies.value
            self.diagnosis.quota = lambda d: self.tenancy.check_rate(
                self._tenant_of(d), "diagnosis_jobs_per_hour"
            )[0]
        if s.remediation_enabled:
            signer = None
            if s.remediation_signing_key:
                try:
                    signer = Signer.from_b64(s.remediation_signing_key)
                except Exception as exc:  # fail closed: nothing can be dispatched without a valid key
                    log.error("remediation_signing_key_invalid", error=type(exc).__name__)
            self.remediation = RemediationService(
                s,
                self.twin,
                self.presence,
                self.remediation_repo or MemoryRemediationRepository(),
                self.admin_repo,
                self.bus.publish_all,
                self.telemetry.record_system_event,
                signer,
                assignments=self.assignments,
                diagnoses=self.diagnosis,
                on_twin_changed=self.twin_state.on_applied,
            )
            self.remediation.tenant_of = self._tenant_of
            self.remediation.governance = self.policies
            self.remediation.lifecycle_of = self.tenancy.lifecycle
        self.ws.set_hooks(
            primary=lambda: self.twin.primary_device_id,
            on_sent=lambda ms: latency.observe("ws_queue_ms", ms),
        )

    def _tenant_of(self, device_id: str) -> str:
        return self.tenancy.org_of(device_id) or "default"

    def _agent_version_of(self, device_id: str) -> str | None:
        p = self.presence.get(device_id)
        version = (p.heartbeat or {}).get("agent_version") if p else None
        twin = self.twin.get(device_id)
        return version or (twin.device.agent_version if twin else None)

    def metrics_authz_denied(self, permission: str) -> None:
        from app.core.metrics import AUTHZ_DENIALS

        AUTHZ_DENIALS.labels(permission[:48]).inc()

    def note_auth_failure(self, request: Any, kind: str, detail: str) -> None:
        from app.core.metrics import AUTH_FAILURES

        AUTH_FAILURES.labels(kind).inc()
        ip = request.client.host if request is not None and request.client else None
        self.audit.record(
            None,
            "anonymous",
            "anonymous",
            "auth.failed",
            "authentication",
            result="FAILURE",
            reason=detail[:120],
            severity="WARNING",
            ip=ip,
        )

    def _build_alerts(self, s: Settings) -> AlertService:
        default_subject = "local" if s.auth_mode == "none" else "api-key"
        holder: dict[str, AlertService] = {}

        def push(n: Any) -> None:
            holder["svc"].push_to_user(n, "created")

        def push_browser(n: Any) -> int:
            return holder["svc"].push_to_user(n, "browser")

        providers = {
            "in_app": InAppProvider(push),
            "browser": BrowserProvider(push_browser),
            "windows": WindowsToastProvider(),
            "email": EmailProvider(
                s.smtp_host, s.smtp_port, s.smtp_username, s.smtp_password, s.smtp_from, s.smtp_starttls
            ),
            "webhook": WebhookProvider(
                lambda: {t.name: t.url for t in holder["svc"].webhooks.values()},
                s.webhook_signing_secret,
                s.webhook_allow_http,
                s.webhook_allow_private,
            ),
        }
        svc = AlertService(
            s,
            self.alert_repo or MemoryAlertRepository(),
            self.admin_repo,
            self.admin_repo,
            self.assignments,
            self.ws,
            self.bus.publish_all,
            self.telemetry.record_system_event,
            providers,
            default_subject,
        )
        holder["svc"] = svc
        return svc

    def publish_soon(self, event: DomainEvent) -> None:
        """Fan out a domain event from synchronous code (ingest path) without awaiting it."""
        task = asyncio.get_running_loop().create_task(self.bus.publish_all([event]))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @property
    def persistence_mode(self) -> str:
        return "postgresql" if self.db is not None else "memory"

    # ----------------------------------------------------------------- startup
    async def start(self) -> None:
        self.started = True
        if self.db is not None:
            try:
                await self.db.ping()
                await self.db.detect_timescale()
                for device in await self.device_repo.list_all():
                    self.twin.restore_device(device)
                    self.presence.known(device.device_id, device.last_seen)
                warmed = await self.ingest.receipts.warm(self.deduper)
                await self.assignments.load()
                self.assignments.set_departments(
                    await self.admin.workspaces([d.device_id for d in self.twin.devices()])
                )
                policies = await self.db.apply_retention(
                    self.settings.retention_days, self.settings.aggregate_retention_days
                )
                log.info(
                    "database_ready",
                    timescaledb=self.db.timescale,
                    aggregate_5m=self.db.aggregate_5m,
                    devices_restored=len(self.twin.devices()),
                    receipts_warmed=warmed,
                    retention=policies,
                )
            except Exception as exc:
                log.error("database_unavailable_at_startup", error=str(exc)[:300])
        if self.redis is not None:
            try:
                await self.redis.ping()
                self.redis.start_listener(self._on_redis_event)
                restored = await self.twin_state.restore([d.device_id for d in self.twin.devices()])
                log.info("redis_ready", twin_documents_restored=restored)
            except Exception as exc:
                log.error("redis_unavailable_at_startup", error=str(exc)[:300])
        self.bus.subscribe(self._fan_out)
        retention = RetentionTask(
            self.telemetry_repo,
            self.settings.retention_days,
            enabled=self.db is not None and not self.db.timescale,
        )
        # Phase 10: every loop is supervised (restart with bounded backoff, health in /health/ready)
        spawn = self.supervisor.spawn
        stop = self._stop
        # (name, loop, critical, may_finish: returns at once when its feature is off)
        for name, factory, critical, may_finish in (
            ("persister", lambda: self.persister.run(stop), True, False),
            ("recorder", lambda: self.recorder.run(stop), False, False),
            ("retention", lambda: retention.run(stop), False, True),
            ("event_retention", self._event_retention_loop, False, False),
            ("receipts", lambda: self.ingest.receipts.run(stop), True, True),
            ("liveness", self._liveness_loop, True, False),
            ("heartbeat", self._heartbeat_loop, False, False),
            ("sync", lambda: self.sync.run(stop), False, True),
        ):
            self._tasks.append(spawn(name, factory, critical=critical, may_finish=may_finish))
        await self._start_governance()
        if self.intelligence is not None:
            await self.intelligence.load_config()
            intelligence = self.intelligence
            self._tasks.append(spawn("intelligence", lambda: intelligence.run(stop)))
        if self.alerts is not None:
            await self.alerts.load()
            self.bus.subscribe(self.alerts.on_event)
            alerts = self.alerts
            self._tasks.append(spawn("alerting", lambda: alerts.run(stop), critical=True))
        if self.diagnosis is not None:
            self.bus.subscribe(self.diagnosis.on_event)
            diagnosis = self.diagnosis
            self._tasks.append(spawn("diagnosis", lambda: diagnosis.run(stop)))
        if self.remediation is not None:
            await self.remediation.load()
            self.bus.subscribe(self.remediation.on_event)
            remediation = self.remediation
            self._tasks.append(spawn("remediation", lambda: remediation.run(stop), critical=True))
        if self.forecasts is not None:
            await self.forecasts.load()
            forecasts = self.forecasts
            self._tasks.append(spawn("forecasting", lambda: forecasts.run(stop)))

    async def _start_governance(self) -> None:
        """Phase 9: organisations, policies; devices known before enrollment existed join ``default``."""
        try:
            await self.tenancy.load()
            for d in self.twin.devices():
                if self.tenancy.org_of(d.device_id) is None:
                    await self.tenancy.ensure_device(d.device_id, "default", "legacy", "startup")
            await self.policies.load()
            await self.identity.load()
        except Exception as exc:
            log.error("governance_load_failed", error=str(exc)[:300])
        stop = self._stop
        self._tasks.append(self.supervisor.spawn("audit", lambda: self.audit.run(stop), critical=True))
        self._tasks.append(self.supervisor.spawn("tenant_retention", lambda: self.governance_jobs.run(stop)))
        self._tasks.append(self.supervisor.spawn("fleet_snapshots", lambda: self.fleet.run(stop)))

    async def stop(self) -> None:
        self._stop.set()
        try:
            await self.twin_state.persist_all()  # resume from the last known state after a restart
        except Exception as exc:
            log.warning("twin_persist_on_stop_failed", error=str(exc)[:200])
        await self.ws.close_all()
        for task in self._tasks:
            if task.get_name() in ("liveness", "heartbeat", "sync", "event_retention"):
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.redis is not None:
            await self.redis.close()
        if self.db is not None:
            await self.db.dispose()

    # -------------------------------------------------------- event fan-out
    async def _fan_out(self, event: DomainEvent) -> None:
        """Serialise once; deliver to this replica's sockets directly; other replicas via Redis."""
        message = to_message(event)
        device_id = message.get("device_id")
        if (
            message.get("event") in HIGH_VOLUME_EVENTS
            and device_id
            and not self.ws.wants_device(device_id)
            and device_id not in self._remote_interest
        ):
            # Nobody watches this device right now (here or on another replica): do not serialise or
            # publish its per-batch deltas. A client that subscribes later gets a snapshot first.
            self.fanout_skipped += 1
            return
        data = json.dumps(message, default=str)
        if message.get("event") == "twin.state.patch":
            self.twin_state.patch_bytes += len(data)  # wire size, measured on the string we send anyway
            self.twin_state.patches_serialized += 1
        self.ws.broadcast(message, data)
        if self.redis is not None and self.redis.connected:
            try:
                await self.redis.publish_raw(data)
            except Exception as exc:
                log.warning("redis_publish_failed", error=str(exc)[:200])

    async def _on_redis_event(self, message: dict[str, Any]) -> None:
        self.ws.broadcast(message)

    async def _liveness_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.telemetry.check_liveness()
                presence_changes: list[DomainEvent] = list(self.presence.evaluate())
                if presence_changes:
                    await self.bus.publish_all(presence_changes)
                await self.twin_state.tick()
                twin = self.twin.get()
                if twin is not None:
                    counts = {"info": 0, "warning": 0, "critical": 0}
                    for a in [*twin.anomalies.active.values(), *twin.behavior_active]:
                        counts[a.severity.value] += 1
                    for sev, n in counts.items():
                        ANOMALIES_ACTIVE.labels(sev).set(n)
            except Exception:
                log.exception("liveness_check_failed")

    async def _event_retention_loop(self) -> None:
        """Device/system events are kept EVENT_RETENTION_DAYS (longer than raw samples)."""
        while True:
            await asyncio.sleep(3600)
            try:
                cutoff = datetime.now(UTC) - timedelta(days=self.settings.event_retention_days)
                removed = await self.event_repo.purge_system_events_older_than(cutoff)
                if removed:
                    log.info("event_retention_purged", rows=removed)
            except Exception as exc:
                log.warning("event_retention_failed", error=str(exc)[:200])

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.ws_heartbeat_s)
            try:
                await self._heartbeat_once()
            except Exception:  # one bad client or message must not stop heartbeats for everyone
                log.exception("heartbeat_failed")

    async def _heartbeat_once(self) -> None:
        if self.redis is not None and not self.redis.connected:
            with contextlib.suppress(Exception):
                await self.redis.ping()  # recover after an outage (sets connected)
        if self.redis is not None:
            REDIS_CONNECTED.set(1 if self.redis.connected else 0)
        if self.redis is not None and self.redis.connected:
            try:  # interest-based fan-out across replicas
                await self.redis.set_interest(self.ws.interested_devices())
                self._remote_interest = await self.redis.remote_interest()
            except Exception as exc:
                log.debug("interest_refresh_failed", error=str(exc)[:200])
        # per client: each organisation sees only its own primary device (never another tenant's)
        server_time = datetime.now(UTC).isoformat()
        primary = self.twin.primary_device_id
        for client in self.ws.clients():
            allowed = client.allowed or set()
            device_id = primary if primary in allowed else (min(allowed) if allowed else None)
            twin = self.twin.get(device_id) if device_id else None
            self.ws.send(
                client,
                envelope(
                    "heartbeat",
                    server_time=server_time,
                    primary_device_id=device_id,
                    device_status=twin.device.status.value if twin else "OFFLINE",
                    last_seen=twin.device.last_seen.isoformat() if twin and twin.device.last_seen else None,
                ),
            )
        await self.ws.close_idle(self.settings.ws_heartbeat_s * 9)


def build_container(settings: Settings) -> Container:
    db: Database | None = (
        Database(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout_s=settings.db_pool_timeout_s,
            statement_timeout_ms=settings.db_statement_timeout_ms,
        )
        if settings.persistence_enabled
        else None
    )
    redis = RedisGateway(settings.redis_url) if settings.redis_enabled else None
    device_repo: DeviceRepository
    telemetry_repo: TelemetryRepository
    event_repo: EventRepository
    if db is not None:
        device_repo, telemetry_repo, event_repo = (
            SqlDeviceRepository(db),
            SqlTelemetryRepository(db),
            SqlEventRepository(db),
        )
    else:
        device_repo, telemetry_repo, event_repo = (
            MemoryDeviceRepository(),
            MemoryTelemetryRepository(),
            MemoryEventRepository(),
        )
    admin_repo: AdminRepository = SqlAdminRepository(db) if db is not None else MemoryAdminRepository()
    auth = Authenticator(settings)
    admin = AdminService(admin_repo, auth, settings)
    process_history = ProcessHistory()
    bus = EventBus()
    twin = DigitalTwinService(settings)
    persister = SamplePersister(telemetry_repo, settings)
    recorder = EventRecorder()
    telemetry = TelemetryService(
        twin, bus, persister, recorder, telemetry_repo, event_repo, RecentBuffer(redis), redis
    )
    return Container(
        settings=settings,
        auth=auth,
        limiter=SlidingWindowRateLimiter(settings.rate_limit_per_minute),
        bus=bus,
        twin=twin,
        ws=ConnectionManager(settings.ws_send_queue_max),
        device_repo=device_repo,
        telemetry_repo=telemetry_repo,
        event_repo=event_repo,
        persister=persister,
        recorder=recorder,
        telemetry=telemetry,
        analytics=AnalyticsService(twin, telemetry_repo),
        simulation=SimulationService(twin),
        anomalies=AnomalyService(twin, event_repo, admin, telemetry_repo, process_history),
        geometry=GeometryResolver(settings.models_dir),
        admin_repo=admin_repo,
        admin=admin,
        sync=SyncService(admin, settings.sync_target_key),
        process_history=process_history,
        device_auth=DeviceAuthService(admin_repo, settings.device_token_cache_s),
        deduper=BatchDeduper(),
        db=db,
        redis=redis,
        receipt_repo=SqlReceiptRepository(db) if db is not None else None,
        intelligence_repo=SqlIntelligenceRepository(db) if db is not None else MemoryIntelligenceRepository(),
        prediction_repo=SqlPredictionRepository(db) if db is not None else MemoryPredictionRepository(),
        alert_repo=SqlAlertRepository(db) if db is not None else MemoryAlertRepository(),
        diagnosis_repo=SqlDiagnosisRepository(db) if db is not None else MemoryDiagnosisRepository(),
        remediation_repo=SqlRemediationRepository(db) if db is not None else MemoryRemediationRepository(),
        governance_repo=SqlGovernanceRepository(db) if db is not None else MemoryGovernanceRepository(),
    )
