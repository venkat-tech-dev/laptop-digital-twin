"""Behavioral anomaly engine: detectors -> lifecycle -> correlation -> evidence (deterministic).

The engine is pure: the caller passes ``now``, the observations (after data-quality checks), the
device's baselines and (optionally) its multivariate model. Nothing here reads a clock, a database or
the network, so a replay of recorded telemetry produces exactly the same anomalies as the live path.

Lifecycle of one anomaly key (``rule_id``: one per signal and kind):

    normal --abnormal--> pending --abnormal for persistence_s--> DETECTED --next update--> ONGOING
    pending --recovered--> normal (nothing is reported: transient spike)
    active --recovered for recovery_s--> RESOLVED      (hysteresis: recovery bar < trigger bar)
    active --no current data for expire_after_s--> EXPIRED   (cannot be confirmed any more)
    RESOLVED --abnormal again within cooldown_s--> the same anomaly re-opens (occurrences + 1)

De-duplication / suppression (counted, never silently dropped):

* one key has at most one active anomaly; repeated triggers update it
* a behavioral anomaly on a signal that already has an active *threshold* anomaly is SUPPRESSED (the
  safety rule already says it), as is a multivariate anomaly whose top contributors are all active
* keys an operator marked as false positives are SUPPRESSED until their cooldown ends
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.domain.anomalies.baseline import BaselineStatus, SignalBaseline
from app.domain.anomalies.detectors import (
    Assessment,
    MultivariateAssessment,
    SignalMemory,
    assess_multivariate,
    assess_signal,
)
from app.domain.anomalies.iforest import MultivariateModel
from app.domain.anomalies.models import Anomaly, AnomalyType, Detector, Level, Lifecycle
from app.domain.anomalies.observation import Observation
from app.domain.anomalies.policy import AnomalyPolicy
from app.domain.anomalies.scoring import Scored, deviation_points, evidence_strength, score
from app.domain.anomalies.signals import FAMILY_TITLES, SIGNALS_BY_ID, Signal, feature_title
from app.domain.anomalies.stats import Ewma, robust_scale

METHOD_TEXT = {
    "robust_z": "far outside this device's usual range (robust z-score against the median)",
    "quantile": "above this device's 99th percentile for this time of day",
    "ewma_shift": "a sustained shift of the smoothed level (EWMA), not a single spike",
    "volatility": "fluctuating much more than usual for this device",
    "iforest": "an unusual combination of signals for this device (Isolation Forest)",
}


@dataclass(frozen=True, slots=True)
class Transition:
    kind: str  # detected | updated | resolved | expired | suppressed
    anomaly: Anomaly
    changed: tuple[str, ...] = ()


@dataclass
class _Track:
    """Lifecycle state of one key on one device."""

    pending_since: datetime | None = None
    normal_since: datetime | None = None
    last_data: datetime | None = None
    anomaly: Anomaly | None = None
    resolved: Anomaly | None = None  # last resolved anomaly (cooldown re-open)
    last_event_at: datetime | None = None
    last_band: str | None = None
    suppressed: int = 0


@dataclass
class DeviceState:
    memories: dict[str, SignalMemory] = field(default_factory=dict)
    tracks: dict[str, _Track] = field(default_factory=dict)
    evaluations: int = 0


@dataclass(frozen=True, slots=True)
class Candidate:
    """A detector verdict for one key, before the lifecycle decides what to report."""

    key: str
    anomaly_type: AnomalyType
    detector: Detector
    signal: Signal | None
    abnormal: bool
    recovered: bool
    value: float | None
    assessment: Assessment | None = None
    multivariate: MultivariateAssessment | None = None


class BehaviorEngine:
    def __init__(self, policy: AnomalyPolicy, id_factory: Callable[[], str] | None = None) -> None:
        self.policy = policy
        self._new_id = id_factory or (lambda: str(uuid.uuid4()))
        self._devices: dict[str, DeviceState] = {}
        self.stats = {
            "evaluations": 0,
            "detected": 0,
            "resolved": 0,
            "expired": 0,
            "suppressed": 0,
            "reopened": 0,
        }

    # ------------------------------------------------------------------ admin
    def set_policy(self, policy: AnomalyPolicy) -> None:
        if policy.ewma_alpha != self.policy.ewma_alpha:
            for st in self._devices.values():
                st.memories.clear()
        self.policy = policy

    def forget(self, device_id: str) -> None:
        self._devices.pop(device_id, None)

    def adopt(self, anomaly: Anomaly) -> None:
        """Resume tracking an active anomaly loaded from storage (backend restart)."""
        st = self._devices.setdefault(anomaly.device_id, DeviceState())
        tr = st.tracks.setdefault(anomaly.rule_id, _Track())
        tr.anomaly = anomaly
        tr.last_data = anomaly.last_seen_at
        tr.last_event_at = anomaly.updated_at or anomaly.last_seen_at

    def active(self, device_id: str) -> list[Anomaly]:
        st = self._devices.get(device_id)
        if st is None:
            return []
        return [t.anomaly for t in st.tracks.values() if t.anomaly is not None]

    def acknowledge(self, device_id: str, anomaly_id: str) -> None:
        for a in self.active(device_id):
            if a.anomaly_id == anomaly_id:
                a.lifecycle = Lifecycle.ACKNOWLEDGED

    # ------------------------------------------------------------- evaluation
    def evaluate(
        self,
        device_id: str,
        now: datetime,
        observations: dict[str, Observation | None],
        baselines: dict[str, SignalBaseline],
        model: MultivariateModel | None = None,
        *,
        threshold_signals: frozenset[str] = frozenset(),
        muted_keys: frozenset[str] = frozenset(),
        processes: list[dict[str, Any]] | None = None,
    ) -> list[Transition]:
        """One evaluation of one device. ``observations`` maps signal id -> observation (None: no
        trustworthy current data). ``threshold_signals``: signal ids with an active safety anomaly."""
        p = self.policy
        st = self._devices.setdefault(device_id, DeviceState())
        st.evaluations += 1
        self.stats["evaluations"] += 1
        ts = now.timestamp()
        enabled = set(p.enabled_detectors)

        candidates: list[Candidate] = []
        assessments: dict[str, Assessment] = {}
        for sid, obs in observations.items():
            sig = SIGNALS_BY_ID.get(sid)
            base = baselines.get(sid)
            if sig is None or obs is None or base is None:
                continue
            mem = st.memories.get(sid)
            if mem is None:
                mem = st.memories[sid] = SignalMemory(Ewma(p.ewma_alpha))
            a = assess_signal(sig, obs, base, mem, p, ts, now)
            if a is None:
                continue
            assessments[sid] = a
            candidates.append(
                Candidate(
                    f"behavior.{sid}",
                    AnomalyType.BEHAVIORAL,
                    Detector.BEHAVIORAL,
                    sig,
                    a.abnormal,
                    a.recovered,
                    obs.value,
                    a,
                )
            )
            if a.volatility_ratio is not None:
                candidates.append(
                    Candidate(
                        f"behavior.volatility.{sid}",
                        AnomalyType.VOLATILITY,
                        Detector.BEHAVIORAL,
                        sig,
                        a.volatile,
                        a.volatility_recovered,
                        obs.value,
                        a,
                    )
                )
        if model is not None and p.iforest_enabled and "iforest" in enabled:
            mv = assess_multivariate(
                model, {f: observations.get(f) for f in model.features}, p.iforest_margin
            )
            if mv is not None:
                candidates.append(
                    Candidate(
                        "multivariate.iforest",
                        AnomalyType.MULTIVARIATE,
                        Detector.MULTIVARIATE,
                        None,
                        mv.abnormal,
                        mv.recovered,
                        round(mv.score, 4),
                        multivariate=mv,
                    )
                )

        seen = {c.key for c in candidates}
        abnormal_now = {c.signal.signal_id for c in candidates if c.signal and c.abnormal and c.assessment}
        out: list[Transition] = []
        for c in candidates:
            out.extend(
                self._step(
                    device_id, st, c, now, assessments, abnormal_now, threshold_signals, muted_keys, processes
                )
            )
        # keys without current data: expire after a while (device offline / signal stale)
        for key, tr in st.tracks.items():
            if key in seen:
                continue
            tr.pending_since = None
            stale = tr.last_data is not None and (now - tr.last_data).total_seconds() >= p.expire_after_s
            if tr.anomaly is not None and stale:
                out.append(self._close(tr, now, Lifecycle.EXPIRED, "no current data to confirm it"))
        self._correlate(device_id, st, now, out)
        return out

    # ---------------------------------------------------------------- lifecycle
    def _step(
        self,
        device_id: str,
        st: DeviceState,
        c: Candidate,
        now: datetime,
        assessments: dict[str, Assessment],
        abnormal_now: set[str],
        threshold_signals: frozenset[str],
        muted: frozenset[str],
        processes: list[dict[str, Any]] | None,
    ) -> list[Transition]:
        p = self.policy
        tr = st.tracks.setdefault(c.key, _Track())
        tr.last_data = now
        a = tr.anomaly
        if a is None:
            if not c.abnormal:
                if c.recovered:
                    tr.pending_since = None
                return []
            if tr.pending_since is None:
                tr.pending_since = now
            held = (now - tr.pending_since).total_seconds()
            needed = p.persistence_s * (p.iforest_persistence_factor if c.multivariate else 1.0)
            if held < needed:
                return []
            reason = self._suppression(c, st, assessments, threshold_signals, muted)
            if reason:
                tr.suppressed += 1
                self.stats["suppressed"] += 1
                if tr.suppressed == 1 or tr.suppressed % 30 == 0:
                    ghost = self._build(
                        device_id, c, tr.pending_since, now, assessments, abnormal_now, processes
                    )
                    ghost.lifecycle = Lifecycle.SUPPRESSED
                    ghost.resolved_at = now
                    ghost.evidence["suppressed_because"] = reason
                    ghost.occurrences = tr.suppressed
                    return [Transition("suppressed", ghost)]
                return []
            prev = tr.resolved
            if (
                prev is not None
                and prev.resolved_at
                and (now - prev.resolved_at).total_seconds() <= p.cooldown_s
            ):
                # recurrence within the cooldown: re-open the same anomaly instead of a new alert
                fresh = self._build(device_id, c, prev.started_at, now, assessments, abnormal_now, processes)
                fresh.anomaly_id = prev.anomaly_id
                fresh.occurrences = prev.occurrences + 1
                fresh.lifecycle = Lifecycle.ONGOING
                tr.anomaly, tr.resolved, tr.normal_since = fresh, None, None
                tr.last_event_at, tr.last_band = now, fresh.evidence.get("confidence_band")
                self.stats["reopened"] += 1
                return [Transition("updated", fresh, ("reopened", "occurrences"))]
            fresh = self._build(device_id, c, tr.pending_since, now, assessments, abnormal_now, processes)
            tr.anomaly, tr.normal_since = fresh, None
            tr.last_event_at, tr.last_band = now, fresh.evidence.get("confidence_band")
            self.stats["detected"] += 1
            return [Transition("detected", fresh)]

        # active anomaly
        if c.recovered and not c.abnormal:
            if tr.normal_since is None:
                tr.normal_since = now
            if (now - tr.normal_since).total_seconds() >= p.recovery_s:
                return [self._close(tr, now, Lifecycle.RESOLVED, "back within the expected range")]
            return []
        tr.normal_since = None
        updated = self._build(device_id, c, a.started_at, now, assessments, abnormal_now, processes)
        changed = self._merge(a, updated)
        if a.lifecycle is Lifecycle.DETECTED:
            a.lifecycle = Lifecycle.ONGOING
            changed.append("lifecycle")
        band = a.evidence.get("confidence_band")
        escalated = "level" in changed
        due = (
            tr.last_event_at is None
            or (now - tr.last_event_at).total_seconds() >= p.update_event_min_interval_s
        )
        if escalated or (band != tr.last_band) or (due and changed):
            tr.last_event_at, tr.last_band = now, band
            a.updated_at = now
            return [Transition("updated", a, tuple(changed))]
        return []

    def _close(self, tr: _Track, now: datetime, how: Lifecycle, why: str) -> Transition:
        a = tr.anomaly
        assert a is not None
        a.resolved_at = now
        a.updated_at = now
        a.lifecycle = how
        a.evidence["closed_because"] = why
        tr.anomaly, tr.resolved, tr.pending_since, tr.normal_since = None, a, None, None
        self.stats["resolved" if how is Lifecycle.RESOLVED else "expired"] += 1
        return Transition("resolved" if how is Lifecycle.RESOLVED else "expired", a, ("lifecycle",))

    @staticmethod
    def _merge(a: Anomaly, b: Anomaly) -> list[str]:
        """Refresh an active anomaly from a new assessment. Level and confidence keep their *peak*
        (the evidence that the anomaly happened does not shrink while it fades out); the current
        values are in ``evidence.current``. Observed values and the expected range are current."""
        changed: list[str] = []
        peak_level = max(a.effective_level, b.effective_level, key=lambda lv: lv.rank)
        if peak_level != a.effective_level:
            changed.append("level")
        peak_conf = max(a.confidence or 0.0, b.confidence or 0.0)
        if a.confidence is None or peak_conf - a.confidence >= 0.05:
            changed.append("confidence")
        for name in (
            "value",
            "expected_value",
            "expected_min",
            "expected_max",
            "deviation_score",
            "message",
            "related",
            "model_version",
            "baseline_version",
        ):
            setattr(a, name, getattr(b, name))
        a.level, a.severity, a.confidence = peak_level, peak_level.legacy, round(peak_conf, 3)
        ack = a.lifecycle is Lifecycle.ACKNOWLEDGED
        incident = a.evidence.get("incident")
        peak_evidence = {k: a.evidence.get(k) for k in ("severity", "confidence_band", "confidence_factors")}
        a.evidence = b.evidence
        a.evidence["current"] = {
            "level": b.effective_level.value,
            "confidence": b.confidence,
            "confidence_band": b.evidence.get("confidence_band"),
        }
        if b.effective_level.rank < peak_level.rank or (b.confidence or 0.0) < peak_conf:
            a.evidence.update({k: v for k, v in peak_evidence.items() if v is not None})
            a.evidence["confidence"] = a.confidence
        if incident:
            a.evidence["incident"] = incident
        a.last_seen_at = b.last_seen_at
        if ack:
            a.lifecycle = Lifecycle.ACKNOWLEDGED
        return changed

    def _suppression(
        self,
        c: Candidate,
        st: DeviceState,
        assessments: dict[str, Assessment],
        threshold_signals: frozenset[str],
        muted: frozenset[str],
    ) -> str | None:
        if c.key in muted:
            return "marked as a false positive by an operator (cooling down)"
        if c.signal is not None and c.signal.signal_id in threshold_signals:
            return "a safety-threshold anomaly is already active for this signal"
        if c.multivariate is not None:
            top = [f for f, z in c.multivariate.contributions if z >= 2.0]
            active = {t.anomaly.signal_id for t in st.tracks.values() if t.anomaly and t.anomaly.signal_id}
            if top and all(f in active or f in threshold_signals for f in top):
                return "every contributing signal already has its own active anomaly"
        return None

    # ------------------------------------------------------------- construction
    def _build(
        self,
        device_id: str,
        c: Candidate,
        started: datetime,
        now: datetime,
        assessments: dict[str, Assessment],
        abnormal_now: set[str],
        processes: list[dict[str, Any]] | None,
    ) -> Anomaly:
        p = self.policy
        held = max(0.0, (now - started).total_seconds())
        related = self._related(c, assessments, abnormal_now)
        correlated = sum(1 for r in related if r["abnormal"])
        if c.multivariate is not None:
            return self._build_multivariate(
                device_id, c, c.multivariate, started, now, held, related, correlated
            )
        a = c.assessment
        sig = c.signal
        assert a is not None and sig is not None
        scale = robust_scale(a.context.stats.mad, sig.min_scale)
        median, p05, p95 = a.expected
        trigger_z = p.z_trigger_cold if a.cold else p.z_trigger
        if c.anomaly_type is AnomalyType.VOLATILITY:
            ratio = a.volatility_ratio or 0.0
            dev_pts = 3 if ratio >= 8 else 2 if ratio >= 5 else 1
            ev = evidence_strength(ratio, p.volatility_ratio)
            dev_reason = f"fluctuating {ratio:.1f}x more than usual"
            methods = ["volatility"]
            dev_score = ratio
        else:
            z_eff = max(a.z, a.ewma_z)
            dev_pts = deviation_points(z_eff)
            ev = evidence_strength(
                z_eff, trigger_z if "robust_z" in a.triggers or not a.triggers else p.shift_z
            )
            dev_reason = f"{z_eff:.1f} robust standard deviations from this device's median"
            methods = list(a.triggers) or ["robust_z"]
            dev_score = z_eff
        scored = score(
            policy=p,
            deviation_points=dev_pts,
            deviation_reason=dev_reason,
            evidence=ev,
            held_s=held,
            correlated=correlated,
            value=a.observation.value,
            warning_level=sig.warning_level,
            critical_level=sig.critical_level,
            impact=sig.impact,
            baseline_status=a.baseline.status,
            baseline_samples=a.baseline.sample_count,
            coverage=a.observation.coverage,
        )
        upper = median + trigger_z * scale
        fmt = _fmt(sig.unit)
        if c.anomaly_type is AnomalyType.VOLATILITY:
            title = f"{sig.title} unusually unstable"
            message = (
                f"{sig.title} has been fluctuating {a.volatility_ratio or 0:.1f}x more than usual for this "
                f"device for {_dur(held)} (average {fmt(a.observation.value)})."
            )
        else:
            word = "high" if a.delta > 0 else "low"
            title = f"Unusually {word} {sig.title.lower()}"
            message = (
                f"{sig.title} is {fmt(a.observation.value)} (2-minute average) for {_dur(held)}; "
                f"usually {fmt(p05)}-{fmt(p95)} on this device at this time."
            )
        evidence = self._evidence(
            sig.unit,
            a.observation,
            a,
            scored,
            methods,
            held,
            related,
            processes,
            {"median": median, "p05": p05, "p95": p95, "upper_trigger": upper, "context": a.context.context},
        )
        return Anomaly(
            anomaly_id=self._new_id(),
            device_id=device_id,
            detector=c.detector,
            rule_id=c.key,
            component_id=sig.category,
            metric_key=sig.field,
            severity=scored.level.legacy,
            title=title,
            message=message,
            value=round(a.observation.value, 4),
            threshold=round(upper, 4),
            started_at=started,
            last_seen_at=now,
            anomaly_type=c.anomaly_type,
            category=sig.category,
            level=scored.level,
            confidence=scored.confidence,
            lifecycle=Lifecycle.DETECTED,
            signal_id=sig.signal_id,
            baseline_version=a.baseline.version,
            expected_value=round(median, 4),
            expected_min=round(p05, 4),
            expected_max=round(p95, 4),
            deviation_score=round(dev_score, 3),
            evidence=evidence,
            related=related,
            updated_at=now,
        )

    def _build_multivariate(
        self,
        device_id: str,
        c: Candidate,
        mv: MultivariateAssessment,
        started: datetime,
        now: datetime,
        held: float,
        related: list[dict[str, Any]],
        correlated: int,
    ) -> Anomaly:
        p = self.policy
        margin = mv.margin
        dev_pts = 3 if margin >= 0.08 else 2 if margin >= 0.04 else 1
        ev = 1.0 / (1.0 + math.exp(-margin * 60))
        scored = score(
            policy=p,
            deviation_points=dev_pts,
            deviation_reason=f"isolation score {mv.score:.3f} vs threshold {mv.model.threshold:.3f}",
            evidence=ev,
            held_s=held,
            correlated=correlated,
            value=None,
            warning_level=None,
            critical_level=None,
            impact=1,
            baseline_status=BaselineStatus.STABLE
            if mv.model.n_train >= p.stable_min_samples
            else BaselineStatus.DEVELOPING,
            baseline_samples=mv.model.n_train,
            coverage=mv.coverage,
        )
        top = [feature_title(f) for f, z in mv.contributions[:3] if z >= 1.0]
        names = ", ".join(top) if top else "several signals"
        evidence: dict[str, Any] = {
            "summary": f"The combination of {names} is unusual for this device, although each value "
            "alone may be within its normal range.",
            "observed": {"score": round(mv.score, 4)},
            "expected": {
                "threshold": round(mv.model.threshold, 4),
                "meaning": "more isolated than 99.5 % of this device's normal minutes",
            },
            "deviation": {"margin": round(margin, 4)},
            "contributions": [
                {
                    "signal_id": f,
                    "title": feature_title(f),
                    "robust_deviation": z,
                }
                for f, z in mv.contributions
            ],
            "imputed_features": mv.imputed,
            "duration_s": round(held, 1),
            "baseline": {
                "model_id": mv.model.model_id,
                "model_version": mv.model.version,
                "trained_samples": mv.model.n_train,
                "trained_until": mv.model.trained_until,
            },
            "methods": [{"id": "iforest", "text": METHOD_TEXT["iforest"]}],
            "severity": {"level": scored.level.value, "points": scored.points, "breakdown": scored.breakdown},
            "confidence": scored.confidence,
            "confidence_band": scored.confidence_band,
            "confidence_factors": scored.confidence_factors,
        }
        return Anomaly(
            anomaly_id=self._new_id(),
            device_id=device_id,
            detector=Detector.MULTIVARIATE,
            rule_id=c.key,
            component_id="device",
            metric_key="multivariate",
            severity=scored.level.legacy,
            title="Unusual combination of signals",
            message=f"Unusual combination of {names} for this device for {_dur(held)}.",
            value=round(mv.score, 4),
            threshold=round(mv.model.threshold, 4),
            started_at=started,
            last_seen_at=now,
            anomaly_type=AnomalyType.MULTIVARIATE,
            category="device",
            level=scored.level,
            confidence=scored.confidence,
            lifecycle=Lifecycle.DETECTED,
            model_version=mv.model.model_id,
            deviation_score=round(margin, 4),
            evidence=evidence,
            related=related,
            updated_at=now,
        )

    def _related(
        self, c: Candidate, assessments: dict[str, Assessment], abnormal_now: set[str]
    ) -> list[dict[str, Any]]:
        if c.multivariate is not None:
            sids = [f for f, _ in c.multivariate.contributions]
        elif c.signal is not None:
            sids = [
                s
                for s, a in assessments.items()
                if a.signal.family == c.signal.family and s != c.signal.signal_id
            ]
        else:
            sids = []
        out = []
        for sid in sids:
            a = assessments.get(sid)
            if a is None:
                continue
            out.append(
                {
                    "signal_id": sid,
                    "title": a.signal.title,
                    "value": round(a.observation.value, 3),
                    "unit": a.signal.unit,
                    "expected": round(a.context.stats.median, 3),
                    "z": round(a.z, 2),
                    "abnormal": sid in abnormal_now,
                }
            )
        return out

    def _evidence(
        self,
        unit: str,
        obs: Observation,
        a: Assessment,
        scored: Scored,
        methods: list[str],
        held: float,
        related: list[dict[str, Any]],
        processes: list[dict[str, Any]] | None,
        expected: dict[str, Any],
    ) -> dict[str, Any]:
        fmt = _fmt(unit)
        ev: dict[str, Any] = {
            "summary": f"{a.signal.title} {fmt(obs.value)} vs usual "
            f"{fmt(expected['p05'])}-{fmt(expected['p95'])} (median {fmt(expected['median'])}).",
            "observed": {
                "value": round(obs.value, 4),
                "latest": round(obs.latest, 4),
                "unit": unit,
                "window_s": self.policy.observation_window_s,
                "samples": obs.n,
            },
            "expected": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in expected.items()},
            "deviation": {
                "robust_z": round(a.z, 2),
                "ewma_z": round(a.ewma_z, 2),
                "delta": round(a.delta, 4),
                "volatility_ratio": round(a.volatility_ratio, 2) if a.volatility_ratio is not None else None,
                "trend_per_min": round(a.trend_per_min, 4) if a.trend_per_min is not None else None,
            },
            "duration_s": round(held, 1),
            "baseline": {
                "status": a.baseline.status.value,
                "version": a.baseline.version,
                "source": a.baseline.source,
                "context": a.context.context,
                "context_samples": a.context.stats.count,
                "sample_count": a.baseline.sample_count,
            },
            "methods": [{"id": m, "text": METHOD_TEXT.get(m, m)} for m in methods],
            "related": related,
            "data_quality": {
                "coverage": round(obs.coverage, 3),
                "dropped_impossible": obs.dropped_impossible,
            },
            "severity": {"level": scored.level.value, "points": scored.points, "breakdown": scored.breakdown},
            "confidence": scored.confidence,
            "confidence_band": scored.confidence_band,
            "confidence_factors": scored.confidence_factors,
        }
        if a.cold:
            ev["note"] = (
                "No device baseline yet: compared with the fleet baseline (stricter trigger, low confidence)."
            )
        if self.policy.process_context and processes:
            top = processes[:3]
            ev["process_context"] = {
                "wording": "associated with (observed at the same time; not established as the cause)",
                "processes": top,
            }
        return ev

    # ---------------------------------------------------------- correlation
    def _correlate(self, device_id: str, st: DeviceState, now: datetime, out: list[Transition]) -> None:
        """Active univariate anomalies of one family that started within the correlation window of
        each other share a correlation key (one incident, several signals)."""
        window = timedelta(seconds=self.policy.correlation_window_s)
        by_family: dict[str, list[Anomaly]] = {}
        for tr in st.tracks.values():
            a = tr.anomaly
            if a is None:
                continue
            family = _family_of(a)
            if family is not None:
                by_family.setdefault(family, []).append(a)
        emitted = {t.anomaly.anomaly_id for t in out}
        for family, items in by_family.items():
            items.sort(key=lambda x: x.started_at)
            groups: list[list[Anomaly]] = []
            for a in items:
                if groups and a.started_at - groups[-1][-1].started_at <= window:
                    groups[-1].append(a)
                else:
                    groups.append([a])
            for g in groups:
                if len(g) < 2:
                    continue  # a correlation, once established, stays (members may resolve earlier)
                key = f"{device_id}:{family}:{int(g[0].started_at.timestamp())}"
                signals = sorted({x.signal_id or "multivariate" for x in g})
                for a in g:
                    incident = a.evidence.get("incident") or {}
                    known = set(incident.get("signals") or [])
                    if a.correlation_key != key or not set(signals) <= known:
                        a.correlation_key = a.correlation_key or key
                        a.evidence["incident"] = {
                            "title": FAMILY_TITLES.get(family, family),
                            "signals": sorted(known | set(signals)),
                            "wording": "abnormal at the same time (correlated, not necessarily causal)",
                        }
                        if a.anomaly_id not in emitted:
                            a.updated_at = now
                            out.append(Transition("updated", a, ("correlation_key",)))
                            emitted.add(a.anomaly_id)


def _family_of(a: Anomaly) -> str | None:
    """Correlation family: the signal's own, or for a multivariate anomaly that of its strongest
    contributing signal (a relation feature counts for its target signal)."""
    if a.signal_id is not None:
        sig = SIGNALS_BY_ID.get(a.signal_id)
        return sig.family if sig else None
    for c in a.evidence.get("contributions") or []:
        sid = str(c.get("signal_id", "")).split("~", 1)[0]
        if sid in SIGNALS_BY_ID and float(c.get("robust_deviation") or 0) >= 2.0:
            return SIGNALS_BY_ID[sid].family
    return None


def _fmt(unit: str) -> Callable[[float], str]:
    if unit == "B/s":

        def f(v: float) -> str:
            for u, d in (("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3)):
                if abs(v) >= d:
                    return f"{v / d:.1f} {u}"
            return f"{v:.0f} B/s"

        return f
    if unit == "%":
        return lambda v: f"{v:.0f}%"
    return lambda v: f"{v:.0f} {unit}"


def _dur(s: float) -> str:
    return f"{s / 60:.0f} min" if s >= 90 else f"{s:.0f} s"


__all__ = ["BehaviorEngine", "Candidate", "Level", "Transition"]
