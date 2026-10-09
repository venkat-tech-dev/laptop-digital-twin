"""ForecastService (Phase 5): scheduled forecasting per device and target.

    twin.window (live samples) ──┐
    TimescaleDB history (once) ──┴─> BucketSeries (bounded, per device x target)
        -> prepare (quality gates, regime trimming) -> assess (models, crossing, confidence)
        -> PredictionTracker (lifecycle) -> predictions table + prediction.* events + twin predictions.*

It never runs inside an API request or on a telemetry event: one background loop, each target on
its own cadence (``update_interval_s``). Failures are counted and logged per device/target and never
affect telemetry, the twin or anomaly detection. Nothing here can be triggered from the browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.core.config import Settings
from app.core.metrics import (
    FORECAST_ERRORS,
    FORECAST_LATENCY,
    LOW_CONFIDENCE_PREDICTIONS,
    PREDICTION_TIMING_ERROR,
    PREDICTIONS_TOTAL,
    STALE_PREDICTIONS,
)
from app.domain.events.events import DomainEvent, PredictionChanged
from app.domain.prediction.engine import (
    Assessment,
    Prediction,
    PredictionPolicy,
    PredictionTracker,
    Transition,
    assess,
)
from app.domain.prediction.preprocess import BucketSeries, prepare
from app.domain.prediction.targets import TARGETS, Target
from app.domain.twin.projection import project_fields
from app.repositories.predictions import PredictionFilter, PredictionRepository

log = structlog.get_logger("forecasting")

CONFIG_KEY = "prediction_config"
TICK_S = 15.0
#: target -> Phase-4 behavioral signal (anomaly context only)
ANOMALY_SIGNAL = {"memory": "memory", "temperature": "temperature", "cpu": "cpu", "disk": "disk_write"}
EDITABLE_TARGET_KEYS: dict[str, tuple[float, float]] = {
    "window_s": (60, 60 * 86400),
    "min_history_s": (60, 30 * 86400),
    "min_points": (3, 10_000),
    "horizon_s": (60, 730 * 86400),
    "trend_horizon_s": (60, 30 * 86400),
    "update_interval_s": (10, 86400),
    "stale_after_s": (30, 7 * 86400),
    "min_slope_per_h": (0.0, 1000.0),
}
EDITABLE_POLICY_KEYS: dict[str, tuple[float, float]] = {
    "create_min_confidence": (0.0, 1.0),
    "keep_min_confidence": (0.0, 1.0),
    "invalidate_after": (1, 100),
    "material_change": (0.0, 5.0),
    "min_publish_interval_factor": (0.0, 100.0),
}


@dataclass
class TargetState:
    series: BucketSeries
    source_key: str | None = None
    bootstrapped: bool = False
    last_eval: float = 0.0
    assessment: Assessment | None = None


@dataclass
class DeviceForecasts:
    targets: dict[str, TargetState] = field(default_factory=dict)
    unplugged_since: float | None = None
    plugged: bool | None = None
    errors: int = 0
    last_error: str | None = None


class ForecastService:
    def __init__(
        self,
        settings: Settings,
        twins: Any,
        presence: Any,
        telemetry_repo: Any,
        repo: PredictionRepository,
        settings_store: Any,
        publish: Callable[[list[DomainEvent]], Awaitable[None]],
        on_twin_changed: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self._s = settings
        self._twins = twins
        self._presence = presence
        self._telemetry = telemetry_repo
        self.repo = repo
        self._store = settings_store
        self._publish = publish
        self._on_twin_changed = on_twin_changed
        self.policy = PredictionPolicy()
        self.targets: dict[str, Target] = {t.target_id: t for t in TARGETS}
        self.tracker = PredictionTracker(self.policy)
        self._devices: dict[str, DeviceForecasts] = {}
        self.config_meta: dict[str, Any] = {"version": 0, "updated_at": None, "updated_by": None}
        self.stats = {"evaluations": 0, "eval_ms_total": 0.0, "bootstraps": 0}
        self._restored = False

    # ------------------------------------------------------------------ config
    async def load(self) -> None:
        try:
            stored = await self._store.get_setting(CONFIG_KEY)
            if stored:
                self._apply_config(stored)
            for p in await self.repo.search(None, PredictionFilter(active=True), 5000):
                self.tracker.adopt(p)
            self._restored = True
        except Exception as exc:
            FORECAST_ERRORS.labels("load").inc()
            log.warning("prediction_state_load_failed", error=str(exc)[:200])

    def _apply_config(self, stored: dict[str, Any]) -> None:
        base = {t.target_id: t for t in TARGETS}
        for tid, changes in (stored.get("targets") or {}).items():
            if tid in base:
                base[tid] = base[tid].merged(changes)
        self.targets = base
        pol = stored.get("policy") or {}
        self.policy = PredictionPolicy(**{k: v for k, v in pol.items() if k in EDITABLE_POLICY_KEYS})
        self.tracker.policy = self.policy
        self.config_meta = {k: stored.get(k) for k in ("version", "updated_at", "updated_by")}

    def config(self) -> dict[str, Any]:
        return {
            "policy": self.policy.public(),
            "targets": {tid: t.public() for tid, t in self.targets.items()},
            "editable": {
                "policy": {k: {"min": lo, "max": hi} for k, (lo, hi) in EDITABLE_POLICY_KEYS.items()},
                "target": {k: {"min": lo, "max": hi} for k, (lo, hi) in EDITABLE_TARGET_KEYS.items()}
                | {
                    "thresholds": {"type": "list"},
                    "enabled": {"type": "bool"},
                    "severity_bands_s": {"type": "list[4]"},
                },
            },
            **self.config_meta,
        }

    async def set_config(self, changes: dict[str, Any], by: str) -> dict[str, Any]:
        """Validated admin change of forecasting policy (numbers only: never code, models or training)."""
        stored = (await self._store.get_setting(CONFIG_KEY)) or {}
        targets = dict(stored.get("targets") or {})
        policy = dict(stored.get("policy") or {})
        for key, value in (changes.get("policy") or {}).items():
            if key not in EDITABLE_POLICY_KEYS:
                raise ValueError(f"{key} is not an editable prediction policy setting")
            lo, hi = EDITABLE_POLICY_KEYS[key]
            v = float(value)
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            policy[key] = int(v) if key == "invalidate_after" else v
        merged_policy = PredictionPolicy(**policy)
        if merged_policy.keep_min_confidence > merged_policy.create_min_confidence:
            raise ValueError("keep_min_confidence must not exceed create_min_confidence (hysteresis)")
        for tid, tchanges in (changes.get("targets") or {}).items():
            if tid not in self.targets:
                raise ValueError(f"unknown forecast target {tid}")
            clean = dict(targets.get(tid) or {})
            for key, value in tchanges.items():
                if key in EDITABLE_TARGET_KEYS:
                    lo, hi = EDITABLE_TARGET_KEYS[key]
                    v = float(value)
                    if not lo <= v <= hi:
                        raise ValueError(f"{tid}.{key} must be between {lo} and {hi}")
                    clean[key] = int(v) if key in ("min_points",) else v
                elif key == "enabled":
                    clean[key] = bool(value)
                elif key == "thresholds":
                    th = [float(x) for x in value]
                    lo, hi = self.targets[tid].plausible
                    if not 1 <= len(th) <= 3 or not all(lo <= x <= hi for x in th):
                        raise ValueError(f"{tid}.thresholds: 1-3 values within {lo}-{hi}")
                    clean[key] = th
                elif key == "severity_bands_s":
                    b = [int(x) for x in value]
                    if len(b) != 4 or b != sorted(b, reverse=True) or b[-1] <= 0:
                        raise ValueError(f"{tid}.severity_bands_s: 4 decreasing positive durations")
                    clean[key] = b
                else:
                    raise ValueError(f"{tid}.{key} is not editable")
            targets[tid] = clean
        new = {
            "policy": policy,
            "targets": targets,
            "version": int(stored.get("version") or 0) + 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "updated_by": by,
        }
        await self._store.set_setting(CONFIG_KEY, new, by)
        self._apply_config(new)
        log.info("prediction_config_changed", by=by, version=new["version"])
        return self.config()

    # ------------------------------------------------------------------ loop
    async def run(self, stop: asyncio.Event) -> None:
        if not self._restored:
            await self.load()
        while not stop.is_set():
            try:
                await self.evaluate_all(datetime.now(UTC))
            except Exception:
                FORECAST_ERRORS.labels("loop").inc()
                log.exception("forecast_loop_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=TICK_S)

    async def evaluate_all(self, now: datetime) -> None:
        low = stale = 0
        for device in list(self._twins.devices()):
            await self.evaluate_device(device.device_id, now)
            for st in self._devices.get(device.device_id, DeviceForecasts()).targets.values():
                if st.assessment is not None:
                    low += st.assessment.status == "LOW_CONFIDENCE"
                    stale += st.assessment.status == "STALE_DATA"
        LOW_CONFIDENCE_PREDICTIONS.set(low)
        STALE_PREDICTIONS.set(stale)

    async def evaluate_device(self, device_id: str, now: datetime, force: bool = False) -> list[Transition]:
        twin = self._twins.get(device_id)
        if twin is None:
            return []
        dev = self._devices.setdefault(device_id, DeviceForecasts())
        ts = now.timestamp()
        keys = self._source_keys(twin)
        self._track_power(twin, dev, ts)
        out: list[Transition] = []
        changed_twin = False
        for target in self.targets.values():
            st = dev.targets.get(target.target_id)
            if st is None or st.series.bucket_s != target.bucket_s:
                st = dev.targets[target.target_id] = TargetState(
                    BucketSeries(target.bucket_s, max(10, target.window_s // target.bucket_s + 4))
                )
            if not force and ts - st.last_eval < target.update_interval_s:
                continue
            st.last_eval = ts
            started = time.perf_counter()
            try:
                key = keys.get(target.field)
                if key and key != st.source_key:
                    st.source_key, st.bootstrapped = key, False
                    st.series = BucketSeries(target.bucket_s, st.series.max_buckets)
                if st.source_key and not st.bootstrapped:
                    await self._bootstrap(device_id, target, st, now)
                if st.source_key:
                    st.series.add(
                        twin.window.values_since(st.source_key, st.series.newest_t), target.plausible, ts
                    )
                a = self._assess(twin, dev, target, st, ts)
                if st.source_key is None:  # metric not seen yet (e.g. just restarted): retry next tick
                    st.last_eval = ts - target.update_interval_s + TICK_S
                prev = st.assessment.public(ts) if st.assessment else None
                st.assessment = a
                trs = self.tracker.step(device_id, a, now)
                out.extend(trs)
                changed_twin |= bool(trs) or _material(prev, a.public(ts))
            except Exception as exc:
                dev.errors += 1
                dev.last_error = f"{target.target_id}: {type(exc).__name__}"
                FORECAST_ERRORS.labels(target.target_id).inc()
                log.warning(
                    "forecast_failed", device_id=device_id, target=target.target_id, error=str(exc)[:300]
                )
                continue
            finally:
                ms = (time.perf_counter() - started) * 1000
                FORECAST_LATENCY.observe(ms)
                self.stats["evaluations"] += 1
                self.stats["eval_ms_total"] += ms
        if out:
            await self._emit(device_id, out)
        twin.predictions = self.twin_summary(device_id, ts)
        if (out or changed_twin) and self._on_twin_changed is not None:
            await self._on_twin_changed(device_id)
        return out

    # ---------------------------------------------------------------- inputs
    def _source_keys(self, twin: Any) -> dict[str, str]:
        fields = project_fields(twin.components, None, False)
        out = {}
        for t in self.targets.values():
            fv = fields.get(t.field)
            if fv is not None and fv.reading is not None and fv.reading.available:
                out[t.field] = fv.reading.key
        return out

    @staticmethod
    def _reading(twin: Any, metric: str) -> Any:
        for comp in twin.components.values():
            for r in comp.telemetry.values():
                if r.metric == metric and r.available:
                    return r
        return None

    def _track_power(self, twin: Any, dev: DeviceForecasts, ts: float) -> None:
        r = self._reading(twin, "battery.power_plugged")
        if r is None or r.numeric is None:
            return
        plugged = r.numeric >= 0.5
        if not plugged and dev.plugged is True:  # transition to battery: a new discharge regime starts
            dev.unplugged_since = r.timestamp.timestamp()
        dev.plugged = plugged

    async def _bootstrap(self, device_id: str, target: Target, st: TargetState, now: datetime) -> None:
        """Seed the series once from persisted, already aggregated history (bounded window)."""
        st.bootstrapped = True
        assert st.source_key is not None
        try:
            start = now - timedelta(seconds=target.window_s)
            hist = await self._telemetry.history(device_id, [st.source_key], start, now, target.bucket_s)
            st.series.load_history(
                (p.time.timestamp() + target.bucket_s / 2, p.avg) for p in hist.get(st.source_key, [])
            )
            self.stats["bootstraps"] += 1
            if target.target_id == "battery":
                await self._bootstrap_power(device_id, start, now)
        except Exception as exc:
            FORECAST_ERRORS.labels("bootstrap").inc()
            log.warning(
                "forecast_bootstrap_failed",
                device_id=device_id,
                target=target.target_id,
                error=str(exc)[:200],
            )

    async def _bootstrap_power(self, device_id: str, start: datetime, now: datetime) -> None:
        dev = self._devices[device_id]
        twin = self._twins.get(device_id)
        r = self._reading(twin, "battery.power_plugged") if twin else None
        if r is None:
            return
        hist = await self._telemetry.history(device_id, [r.key], start, now, 60)
        pts = hist.get(r.key, [])
        plugged_minutes = [p.time.timestamp() for p in pts if p.avg >= 0.5]
        if pts and pts[-1].avg < 0.5:
            dev.plugged = False
            dev.unplugged_since = (plugged_minutes[-1] + 60) if plugged_minutes else pts[0].time.timestamp()

    def _assess(
        self, twin: Any, dev: DeviceForecasts, target: Target, st: TargetState, ts: float
    ) -> Assessment:
        regime_start = None
        not_applicable = None
        if not target.enabled:
            not_applicable = "forecasting disabled for this metric by configuration"
        elif st.source_key is None:
            not_applicable = "this metric is not reported by the device"
        elif target.target_id == "battery":
            state = self._reading(twin, "battery.charging_state")
            if dev.plugged or (state is not None and str(state.value) in ("charging", "full")):
                not_applicable = "on AC power / charging: no discharge forecast"
            regime_start = dev.unplugged_since
        prepared = prepare(st.series, target, ts, regime_start)
        sig = ANOMALY_SIGNAL.get(target.target_id)
        related = [
            {
                "title": a.title,
                "type": a.anomaly_type.value,
                "level": a.effective_level.value,
                "confidence": a.confidence,
                "anomaly_id": a.anomaly_id,
            }
            for a in [*twin.anomalies.active.values(), *getattr(twin, "behavior_active", [])]
            if sig and a.signal_id == sig
        ]
        volatile = any(r["type"] == "volatility_anomaly" for r in related)
        return assess(
            target,
            prepared,
            ts,
            self.policy,
            not_applicable=not_applicable,
            volatile_anomaly=volatile,
            anomaly_context=related or None,
        )

    # --------------------------------------------------------------- outputs
    async def _emit(self, device_id: str, trs: list[Transition]) -> None:
        events: list[DomainEvent] = []
        for t in trs:
            p = t.prediction
            PREDICTIONS_TOTAL.labels(t.kind, p.target_id).inc()
            if t.kind == "confirmed" and p.timing_error_s is not None:
                PREDICTION_TIMING_ERROR.labels(p.target_id).observe(abs(p.timing_error_s))
            log.info(
                "prediction_" + t.kind,
                device_id=device_id,
                target=p.target_id,
                model=p.model_type,
                model_version=p.model_version,
                status=p.status,
                reason=p.reason,
                time_to_threshold_s=p.time_to_threshold_s,
                confidence=p.confidence,
            )
            try:
                await self.repo.upsert(p)
            except Exception as exc:
                FORECAST_ERRORS.labels("persist").inc()
                log.warning("prediction_persist_failed", device_id=device_id, error=str(exc)[:200])
            events.append(
                PredictionChanged(device_id=device_id, kind=t.kind, prediction=_wire(p), changed=t.changed)
            )
        await self._publish(events)

    def twin_summary(self, device_id: str, ts: float) -> dict[str, Any]:
        dev = self._devices.get(device_id)
        active = {p.target_id: p for p in self.tracker.active(device_id)}
        out: dict[str, Any] = {}
        for tid, st in dev.targets.items() if dev else []:
            a = st.assessment
            if a is None:
                continue
            pub = a.public(ts)
            p = active.get(tid)
            item = {
                "status": pub["status"],
                "health": pub["health"],
                "reason": pub["reason"],
                "current_value": pub["current_value"],
                "unit": pub["unit"],
                "title": pub["title"],
                "forecast": pub["forecast"],
                "model": pub["model"],
            }
            if p is not None:  # the published (smoothed, lifecycle-managed) prediction wins
                item.update(
                    {
                        "prediction_id": p.prediction_id,
                        "prediction_status": p.status,
                        "threshold": p.threshold,
                        "crossing_at": p.crossing_at.isoformat() if p.crossing_at else None,
                        "crossing_earliest": p.crossing_earliest.isoformat() if p.crossing_earliest else None,
                        "crossing_latest": p.crossing_latest.isoformat() if p.crossing_latest else None,
                        "confidence": p.confidence,
                        "confidence_band": p.confidence_band,
                        "severity": p.severity,
                        "statement": p.statement,
                    }
                )
            out[tid] = item
        return out

    # ------------------------------------------------------------ read side
    def current(self, device_id: str) -> dict[str, Any]:
        ts = time.time()
        dev = self._devices.get(device_id)
        items = []
        active = {p.target_id: p for p in self.tracker.active(device_id)}
        for tid, target in self.targets.items():
            st = dev.targets.get(tid) if dev else None
            a = st.assessment if st else None
            items.append(
                {
                    **(
                        a.public(ts)
                        if a
                        else {
                            "target_id": tid,
                            "title": target.title,
                            "status": "INSUFFICIENT_HISTORY",
                            "reason": "not evaluated yet",
                            "health": "UNAVAILABLE",
                        }
                    ),
                    "prediction": _wire(active[tid]) if tid in active else None,
                    "source_key": st.source_key if st else None,
                }
            )
        return {
            "device_id": device_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "targets": items,
            "wording": "Estimates based on recent trends; not guarantees.",
            "errors": {"count": dev.errors, "last": dev.last_error} if dev else {"count": 0, "last": None},
        }

    def detail_curve(self, device_id: str, target_id: str) -> dict[str, Any] | None:
        dev = self._devices.get(device_id)
        st = dev.targets.get(target_id) if dev else None
        if st is None or st.assessment is None:
            return None
        a = st.assessment
        return {
            "history": [[round(t, 1), round(v, 3)] for t, v in a.history],
            "forecast": a.curve,
            "context": a.context,
        }

    def find_active(self, prediction_id: str) -> Prediction | None:
        for device in self._twins.devices():
            for p in self.tracker.active(device.device_id):
                if p.prediction_id == prediction_id:
                    return p
        return None

    def status(self) -> dict[str, Any]:
        n = self.stats["evaluations"] or 1
        return {
            "devices": len(self._devices),
            "evaluations": self.stats["evaluations"],
            "avg_eval_ms": round(self.stats["eval_ms_total"] / n, 3),
            "bootstraps": self.stats["bootstraps"],
            "lifecycle": dict(self.tracker.stats),
        }


def _wire(p: Prediction) -> dict[str, Any]:
    d = p.to_dict()
    ev = dict(d.get("evidence") or {})
    ev.pop("curve", None)  # the forecast curve is served by the detail endpoint, not every event
    obs = dict(ev.get("observed") or {})
    obs.pop("history", None)
    ev["observed"] = obs
    d["evidence"] = ev
    return d


def _material(prev: dict[str, Any] | None, new: dict[str, Any]) -> bool:
    if prev is None:
        return True
    return any(
        prev.get(k) != new.get(k) for k in ("status", "health", "severity", "confidence_band", "model")
    )
