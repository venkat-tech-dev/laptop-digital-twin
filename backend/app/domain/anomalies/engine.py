"""Safety-threshold anomaly engine (V1 rules), evaluated on every applied reading.

Phase 4: threshold anomalies are labelled ``threshold_anomaly`` (a fact about a safety limit, not a
learned judgement, so confidence is high and fixed). Learned behavioral / multivariate detection
runs in ``app.services.intelligence`` (``BehaviorEngine``) and supersedes the Phase-2 EWMA
statistical detector, which is kept only for explicit use (``specs=DEFAULT_SPECS``) and old records.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from app.domain.anomalies.models import LEGACY_LEVEL, Anomaly, AnomalyType, Detector, Lifecycle, Severity
from app.domain.anomalies.rules import DEFAULT_RULES, ThresholdRule
from app.domain.anomalies.statistical import StatisticalDetector, StatisticalSpec
from app.domain.components.models import Component
from app.domain.telemetry.models import MetricReading, Quality

#: Threshold metric -> behavioral signal id (a behavioral anomaly on the same signal is suppressed
#: while the safety rule is active: the threshold already says it).
THRESHOLD_SIGNALS = {
    "cpu.usage_percent": "cpu",
    "memory.usage_percent": "memory",
    "cpu.temperature_c": "temperature",
    "thermal.zone_temperature_c": "temperature",
}
CATEGORY_BY_PREFIX = {
    "cpu": "performance",
    "gpu": "performance",
    "memory": "memory",
    "thermal": "thermal",
    "fan": "thermal",
    "disk": "storage",
    "network": "network",
    "battery": "battery",
}
THRESHOLD_CONFIDENCE = 0.95  # deterministic limit crossed for the rule's duration


@dataclass
class AnomalyTransitions:
    opened: list[Anomaly] = field(default_factory=list)
    resolved: list[Anomaly] = field(default_factory=list)
    updated: list[Anomaly] = field(default_factory=list)


def _where(labels: Mapping[str, str]) -> str:
    for key in ("volume", "zone", "disk", "nic", "fan", "adapter"):
        if key in labels:
            return f" {labels[key]}"
    return ""


class AnomalyEngine:
    def __init__(
        self,
        device_id: str,
        rules: Iterable[ThresholdRule] = DEFAULT_RULES,
        specs: Iterable[StatisticalSpec] = (),
        detector: StatisticalDetector | None = None,
        history_size: int = 200,
    ) -> None:
        self.device_id = device_id
        self._rules: dict[str, list[ThresholdRule]] = {}
        for rule in rules:
            self._rules.setdefault(rule.metric, []).append(rule)
        self._specs = {s.metric: s for s in specs}
        self._detector = detector or StatisticalDetector()
        self._pending: dict[tuple[str, str], datetime] = {}
        self._series_max: dict[str, float] = {}
        self._last_ts: dict[str, datetime] = {}
        self.active: dict[tuple[str, str], Anomaly] = {}
        self.recent_resolved: deque[Anomaly] = deque(maxlen=history_size)

    @property
    def statistical(self) -> StatisticalDetector:
        return self._detector

    def evaluate(
        self, readings: Iterable[MetricReading], components: Mapping[str, Component]
    ) -> AnomalyTransitions:
        out = AnomalyTransitions()
        for r in readings:
            last = self._last_ts.get(r.key)
            if last is not None and r.timestamp <= last:
                continue  # replayed or duplicate sample: already evaluated
            self._last_ts[r.key] = r.timestamp
            if not r.available or r.quality is not Quality.GOOD or r.value is None:
                continue
            for rule in self._rules.get(r.metric, ()):
                if rule.unlabelled_only and r.labels:
                    continue
                self._apply_rule(rule, r, components, out)
            spec = self._specs.get(r.metric)
            if spec is not None and r.numeric is not None and not (spec.unlabelled_only and r.labels):
                self._apply_statistical(spec, r, out)
        return out

    # ------------------------------------------------------------------ rules
    def _apply_rule(
        self, rule: ThresholdRule, r: MetricReading, comps: Mapping[str, Component], out: AnomalyTransitions
    ) -> None:
        value: float | str = r.numeric if r.numeric is not None else str(r.value)
        resolved = self._resolve_dynamic(rule, r)
        if resolved is None:
            return
        rule = resolved
        key = (rule.rule_id, r.key)
        active = self.active.get(key)
        guard_ok = rule.guard is None or rule.guard(comps)
        if guard_ok and rule.breached(value):
            since = self._pending.setdefault(key, r.timestamp)
            held = (r.timestamp - since).total_seconds()
            if active is not None:
                active.last_seen_at = r.timestamp
                active.value = value
                if active.lifecycle is Lifecycle.DETECTED:
                    active.lifecycle = Lifecycle.ONGOING
                return
            if held >= rule.duration_s:
                anomaly = self._new(
                    Detector.RULE,
                    rule.rule_id,
                    r,
                    rule.severity,
                    rule.title,
                    self._format(rule, value, r, held),
                    value,
                    rule.threshold,
                    since,
                    {"duration_s": round(held, 1), "rule": rule.op + " " + str(rule.threshold)},
                )
                self.active[key] = anomaly
                out.opened.append(anomaly)
            return
        if active is not None and (not guard_ok or rule.cleared(value)):
            self._resolve(key, r.timestamp, out)
        if active is None or not guard_ok or rule.cleared(value):
            self._pending.pop(key, None)

    def _resolve_dynamic(self, rule: ThresholdRule, r: MetricReading) -> ThresholdRule | None:
        if rule.dynamic_fraction_of_max is None:
            return rule
        v = r.numeric or 0.0
        peak = max(self._series_max.get(r.key, 0.0), v)
        self._series_max[r.key] = peak
        if peak < 1000:  # no meaningful maximum observed yet
            return None
        frac = rule.dynamic_fraction_of_max
        return replace(rule, threshold=round(peak * frac), clear_threshold=round(peak * (frac - 0.1)))

    @staticmethod
    def _format(rule: ThresholdRule, value: float | str, r: MetricReading, held: float) -> str:
        try:
            return rule.message.format(
                value=value, threshold=rule.threshold, unit=r.unit, where=_where(r.labels), duration=held
            )
        except (ValueError, TypeError):
            return f"{rule.title}: {value} {r.unit}"

    # ------------------------------------------------------------ statistical
    def _apply_statistical(self, spec: StatisticalSpec, r: MetricReading, out: AnomalyTransitions) -> None:
        value = r.numeric
        assert value is not None
        verdict = self._detector.observe(r.key, value, spec)
        key = (f"stat:{spec.metric}", r.key)
        if verdict.opened:
            msg = (
                f"{r.metric}{_where(r.labels)} = {value:.4g} {r.unit} vs baseline {verdict.mean:.4g} "
                f"± {verdict.std:.3g} (z = {verdict.zscore:.1f})"
            )
            anomaly = self._new(
                Detector.STATISTICAL,
                f"stat:{spec.metric}",
                r,
                Severity.INFO,
                spec.title,
                msg,
                value,
                round(verdict.mean + self._detector.z_threshold * verdict.std, 4),
                r.timestamp,
                {
                    "zscore": round(verdict.zscore, 2),
                    "baseline_mean": round(verdict.mean, 4),
                    "baseline_std": round(verdict.std, 4),
                    "method": "EWMA z-score",
                },
            )
            self.active[key] = anomaly
            out.opened.append(anomaly)
        elif verdict.resolved and key in self.active:
            self._resolve(key, r.timestamp, out)
        elif key in self.active:
            self.active[key].last_seen_at = r.timestamp

    # ---------------------------------------------------------------- helpers
    def _new(
        self,
        detector: Detector,
        rule_id: str,
        r: MetricReading,
        severity: Severity,
        title: str,
        message: str,
        value: float | str,
        threshold: float | str | None,
        started: datetime,
        context: dict[str, object],
    ) -> Anomaly:
        rule_based = detector is Detector.RULE
        return Anomaly(
            anomaly_id=str(uuid.uuid4()),
            device_id=self.device_id,
            detector=detector,
            rule_id=rule_id,
            component_id=r.component_id,
            metric_key=r.key,
            severity=severity,
            title=title,
            message=message,
            value=value,
            threshold=threshold,
            started_at=started,
            last_seen_at=r.timestamp,
            context={**context, "source": r.source},
            anomaly_type=AnomalyType.THRESHOLD if rule_based else AnomalyType.BEHAVIORAL,
            category=CATEGORY_BY_PREFIX.get(r.metric.split(".", 1)[0], "device"),
            level=LEGACY_LEVEL[severity],
            confidence=THRESHOLD_CONFIDENCE if rule_based else None,
            lifecycle=Lifecycle.DETECTED,
            signal_id=THRESHOLD_SIGNALS.get(r.metric) if rule_based else None,
            evidence={
                "summary": message,
                "observed": {"value": value, "unit": r.unit},
                "expected": {"threshold": threshold, "rule": context.get("rule")},
                "methods": [{"id": "threshold", "text": "safety threshold held for the rule's duration"}],
            }
            if rule_based
            else {},
            updated_at=started,
        )

    def _resolve(self, key: tuple[str, str], ts: datetime, out: AnomalyTransitions) -> None:
        anomaly = self.active.pop(key, None)
        self._pending.pop(key, None)
        if anomaly is not None:
            anomaly.resolved_at = ts
            anomaly.updated_at = ts
            anomaly.lifecycle = Lifecycle.RESOLVED
            self.recent_resolved.appendleft(anomaly)
            out.resolved.append(anomaly)

    def resolve_component(self, component_id: str, ts: datetime) -> AnomalyTransitions:
        """Resolve anomalies whose source stopped reporting (e.g. sensor became unavailable)."""
        out = AnomalyTransitions()
        for key, a in list(self.active.items()):
            if a.component_id == component_id:
                self._resolve(key, ts, out)
        return out
