"""Agent lifecycle: INITIALIZE -> COLLECT -> CACHE -> SYNC -> HEALTH CHECK -> STOP (graceful).

Used by both the console entry point and the Windows service. Collection never waits for the
network: every flush writes a batch to the local SQLite outbox first; synchronisation is a separate
concern that drains the outbox whenever the backend is reachable.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from app.config.settings import AgentSettings, RunMode
from app.contracts import (
    DeviceEvent,
    EventSeverity,
    InventoryEnvelope,
    MetricKind,
    MetricSample,
    Priority,
    ProcessSnapshot,
    TelemetryBatch,
)
from app.health.monitor import HealthMonitor
from app.health.posture import PostureTracker
from app.normalization.normalizer import Normalizer
from app.observability.logging import bind_identity
from app.platform.toast import interactive_session, show_toast
from app.platform.winsys import redact_profile
from app.providers.base import Reading
from app.publisher.pipeline import BatchAccumulator
from app.publisher.sync import SyncManager
from app.remediation.apps import WindowsAppController
from app.remediation.executor import IMPLEMENTED, ActionExecutor, Hooks, parse_applications
from app.remediation.keys import KeyPin
from app.remediation.ledger import ExecutionLedger
from app.runtime import AgentRuntime
from app.scheduler import CollectionScheduler
from app.security.credentials import CredentialStore
from app.storage.queue import TelemetryStore
from app.transport.client import AgentApiClient

UNCONFIRMED_PER_HEARTBEAT = 500  # batch ids asked about per heartbeat (backend limit)
AGENT_VERSION = (
    "1.8.0"  # 1.8: keeps accepted batches until confirmed durable (Phase 10); 1.7: enrollment tokens
)
TEMPERATURE_METRICS = ("cpu.temperature_c", "cpu.package_temperature_c", "thermal.zone_temperature_c")
TEMPERATURE_HYSTERESIS_C = 5.0
log = structlog.get_logger("agent.runner")
SHUTDOWN_SYNC_TIMEOUT_S = 5.0


class AgentRunner:
    def __init__(self, settings: AgentSettings, run_mode: RunMode) -> None:
        self.settings = settings
        self.run_mode = run_mode
        self.data_dir: Path = settings.resolved_data_dir(run_mode)
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ external control (service)
    def request_stop(self) -> None:
        """Thread-safe stop request (called from the service control handler)."""
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    async def run(self, stop: asyncio.Event | None = None) -> None:
        s = self.settings
        self._loop = asyncio.get_running_loop()
        self._stop = stop or asyncio.Event()
        stop = self._stop

        runtime = AgentRuntime(s)
        inventory = await asyncio.to_thread(runtime.discover)
        bind_identity(runtime.device_id, s.agent_id)
        log.info(
            "hardware_discovered",
            manufacturer=inventory.get("manufacturer"),
            model=inventory.get("model"),
            discovery_errors=sorted((inventory.get("discovery_errors") or {}).keys()),
            run_mode=self.run_mode.value,
            data_dir=redact_profile(str(self.data_dir)),
        )

        store = TelemetryStore(
            self.data_dir / "telemetry.db",
            max_batches=s.queue_max_batches,
            max_bytes=s.queue_max_mb * 1024 * 1024,
            max_age_s=s.queue_max_age_h * 3600,
        )
        client = AgentApiClient(
            s.backend_url,
            s.agent_ingest_key,
            CredentialStore(self.data_dir / "credentials.bin"),
            agent_version=AGENT_VERSION,
            timeout_s=s.request_timeout_s,
            ca_bundle=s.ca_bundle,
            allow_insecure_localhost=s.allow_insecure_localhost,
            use_device_tokens=s.use_device_tokens,
            enrollment_token=s.enrollment_token,
            rotate_before=timedelta(days=s.credential_rotate_days),
        )
        client.bind_device(runtime.device_id)
        agent_id = s.agent_id or store.get_state("agent_instance_id")
        if agent_id is None:  # first start of this installation: a stable random instance id
            agent_id = uuid.uuid4().hex
            store.set_state("agent_instance_id", agent_id)

        def envelope() -> InventoryEnvelope:
            return InventoryEnvelope(
                device_id=runtime.device_id,
                agent_id=agent_id,
                agent_version=AGENT_VERSION,
                discovered_at=datetime.now(UTC),
                inventory=runtime.inventory,
            )

        sync = SyncManager(
            store,
            client,
            envelope,
            batch_size=s.batch_size,
            max_attempts=s.sync_max_attempts,
            backoff_base_s=s.backoff_base_s,
            backoff_max_s=s.backoff_max_s,
            replay_after_s=max(30.0, 3 * s.publish_interval_ms / 1000.0),
            max_batch_bytes=s.max_batch_bytes,
        )
        accumulator = BatchAccumulator(s.static_resend_s)
        posture = PostureTracker()
        wake = asyncio.Event()  # set by HIGH/CRITICAL events: flush + sync without waiting
        hot: set[str] = set()  # temperature sensors currently above CRITICAL_TEMPERATURE_C
        counters = {"batches_created_total": 0}

        def on_events(events: list[DeviceEvent]) -> None:
            accumulator.add_events(events)
            if accumulator.urgent():
                wake.set()

        def watch_temperature(samples: list[MetricSample]) -> None:
            for smp in samples:
                if smp.metric not in TEMPERATURE_METRICS or not isinstance(smp.value, (int, float)):
                    continue
                key = smp.metric + str(sorted(smp.labels.items()))
                if smp.value >= s.critical_temperature_c and key not in hot:
                    hot.add(key)
                    on_events(
                        [
                            DeviceEvent(
                                type="critical_temperature",
                                severity=EventSeverity.CRITICAL,
                                timestamp=smp.timestamp,
                                source=smp.source,
                                message=f"{smp.metric} reached {smp.value:.0f} °C "
                                f"(threshold {s.critical_temperature_c:.0f} °C)",
                                data={"metric": smp.metric, "value": float(smp.value), **smp.labels},
                            )
                        ]
                    )
                elif smp.value < s.critical_temperature_c - TEMPERATURE_HYSTERESIS_C:
                    hot.discard(key)

        def sink(samples: list[MetricSample], procs: ProcessSnapshot | None) -> None:
            accumulator.add(samples, procs)
            posture.observe(samples)
            watch_temperature(samples)

        providers = runtime.build_providers(
            (store.get_state, store.set_state), lambda: client.last_latency_ms
        )
        if runtime.network_health is not None:
            runtime.network_health.on_internet_restored = sync.retry_now
        scheduler = CollectionScheduler(
            providers, runtime.worker, sink, s.cpu_budget_percent, event_sink=on_events
        )
        scheduler.collector_failed_after = s.collector_failed_after
        monitor = HealthMonitor(
            agent_version=AGENT_VERSION,
            run_mode=self.run_mode.value,
            scheduler=scheduler,
            store=store,
            sync=sync,
            worker=runtime.worker,
            path=self.data_dir / "health.json",
        )
        monitor.extra = lambda: {
            "api_latency_ms": client.last_latency_ms,
            "batches_created_total": counters["batches_created_total"],
            "events_generated_total": accumulator.events_generated_total,
            "last_sequence": int(store.get_state("sequence") or 0),
        }
        normalizer = Normalizer()

        def seq_next() -> int:
            n = int(store.get_state("sequence") or 0) + 1
            store.set_state("sequence", str(n))
            return n

        def report_config() -> None:
            cfg = runtime.config
            ts = datetime.now(UTC)
            accumulator.add(
                [
                    normalizer.normalize(
                        Reading(m, "agent", v, u, "ldt-agent configuration", MetricKind.STATIC), ts
                    )
                    for m, v, u in (
                        ("agent.telemetry_interval_ms", cfg.telemetry_interval_ms, "ms"),
                        ("agent.process_interval_ms", cfg.process_interval_ms, "ms"),
                        ("agent.collect_process_details", cfg.collect_process_details, "bool"),
                        ("agent.config_version", cfg.version, "version"),
                    )
                ]
            )

        def flush() -> bool:
            """Move everything collected since the last flush into the durable outbox."""
            health, health_events = posture.evaluate()
            accumulator.set_device_health(health)
            if health_events:
                accumulator.add_events(health_events)
            samples, procs = accumulator.drain()
            events, device_health, agent_health = accumulator.drain_extras()
            if not samples and procs is None and not events and agent_health is None:
                return False
            now = datetime.now(UTC)
            priority = accumulator.batch_priority(events)
            if not samples and procs is None and not events:
                priority = Priority.LOW  # agent health only
            batch = TelemetryBatch(
                priority=priority,
                device_id=runtime.device_id,
                agent_version=AGENT_VERSION,
                sequence=seq_next(),
                sent_at=now,
                # oldest measurement in the batch: latency figures include the batching wait
                collected_at=min((smp.timestamp for smp in samples), default=now),
                samples=samples,
                processes=procs,
                events=events,
                device_health=device_health,
                agent_health=agent_health,
            )
            store.put(batch.batch_id, batch.model_dump_json().encode(), priority.rank)
            counters["batches_created_total"] += 1
            return True

        accumulator.add_events(
            [self._lifecycle_event("agent_started", f"Agent {AGENT_VERSION} started ({self.run_mode.value})")]
        )
        report_config()

        async def publish_loop() -> None:
            last_refresh = time.monotonic()
            was_online = False
            while not stop.is_set():
                wake.clear()
                flush()
                await sync.sync_once()
                if sync.online and not was_online:
                    accumulator.resend_static()  # (re)connected: next batch carries a full keyframe
                was_online = sync.online
                if time.monotonic() - last_refresh > s.static_refresh_interval_s:
                    last_refresh = time.monotonic()
                    try:
                        await asyncio.to_thread(runtime.discover)
                        sync.mark_inventory_stale()
                    except Exception as exc:  # inventory refresh is best-effort
                        log.warning("inventory_refresh_failed", error=str(exc)[:200])
                interval = (
                    s.publish_interval_ms / 1000.0
                    if sync.failures_consecutive == 0
                    else s.offline_flush_interval_s
                )
                if wake.is_set():
                    continue  # an urgent event arrived while syncing
                waiters = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(wake.wait())]
                try:
                    await asyncio.wait(waiters, timeout=interval, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for w in waiters:
                        w.cancel()
                if wake.is_set() and not stop.is_set():
                    sync.retry_now()  # HIGH/CRITICAL: try now instead of waiting for the backoff

        async def heartbeat_loop() -> None:
            """Liveness for presence (ONLINE/STALE/OFFLINE) independent of the telemetry backlog."""
            while not stop.is_set():
                stats = store.stats()
                info: dict[str, object] = {
                    "agent_version": AGENT_VERSION,
                    "agent_id": agent_id,
                    "sent_at": datetime.now(UTC).isoformat(),
                    "run_mode": self.run_mode.value,
                    "last_sequence": int(store.get_state("sequence") or 0),
                    "queue_depth": stats.depth,
                    "queue_oldest_age_s": stats.oldest_age_s,
                    "sync_online": sync.online,
                    "api_latency_ms": client.last_latency_ms,
                    "collectors_failing": sum(1 for h in scheduler.health.values() if h.consecutive_failures),
                }
                if s.remediation_enabled:  # advertise the local allowlist (the platform never exceeds it)
                    info["remediation_actions"] = list(executor.allowed_actions)
                    info["restartable_applications"] = sorted(executor.applications)
                try:
                    info["unconfirmed_batch_ids"] = store.unconfirmed(UNCONFIRMED_PER_HEARTBEAT)
                    hb = await client.heartbeat(info)
                    await client.maybe_rotate(hb)
                    confirmed = store.confirm(list(hb.get("durable_batch_ids") or []))
                    resent = store.resend(list(hb.get("unknown_batch_ids") or []))
                    if resent:  # accepted earlier, lost by the backend before it was written
                        log.warning("resending_batches_lost_by_backend", count=resent)
                        wake.set()
                    if confirmed:
                        log.debug("batches_confirmed_durable", count=confirmed)
                    if not sync.online:
                        sync.retry_now()  # the backend answers again: drain the queue now
                        wake.set()  # ... without waiting for the offline flush interval
                except Exception as exc:  # unreachable backend is normal offline operation
                    log.debug("heartbeat_failed", error=str(exc)[:200])
                with contextlib.suppress(TimeoutError):
                    # while the backend is unreachable, probe more often so recovery is noticed fast
                    interval = s.heartbeat_interval_s if sync.online else min(s.heartbeat_interval_s, 5)
                    await asyncio.wait_for(stop.wait(), timeout=interval)

        async def config_loop() -> None:
            if s.config_poll_interval_s <= 0:
                return
            while not stop.is_set():
                data = await client.fetch_config()
                if data is not None:
                    changed = runtime.apply_config(runtime.config.merged(data))
                    if changed:
                        log.info("remote_config_applied", changed=changed, version=runtime.config.version)
                report_config()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=s.config_poll_interval_s)

        async def toast_loop() -> None:
            """Alerts routed to the Windows channel: fetch, show, acknowledge (never executes anything)."""
            if not s.toast_notifications or s.toast_poll_interval_s <= 0 or not interactive_session():
                return
            while not stop.is_set():
                if sync.online:
                    for item in await client.fetch_notifications():
                        shown = await asyncio.to_thread(
                            show_toast, str(item.get("title", "")), str(item.get("body", ""))
                        )
                        if shown:
                            await client.ack_notification(str(item["notification_id"]))
                        log.info(
                            "toast_shown" if shown else "toast_failed",
                            notification_id=item["notification_id"],
                        )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=s.toast_poll_interval_s)

        # ---------------------------------------------------------------- Phase 8: remediation
        async def refresh_hook() -> dict[str, object]:
            n = scheduler.request_all_now()
            accumulator.resend_static()
            wake.set()
            return {"collectors": n}

        async def rescan_hook() -> dict[str, object]:
            await asyncio.to_thread(runtime.discover)
            sync.mark_inventory_stale()
            accumulator.resend_static()
            wake.set()
            return {}

        async def reconnect_hook() -> dict[str, object]:
            await client.reset_connections()
            sync.retry_now()
            wake.set()
            return {}

        key_pin = KeyPin(self.data_dir / "action_key.json", s.action_public_key or None)
        executor = ActionExecutor(
            runtime.device_id,
            key_pin.current,
            ExecutionLedger(self.data_dir / "actions.json"),
            Hooks(refresh_hook, rescan_hook, reconnect_hook, self.run_mode.value),
            allowed_actions=tuple(a for a in s.remediation_actions if a in IMPLEMENTED),
            applications=parse_applications(s.restartable_applications),
            apps=WindowsAppController() if sys.platform == "win32" else None,
        )

        async def action_loop() -> None:
            """Signed, approved actions: pull, validate locally, execute, report (never a command)."""
            if not s.remediation_enabled or s.action_poll_interval_s <= 0:
                return
            while not stop.is_set():
                if sync.online and client.auth_mode == "device-token":
                    if key_pin.current() is None:
                        key_pin.trust_on_first_use(await client.fetch_action_key())
                    for envelope_data in await client.fetch_actions():
                        for execution_id, (phase, detail, data) in await executor.handle(envelope_data):
                            await client.report_action(execution_id, phase, detail, data)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=s.action_poll_interval_s)

        async def health_loop() -> None:
            while not stop.is_set():
                snapshot = monitor.snapshot()
                accumulator.set_agent_health(snapshot)
                try:
                    monitor.write(snapshot)
                except OSError as exc:
                    log.warning("health_file_write_failed", error=str(exc)[:200])
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=s.agent_health_interval_s)

        log.info(
            "agent_started",
            version=AGENT_VERSION,
            backend=client.base_url,
            auth=client.auth_mode,
            providers=[p.name for p in providers],
            queue=store.stats().depth,
        )
        try:
            await asyncio.gather(
                scheduler.run(stop),
                publish_loop(),
                config_loop(),
                health_loop(),
                heartbeat_loop(),
                toast_loop(),
                action_loop(),
            )
        finally:
            accumulator.add_events([self._lifecycle_event("agent_stopped", "Agent stopping")])
            accumulator.set_agent_health(monitor.snapshot())
            flush()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(sync.sync_once(), timeout=SHUTDOWN_SYNC_TIMEOUT_S)
            with contextlib.suppress(Exception):
                monitor.write()
            await client.close()
            stats = store.stats()
            store.close()
            await asyncio.to_thread(runtime.close)
            log.info("agent_stopped", queued=stats.depth, delivered=sync.delivered_total)

    @staticmethod
    def _lifecycle_event(kind: str, message: str) -> DeviceEvent:
        return DeviceEvent(
            type=kind,
            severity=EventSeverity.INFO,
            timestamp=datetime.now(UTC),
            source="ldt-agent",
            message=message,
        )
