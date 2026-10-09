"""IntelligenceService: device-specific behavioral anomaly detection on live twin data.

Data flow (no new transport, no raw telemetry is ever modified):

    twin.window (live samples)  --observe (quality gates)-->  observations
    device baselines / model    --BehaviorEngine.evaluate-->  transitions
    transitions --> anomalies table (durable) + anomaly.* WebSocket events + twin alerts summary

Training (scheduled, never triggered from the browser): every ``retrain_interval_s`` per device, from
persisted 1-minute history of the last ``baseline_history_days``. Intervals covered by confident
anomalies are cut out first (no contamination), and only data before the training time is used (no
future leakage). The Isolation Forest is trained on the same clean minutes, versioned and stored as
JSON. Any failure degrades gracefully: multivariate -> statistical -> safety thresholds only.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.core.config import Settings
from app.core.metrics import (
    ACTIVE_ANOMALIES,
    ANOMALIES_DETECTED,
    ANOMALIES_RESOLVED,
    ANOMALIES_SUPPRESSED,
    BASELINE_TRAINING,
    DETECTOR_ERRORS,
    DETECTOR_LATENCY,
    FALSE_POSITIVE_FEEDBACK,
    MODEL_TRAINING,
)
from app.domain.anomalies.baseline import (
    BaselineStatus,
    SignalBaseline,
    build_signal_baseline,
    fleet_baseline,
)
from app.domain.anomalies.behavior import BehaviorEngine, Transition
from app.domain.anomalies.iforest import MultivariateModel, select_features, train_model
from app.domain.anomalies.models import Anomaly, AnomalyType, Level
from app.domain.anomalies.observation import Observation, QualityIssue, observe
from app.domain.anomalies.policy import AnomalyPolicy
from app.domain.anomalies.signals import RELATIONS, SIGNALS, SIGNALS_BY_ID
from app.domain.events.events import AnomalyChanged, DomainEvent
from app.domain.twin.projection import project_fields
from app.repositories.base import AnomalyFilter, EventRepository, TelemetryRepository
from app.repositories.intelligence import IntelligenceRepository, StoredBaseline

log = structlog.get_logger("intelligence")

CONFIG_KEY = "anomaly_config"
#: Admin-editable policy keys and their allowed ranges (everything else is deployment config).
EDITABLE: dict[str, tuple[float, float]] = {
    "z_trigger": (2.0, 10.0),
    "z_recover": (0.5, 5.0),
    "z_trigger_cold": (3.0, 15.0),
    "persistence_s": (0.0, 3600.0),
    "recovery_s": (0.0, 3600.0),
    "cooldown_s": (0.0, 86400.0),
    "shift_z": (1.5, 10.0),
    "volatility_ratio": (1.5, 20.0),
    "correlation_window_s": (10.0, 1800.0),
    "expire_after_s": (60.0, 86400.0),
    "contamination_min_confidence": (0.0, 1.0),
    "iforest_threshold_quantile": (0.9, 0.9999),
}
EDITABLE_BOOL = ("iforest_enabled", "process_context")
DETECTORS = ("robust_z", "quantile", "ewma_shift", "iforest")
CONNECTED = ("ONLINE",)
TRAINING_PAUSE_S = 0.5


@dataclass
class DeviceIntel:
    baselines: dict[str, StoredBaseline] = field(default_factory=dict)
    model: MultivariateModel | None = None
    loaded: bool = False
    trained_at: float = 0.0  # monotonic
    last_training: dict[str, Any] | None = None
    source_keys: dict[str, str] = field(default_factory=dict)
    keys_resolved_at: float = 0.0
    quality: dict[str, str] = field(default_factory=dict)
    last_evaluated: datetime | None = None
    muted: dict[str, float] = field(default_factory=dict)  # rule_id -> epoch until
    errors: int = 0
    last_error: str | None = None


class IntelligenceService:
    def __init__(
        self,
        settings: Settings,
        twins: Any,  # DigitalTwinService
        presence: Any,  # PresenceService
        telemetry_repo: TelemetryRepository,
        event_repo: EventRepository,
        repo: IntelligenceRepository,
        settings_store: Any,  # AdminRepository (get_setting / set_setting)
        publish: Callable[[list[DomainEvent]], Awaitable[None]],
        record: Callable[[Anomaly], None],
        on_twin_changed: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self._s = settings
        self._twins = twins
        self._presence = presence
        self._telemetry = telemetry_repo
        self._events = event_repo
        self._repo = repo
        self._store = settings_store
        self._publish = publish
        self._record = record
        self._on_twin_changed = on_twin_changed
        self.base_policy = AnomalyPolicy(
            eval_interval_s=settings.anomaly_eval_interval_s,
            retrain_interval_s=settings.anomaly_retrain_interval_s,
            model_retrain_interval_s=settings.anomaly_model_retrain_interval_s,
            baseline_history_days=settings.anomaly_baseline_history_days,
            z_trigger=settings.anomaly_z_trigger,
            persistence_s=settings.anomaly_persistence_s,
            cooldown_s=settings.anomaly_cooldown_s,
            iforest_enabled=settings.anomaly_iforest_enabled,
            process_context=settings.anomaly_process_context and settings.twin_show_process_names,
        )
        self.policy = self.base_policy
        self.config_meta: dict[str, Any] = {"version": 0, "updated_at": None, "updated_by": None}
        self.engine = BehaviorEngine(self.policy)
        self._devices: dict[str, DeviceIntel] = {}
        self._fleet: dict[str, SignalBaseline] = {}
        self._train_queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self.stats = {"evaluations": 0, "trainings": 0, "training_failures": 0, "eval_ms_total": 0.0}

    # ------------------------------------------------------------------ config
    async def load_config(self) -> None:
        try:
            stored = await self._store.get_setting(CONFIG_KEY)
        except Exception as exc:
            log.warning("anomaly_config_load_failed", error=str(exc)[:200])
            return
        if stored:
            self._apply_config(stored)

    def _apply_config(self, stored: dict[str, Any]) -> None:
        changes = dict(stored.get("policy") or {})
        if "process_context" in changes and not self._s.twin_show_process_names:
            changes["process_context"] = False  # enterprise policy wins over the anomaly config
        self.policy = self.base_policy.merged(changes)
        self.engine.set_policy(self.policy)
        self.config_meta = {k: stored.get(k) for k in ("version", "updated_at", "updated_by")}

    def config(self) -> dict[str, Any]:
        return {
            "policy": self.policy.public(),
            "editable": {k: {"min": lo, "max": hi} for k, (lo, hi) in EDITABLE.items()}
            | {k: {"type": "bool"} for k in EDITABLE_BOOL}
            | {"enabled_detectors": {"options": list(DETECTORS)}},
            **self.config_meta,
        }

    async def set_config(self, changes: dict[str, Any], by: str) -> dict[str, Any]:
        """Validated admin change of detection policy (data only: never code, never a training run)."""
        clean: dict[str, Any] = {}
        for key, value in changes.items():
            if key in EDITABLE:
                lo, hi = EDITABLE[key]
                v = float(value)
                if not lo <= v <= hi:
                    raise ValueError(f"{key} must be between {lo} and {hi}")
                clean[key] = v
            elif key in EDITABLE_BOOL:
                clean[key] = bool(value)
            elif key == "enabled_detectors":
                ds = [str(d) for d in value]
                if any(d not in DETECTORS for d in ds):
                    raise ValueError(f"enabled_detectors: allowed values are {', '.join(DETECTORS)}")
                clean[key] = sorted(set(ds), key=DETECTORS.index)
            else:
                raise ValueError(f"{key} is not an editable anomaly setting")
        merged = {**self.policy.public(), **clean}
        if merged["z_recover"] >= merged["z_trigger"]:
            raise ValueError("z_recover must be lower than z_trigger (hysteresis)")
        current = {k: v for k, v in (self.policy.public()).items() if k in EDITABLE or k in EDITABLE_BOOL}
        current["enabled_detectors"] = list(self.policy.enabled_detectors)
        stored = {
            "policy": {**current, **clean},
            "version": int(self.config_meta.get("version") or 0) + 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "updated_by": by,
        }
        await self._store.set_setting(CONFIG_KEY, stored, by)
        self._apply_config(stored)
        log.info("anomaly_config_changed", by=by, keys=sorted(clean), version=stored["version"])
        return self.config()

    # ------------------------------------------------------------- lifecycle
    async def run(self, stop: asyncio.Event) -> None:
        trainer = asyncio.create_task(self._trainer(stop), name="anomaly_trainer")
        try:
            while not stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.policy.eval_interval_s)
                if stop.is_set():
                    break
                if trainer.done():  # the trainer died: let the supervisor restart the service
                    raise RuntimeError(f"anomaly_trainer stopped: {trainer.exception()!r}")
                try:
                    await self.evaluate_all(datetime.now(UTC))
                except Exception:
                    DETECTOR_ERRORS.labels("evaluate").inc()
                    log.exception("anomaly_evaluation_failed")
        finally:
            trainer.cancel()
            await asyncio.gather(trainer, return_exceptions=True)

    async def _trainer(self, stop: asyncio.Event) -> None:
        """One training at a time (bounded CPU/DB load); devices are queued by the evaluation loop."""
        while not stop.is_set():
            device_id = await self._train_queue.get()
            self._queued.discard(device_id)
            try:
                await self.train_device(device_id, datetime.now(UTC))
            except Exception as exc:
                self.stats["training_failures"] += 1
                DETECTOR_ERRORS.labels("training").inc()
                intel = self._devices.setdefault(device_id, DeviceIntel())
                intel.errors += 1
                intel.last_error = f"training failed: {type(exc).__name__}"
                intel.trained_at = time.monotonic()  # back off until the next retrain interval
                log.warning("anomaly_training_failed", device_id=device_id, error=str(exc)[:300])
            # pacing: training never monopolises the event loop / database in a burst
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=TRAINING_PAUSE_S)

    def _trainable(self, twin: Any, intel: DeviceIntel, now: datetime) -> bool:
        """A device seen for less than ``cold_min_samples`` minutes cannot have a baseline yet:
        training it would only cost database and CPU time (a fleet of new devices = training storm)."""
        if intel.baselines:
            return True
        first = twin.device.first_seen
        return first is None or (now - first).total_seconds() >= self.policy.cold_min_samples * 60

    def _schedule_training(self, device_id: str) -> None:
        if device_id not in self._queued:
            self._queued.add(device_id)
            self._train_queue.put_nowait(device_id)

    async def _ensure_loaded(self, device_id: str) -> DeviceIntel:
        intel = self._devices.get(device_id)
        if intel is None:
            intel = self._devices[device_id] = DeviceIntel()
        if not intel.loaded:
            intel.loaded = True
            try:
                intel.baselines = await self._repo.load_baselines(device_id)
                intel.model = await self._repo.active_model(device_id)
                active = await self._events.search_anomalies(device_id, AnomalyFilter(status="active"), 100)
                for a in active:
                    if a.anomaly_type is not AnomalyType.THRESHOLD:
                        self.engine.adopt(a)
            except Exception as exc:
                DETECTOR_ERRORS.labels("load").inc()
                log.warning("anomaly_state_load_failed", device_id=device_id, error=str(exc)[:200])
            if intel.baselines:
                self._refresh_fleet()
                trained = [
                    s.baseline.trained_until for s in intel.baselines.values() if s.baseline.trained_until
                ]
                if trained:  # resume the retraining schedule instead of retraining on every restart
                    age = (datetime.now(UTC) - max(trained)).total_seconds()
                    interval = self.policy.retrain_interval_s
                    # restored fleets are spread over the interval instead of retraining together
                    jitter = random.uniform(0.0, interval * 0.25)  # noqa: S311 - scheduling, not security
                    intel.trained_at = time.monotonic() - max(0.0, min(age, interval)) + jitter
        return intel

    # ------------------------------------------------------------- evaluation
    async def evaluate_all(self, now: datetime) -> None:
        for device in list(self._twins.devices()):
            await self.evaluate_device(device.device_id, now)
        self._export_gauges()

    async def evaluate_device(self, device_id: str, now: datetime) -> list[Transition]:
        twin = self._twins.get(device_id)
        if twin is None:
            return []
        intel = await self._ensure_loaded(device_id)
        mono = time.monotonic()
        if mono - intel.trained_at >= self.policy.retrain_interval_s and self._trainable(twin, intel, now):
            intel.trained_at = mono  # queued once per interval
            self._schedule_training(device_id)
        started = time.perf_counter()
        try:
            transitions = self._evaluate(twin, intel, now)
        except Exception as exc:
            intel.errors += 1
            intel.last_error = f"evaluation failed: {type(exc).__name__}"
            DETECTOR_ERRORS.labels("evaluate").inc()
            log.warning("anomaly_device_evaluation_failed", device_id=device_id, error=str(exc)[:300])
            return []
        elapsed = (time.perf_counter() - started) * 1000
        DETECTOR_LATENCY.observe(elapsed)
        self.stats["evaluations"] += 1
        self.stats["eval_ms_total"] += elapsed
        intel.last_evaluated = now
        if transitions:
            await self._emit(twin, transitions)
        return transitions

    def _resolve_keys(self, twin: Any, intel: DeviceIntel) -> dict[str, str]:
        """Twin field -> concrete reading key on this device; sticky to the key the baseline learned."""
        mono = time.monotonic()
        if intel.source_keys and mono - intel.keys_resolved_at < 60:
            return intel.source_keys
        fields = project_fields(twin.components, None, False)
        keys: dict[str, str] = {}
        for sig in SIGNALS:
            stored = intel.baselines.get(sig.signal_id)
            if stored is not None and stored.source_key and twin.window.latest(stored.source_key) is not None:
                keys[sig.signal_id] = stored.source_key
                continue
            fv = fields.get(sig.field)
            if fv is not None and fv.reading is not None:
                keys[sig.signal_id] = fv.reading.key
        intel.source_keys, intel.keys_resolved_at = keys, mono
        return keys

    def _evaluate(self, twin: Any, intel: DeviceIntel, now: datetime) -> list[Transition]:
        p = self.policy
        device_id = twin.device.device_id
        connected = self._presence.presence_of(device_id) in CONNECTED
        ts = now.timestamp()
        keys = self._resolve_keys(twin, intel)
        observations: dict[str, Observation | None] = {}
        baselines: dict[str, SignalBaseline] = {}
        for sig in SIGNALS:
            key = keys.get(sig.signal_id)
            if key is None:
                intel.quality[sig.signal_id] = QualityIssue.NO_DATA.value
                continue
            reading = _reading(twin, key)
            interval = (reading.interval_s if reading is not None and reading.interval_s else None) or 5.0
            live_limit = interval * 3 + self._s.twin_publish_wait_s + self._s.twin_freshness_grace_s
            points = twin.window.values_since(key, ts - p.observation_window_s)
            obs, issue = observe(
                sig,
                points,
                ts,
                p.observation_window_s,
                interval,
                live_limit,
                p.min_window_coverage,
                connected,
            )
            intel.quality[sig.signal_id] = issue.value
            observations[sig.signal_id] = obs
            stored = intel.baselines.get(sig.signal_id)
            if stored is not None and stored.baseline.status is not BaselineStatus.COLD:
                baselines[sig.signal_id] = stored.baseline
            elif sig.signal_id in self._fleet:
                baselines[sig.signal_id] = self._fleet[sig.signal_id]
        muted = frozenset(k for k, until in intel.muted.items() if until > ts)
        threshold_signals = frozenset(
            a.signal_id for a in twin.anomalies.active.values() if a.signal_id is not None
        )
        return self.engine.evaluate(
            device_id,
            now,
            observations,
            baselines,
            intel.model,
            threshold_signals=threshold_signals,
            muted_keys=muted,
            processes=self._process_context(twin),
        )

    def _process_context(self, twin: Any) -> list[dict[str, Any]] | None:
        if not self.policy.process_context or not twin.processes:
            return None
        procs = twin.processes.get("processes") or []
        top = sorted(procs, key=lambda p: -(p.get("cpu_percent") or 0.0))[:3]
        return [
            {
                "name": p.get("name"),
                "cpu_percent": p.get("cpu_percent"),
                "memory_percent": p.get("memory_percent"),
            }
            for p in top
        ]

    async def _emit(self, twin: Any, transitions: list[Transition]) -> None:
        device_id = twin.device.device_id
        events: list[DomainEvent] = []
        for t in transitions:
            a = t.anomaly
            kind = a.anomaly_type.value
            if t.kind == "detected":
                ANOMALIES_DETECTED.labels(kind, a.effective_level.value).inc()
                log.info(
                    "anomaly_detected",
                    device_id=device_id,
                    rule_id=a.rule_id,
                    level=a.effective_level.value,
                    confidence=a.confidence,
                )
            elif t.kind in ("resolved", "expired"):
                ANOMALIES_RESOLVED.labels(kind, t.kind).inc()
                twin.anomalies_recent.appendleft(a)
                log.info("anomaly_closed", device_id=device_id, rule_id=a.rule_id, how=t.kind)
            elif t.kind == "suppressed":
                ANOMALIES_SUPPRESSED.labels(kind).inc()
                if a.occurrences > 1:
                    continue  # the first suppression is recorded; repeats are counted only
            self._record(a)
            if t.kind != "suppressed":
                ws_kind = "resolved" if t.kind == "expired" else t.kind
                events.append(
                    AnomalyChanged(device_id=device_id, kind=ws_kind, anomaly=a.to_dict(), changed=t.changed)
                )
        twin.behavior_active = self.engine.active(device_id)
        if events:
            await self._publish(events)
        if self._on_twin_changed is not None:
            await self._on_twin_changed(device_id)

    def _export_gauges(self) -> None:
        counts: dict[tuple[str, str], int] = {}
        for device in self._twins.devices():
            twin = self._twins.get(device.device_id)
            if twin is None:
                continue
            for a in [*twin.anomalies.active.values(), *twin.behavior_active]:
                k = (a.anomaly_type.value, a.effective_level.value)
                counts[k] = counts.get(k, 0) + 1
        for t in AnomalyType:
            for lv in Level:
                ACTIVE_ANOMALIES.labels(t.value, lv.value).set(counts.get((t.value, lv.value), 0))

    # --------------------------------------------------------------- training
    async def train_device(self, device_id: str, now: datetime) -> dict[str, Any]:
        """Learn baselines (+ Isolation Forest) for one device from clean persisted history."""
        p = self.policy
        intel = await self._ensure_loaded(device_id)
        twin = self._twins.get(device_id)
        keys = self._resolve_keys(twin, intel) if twin is not None else dict(intel.source_keys)
        if not keys:
            return {"device_id": device_id, "skipped": "no telemetry keys yet"}
        start = now - timedelta(days=p.baseline_history_days)
        t0 = time.perf_counter()
        hist = await self._telemetry.history(
            device_id, sorted(set(keys.values())), start, now, p.training_bucket_s
        )
        exclusions = await self._exclusions(device_id, start, now)
        version = now.strftime("%Y%m%dT%H%M%SZ")
        results: dict[str, Any] = {}
        minute_rows: dict[str, dict[int, float]] = {}
        for sig in SIGNALS:
            key = keys.get(sig.signal_id)
            if key is None:
                continue
            points = [(h.time, h.avg) for h in hist.get(key, []) if h.time < now]  # no future data
            excl = exclusions.get(sig.signal_id, []) + exclusions.get("*", [])
            baseline = await asyncio.to_thread(
                build_signal_baseline, sig.signal_id, points, excl, p, f"{sig.signal_id}-{version}", now
            )
            stored = StoredBaseline(baseline, key)
            await self._repo.save_baseline(device_id, stored)
            intel.baselines[sig.signal_id] = stored
            results[sig.signal_id] = {
                "status": baseline.status.value,
                "samples": baseline.sample_count,
                "excluded": baseline.excluded_count,
            }
            if sig.multivariate and baseline.status is not BaselineStatus.COLD:
                ex = [(a.timestamp(), b.timestamp()) for a, b in excl]
                minute_rows[sig.signal_id] = {
                    int(t.timestamp()) // 60: v
                    for t, v in points
                    if not any(a <= t.timestamp() <= b for a, b in ex)
                }
        BASELINE_TRAINING.observe(time.perf_counter() - t0)
        model_info = await self._train_model(device_id, intel, minute_rows, start, now)
        intel.trained_at = time.monotonic()
        intel.last_training = {
            "at": now.isoformat(),
            "signals": results,
            "model": model_info,
            "duration_s": round(time.perf_counter() - t0, 3),
        }
        self.stats["trainings"] += 1
        self._refresh_fleet()
        log.info(
            "anomaly_training_done",
            device_id=device_id,
            statuses={k: v["status"] for k, v in results.items()},
            model=model_info.get("model_id"),
        )
        return intel.last_training

    async def _exclusions(
        self, device_id: str, start: datetime, end: datetime
    ) -> dict[str, list[tuple[datetime, datetime]]]:
        """Confident anomaly intervals per signal ('*' = all signals) to cut out of training."""
        p = self.policy
        out: dict[str, list[tuple[datetime, datetime]]] = {}
        try:
            items = await self._events.search_anomalies(device_id, AnomalyFilter(since=start), 2000)
        except Exception as exc:
            log.warning("anomaly_exclusions_unavailable", device_id=device_id, error=str(exc)[:200])
            return out
        for a in items:
            if (a.feedback or {}).get("verdict") == "false_positive":
                continue  # an operator said this was normal behavior: keep it in the baseline
            if (a.confidence or 0.0) < p.contamination_min_confidence or a.lifecycle.value == "SUPPRESSED":
                continue
            sid = "*" if a.anomaly_type is AnomalyType.MULTIVARIATE else a.signal_id
            if sid is None:
                continue
            out.setdefault(sid, []).append((a.started_at, a.resolved_at or end))
        return out

    async def _train_model(
        self,
        device_id: str,
        intel: DeviceIntel,
        minute_rows: dict[str, dict[int, float]],
        start: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        p = self.policy
        if not p.iforest_enabled:
            return {"skipped": "disabled"}
        if intel.model is not None:  # the model retrains less often than baselines (cost)
            age = (now - datetime.fromisoformat(intel.model.trained_until)).total_seconds()
            if age < p.model_retrain_interval_s:
                return {"kept": intel.model.model_id, "age_s": round(age)}
        if len(minute_rows) < 2:
            return {"skipped": "fewer than two signals with a baseline"}
        features, common = select_features(minute_rows, p.iforest_min_train_samples)
        if not common:
            return {"skipped": f"< {p.iforest_min_train_samples} complete minutes for two or more signals"}
        rows = [[minute_rows[f][m] for f in features] for m in common]
        floors = [SIGNALS_BY_ID[f].min_scale for f in features]
        version = (intel.model.version + 1) if intel.model is not None else 1
        t0 = time.perf_counter()
        model = await asyncio.to_thread(
            train_model,
            device_id,
            version,
            features,
            rows,
            floors,
            start.isoformat(),
            now.isoformat(),
            p.iforest_trees,
            p.iforest_sample_size,
            p.iforest_threshold_quantile,
            p.iforest_seed,
            relations=RELATIONS,
        )
        MODEL_TRAINING.observe(time.perf_counter() - t0)
        await self._repo.save_model(model)
        intel.model = model
        return {
            "model_id": model.model_id,
            "version": model.version,
            "n_train": model.n_train,
            "features": features,
            "threshold": round(model.threshold, 4),
        }

    def _refresh_fleet(self) -> None:
        now = datetime.now(UTC)
        for sig in SIGNALS:
            per_device = [
                d.baselines[sig.signal_id].baseline
                for d in self._devices.values()
                if sig.signal_id in d.baselines
            ]
            fb = fleet_baseline(sig.signal_id, per_device, f"fleet-{sig.signal_id}-{now:%Y%m%dT%H}", now)
            if fb is not None:
                self._fleet[sig.signal_id] = fb

    # ---------------------------------------------------------------- read side
    def detection_mode(self, intel: DeviceIntel | None) -> dict[str, Any]:
        """What protects this device right now (graceful degradation is visible, not silent)."""
        if intel is None:
            return {"mode": "thresholds", "reason": "not evaluated yet"}
        usable = {sid for sid, s in intel.baselines.items() if s.baseline.status is not BaselineStatus.COLD}
        if self.policy.iforest_enabled and intel.model is not None and usable:
            return {"mode": "multivariate", "reason": "device baselines and an Isolation Forest model"}
        if usable:
            return {"mode": "statistical", "reason": "device baselines (no multivariate model yet)"}
        if self._fleet:
            return {"mode": "fleet_baseline", "reason": "cold start: fleet baseline with a stricter trigger"}
        return {"mode": "thresholds", "reason": "no baseline yet: safety thresholds only"}

    async def baseline(self, device_id: str) -> dict[str, Any]:
        intel = await self._ensure_loaded(device_id)
        signals = []
        for sig in SIGNALS:
            stored = intel.baselines.get(sig.signal_id)
            item: dict[str, Any] = {
                "signal_id": sig.signal_id,
                "title": sig.title,
                "unit": sig.unit,
                "field": sig.field,
                "data_quality": intel.quality.get(sig.signal_id),
            }
            if stored is None:
                fb = self._fleet.get(sig.signal_id)
                item.update(
                    status="COLD", source="fleet" if fb else None, baseline=fb.public() if fb else None
                )
            else:
                item.update(
                    status=stored.baseline.status.value,
                    source="device",
                    source_key=stored.source_key,
                    baseline=stored.baseline.public(),
                )
            signals.append(item)
        try:
            models = await self._repo.list_models(device_id)
        except Exception:
            models = []
        return {
            "device_id": device_id,
            "detection": self.detection_mode(intel),
            "last_training": intel.last_training,
            "signals": signals,
            "models": models,
            "policy": {
                k: getattr(self.policy, k)
                for k in (
                    "baseline_history_days",
                    "cold_min_samples",
                    "stable_min_span_days",
                    "min_context_samples",
                    "retrain_interval_s",
                    "contamination_min_confidence",
                )
            },
        }

    def baseline_bounds(self, device_id: str, ts: datetime) -> dict[str, tuple[float, float, str]]:
        """signal -> (median, p95, baseline status) for the hour/day-type context of ``ts`` (Phase 7
        diagnosis reads the learned "usual range" from here; COLD signals are omitted)."""
        intel = self._devices.get(device_id)
        out: dict[str, tuple[float, float, str]] = {}
        for sid, stored in intel.baselines.items() if intel else []:
            ctx = stored.baseline.for_time(ts, self.policy.min_context_samples)
            if ctx is not None and ctx.stats.count > 0:
                out[sid] = (ctx.stats.median, ctx.stats.p95, stored.baseline.status.value)
        return out

    def summary(self, device_id: str) -> dict[str, Any]:
        twin = self._twins.get(device_id)
        intel = self._devices.get(device_id)
        active = active_anomalies(twin) if twin is not None else []
        by_level = {lv.value: 0 for lv in Level}
        by_type = {t.value: 0 for t in AnomalyType}
        for a in active:
            by_level[a.effective_level.value] += 1
            by_type[a.anomaly_type.value] += 1
        highest = max((a.effective_level for a in active), key=lambda lv: lv.rank, default=None)
        return {
            "device_id": device_id,
            "active_count": len(active),
            "highest_severity": highest.value if highest else None,
            "by_level": by_level,
            "by_type": by_type,
            "recent": [a.to_dict() for a in list(twin.anomalies_recent)[:5]] if twin is not None else [],
            "detection": self.detection_mode(intel),
            "baseline_status": {
                sid: s.baseline.status.value for sid, s in (intel.baselines.items() if intel else [])
            },
            "data_quality": dict(intel.quality) if intel else {},
            "last_evaluated": intel.last_evaluated.isoformat() if intel and intel.last_evaluated else None,
            "errors": {"count": intel.errors, "last": intel.last_error}
            if intel
            else {"count": 0, "last": None},
        }

    def find_active(self, anomaly_id: str) -> Anomaly | None:
        for device in self._twins.devices():
            for a in self.engine.active(device.device_id):
                if a.anomaly_id == anomaly_id:
                    return a
        return None

    def acknowledge(self, anomaly: Anomaly) -> None:
        self.engine.acknowledge(anomaly.device_id, anomaly.anomaly_id)

    async def feedback(self, anomaly: Anomaly, verdict: str, by: str, note: str | None) -> dict[str, Any]:
        """Operator verdict. A false positive mutes the same detector key on this device for one
        cooldown and keeps the interval *in* the next baseline (it was normal behavior)."""
        fb = {
            "verdict": verdict,
            "by": by,
            "note": (note or "")[:500] or None,
            "at": datetime.now(UTC).isoformat(),
        }
        anomaly.feedback = fb
        await self._events.set_anomaly_feedback(anomaly.anomaly_id, fb)
        if verdict == "false_positive":
            FALSE_POSITIVE_FEEDBACK.labels(anomaly.anomaly_type.value).inc()
            if anomaly.anomaly_type is not AnomalyType.THRESHOLD:
                intel = self._devices.setdefault(anomaly.device_id, DeviceIntel())
                intel.muted[anomaly.rule_id] = time.time() + max(self.policy.cooldown_s, 3600.0)
        log.info("anomaly_feedback", anomaly_id=anomaly.anomaly_id, verdict=verdict, by=by)
        return fb

    def status(self) -> dict[str, Any]:
        n = self.stats["evaluations"] or 1
        return {
            "devices": len(self._devices),
            "fleet_signals": sorted(self._fleet),
            "evaluations": self.stats["evaluations"],
            "avg_eval_ms": round(self.stats["eval_ms_total"] / n, 3),
            "trainings": self.stats["trainings"],
            "training_failures": self.stats["training_failures"],
            "training_queue": self._train_queue.qsize(),
            "engine": dict(self.engine.stats),
        }


def active_anomalies(twin: Any) -> list[Anomaly]:
    """Threshold + behavioral active anomalies of one twin, newest first."""
    items = [*twin.anomalies.active.values(), *twin.behavior_active]
    return sorted(items, key=lambda a: a.started_at, reverse=True)


def _reading(twin: Any, key: str) -> Any:
    for comp in twin.components.values():
        r = comp.telemetry.get(key)
        if r is not None:
            return r
    return None


__all__ = ["DeviceIntel", "IntelligenceService", "active_anomalies"]
