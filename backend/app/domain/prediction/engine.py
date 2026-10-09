"""Forecast assessment per target + prediction lifecycle (pure; the caller passes ``now``).

Assessment status (what the twin shows for each target, every evaluation):
    AVAILABLE             a forecast exists (with or without a threshold crossing)
    INSUFFICIENT_HISTORY  not enough usable history (target minimum span / buckets)
    LOW_CONFIDENCE        a crossing is forecast but confidence is below the publishing minimum
    UNSTABLE              the data is too erratic for a trend claim (no crossing is reported)
    NOT_APPLICABLE        e.g. battery charging / on AC, metric not reported by this device
    STALE_DATA            the newest sample is too old to forecast from

Prediction records (one per device x target x type x threshold - the correlation key):
    ACTIVE -> UPDATED (material change only) -> CONFIRMED (the threshold was actually reached)
                                             -> INVALIDATED (trend stopped / reversed / regime change /
                                                no longer applicable), with a reason
                                             -> EXPIRED (the latest likely crossing time passed without
                                                a crossing, or no current data for long)
    LOW_CONFIDENCE  an active prediction whose confidence fell below the publishing minimum but
                    above the keep minimum (hysteresis: it is not dropped on one weak evaluation)
    CANCELLED       the target was disabled by configuration
Confirmation stores the calibration: actual crossing time, timing error of the first and the latest
estimate, and the warning lead time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.anomalies.stats import median
from app.domain.prediction.forecasters import FITTERS, Fitted, Selection, fit_trend, select
from app.domain.prediction.preprocess import Prepared
from app.domain.prediction.scoring import Confidence, Crossing, confidence, severity, time_to_threshold
from app.domain.prediction.targets import Target

FEATURE_VERSION = "features-v1"  # bucketing + regime trimming + quality gates


@dataclass(frozen=True)
class PredictionPolicy:
    create_min_confidence: float = 0.5
    keep_min_confidence: float = 0.4
    invalidate_after: int = 3  # consecutive evaluations without a supporting forecast
    material_change: float = 0.2  # relative ETA change that is worth publishing
    min_publish_interval_factor: float = 3.0  # x target update interval between "updated" events
    expiry_grace_factor: float = 0.25  # of the ETA, after the latest likely crossing
    confirm_tolerance_factor: float = 0.5  # crossing within this share of the ETA counts as on time

    def public(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Assessment:
    target: Target
    status: str
    reason: str
    current: float | None = None
    fitted: Fitted | None = None
    selection: Selection | None = None
    crossing: Crossing | None = None
    conf: Confidence | None = None
    severity: str | None = None
    forecast: dict[str, float | None] | None = None  # expected value + range at the trend horizon
    curve: list[dict[str, float]] = field(default_factory=list)  # forecast path (future)
    history: list[tuple[float, float]] = field(default_factory=list)  # recent actual buckets
    prepared: Prepared | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def public(self, now: float) -> dict[str, Any]:
        t = self.target
        cr = self.crossing
        return {
            "target_id": t.target_id,
            "title": t.title,
            "unit": t.unit,
            "prediction_type": t.prediction_type,
            "status": self.status,
            "reason": self.reason,
            "current_value": _r(self.current),
            "threshold": cr.threshold if cr else t.thresholds[0],
            "time_to_threshold_s": round(cr.eta_s) if cr else None,
            "earliest_s": round(cr.earliest_s) if cr else None,
            "latest_s": round(cr.latest_s) if cr and cr.latest_s is not None else None,
            "crossing_at": _iso(now + cr.eta_s) if cr else None,
            "forecast": self.forecast,
            "confidence": self.conf.value if self.conf else None,
            "confidence_band": self.conf.band if self.conf else None,
            "severity": self.severity,
            "model": self.fitted.model if self.fitted else None,
            "model_version": self.fitted.version if self.fitted else None,
            "health": health(self),
        }


def health(a: Assessment) -> str:
    if a.status in ("INSUFFICIENT_HISTORY", "STALE_DATA", "NOT_APPLICABLE"):
        return "UNAVAILABLE"
    if a.status in ("LOW_CONFIDENCE", "UNSTABLE"):
        return "LOW CONFIDENCE"
    c = a.conf.value if a.conf else 0.6
    return "GOOD" if c >= 0.75 else "FAIR" if c >= 0.5 else "LOW CONFIDENCE"


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(v, nd)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _fmt_dur(s: float) -> str:
    if s < 90:
        return f"{s:.0f} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    if s < 2 * 86400:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.0f} days"


def _fmt_range(cr: Crossing) -> str:
    lo = _fmt_dur(cr.earliest_s)
    hi = _fmt_dur(cr.latest_s) if cr.latest_s is not None else "beyond the horizon"
    return f"likely {lo} to {hi}"


def assess(
    target: Target,
    prepared: Prepared,
    now: float,
    policy: PredictionPolicy,
    *,
    not_applicable: str | None = None,
    volatile_anomaly: bool = False,
    anomaly_context: list[dict[str, Any]] | None = None,
) -> Assessment:
    pts = prepared.points
    current = pts[-1][1] if pts else None
    a = Assessment(target, "AVAILABLE", "", current=current, prepared=prepared, history=pts[-120:])
    a.context = {
        "history_span_s": round(prepared.span_s),
        "buckets": len(pts),
        "coverage": round(prepared.coverage, 3),
        "newest_age_s": _r(prepared.newest_age_s, 1),
        "largest_gap_s": round(prepared.largest_gap_s),
        "dropped_invalid": prepared.dropped_invalid,
        "feature_version": FEATURE_VERSION,
        "bucket_s": target.bucket_s,
        "aggregation": target.aggregation,
    }
    if prepared.regime_break_at is not None:
        a.context["regime_change"] = {
            "at": _iso(prepared.regime_break_at),
            "jump": _r(prepared.regime_jump, 2),
        }
    if anomaly_context:
        a.context["anomalies"] = anomaly_context  # Phase 4 context: supporting evidence, not a forecast input
    if not_applicable:
        a.status, a.reason = "NOT_APPLICABLE", not_applicable
        return a
    if prepared.issue == "STALE_DATA":
        a.status = "STALE_DATA"
        age = prepared.newest_age_s
        a.reason = f"newest sample is {_fmt_dur(age)} old" if age is not None else "no samples received"
        return a
    if prepared.issue == "INSUFFICIENT_HISTORY":
        a.status = "INSUFFICIENT_HISTORY"
        a.reason = (
            f"{_fmt_dur(prepared.span_s)} of usable history ({len(pts)} points); needs "
            f"{_fmt_dur(target.min_history_s)} and {target.min_points} points"
        )
        if prepared.regime_break_at is not None:
            a.reason += " since the last regime change"
        return a
    assert current is not None
    floor = 0.05 if target.unit == "%" else 0.1
    trend = fit_trend(pts, target.bucket_s, floor)
    slope_h = trend.slope * 3600
    bad_sign = 1 if target.direction == "up" else -1
    ci_excludes_zero = (
        trend.slope_lo is not None
        and trend.slope_hi is not None
        and ((trend.slope_lo > 0) if bad_sign > 0 else (trend.slope_hi < 0))
    )
    agreement = trend.extra.get("sign_agreement", 0.0)
    # the data must actually have moved: net change between the first and last fifth of the window
    fifth = max(2, len(pts) // 5)
    net = (median([v for _, v in pts[-fifth:]]) - median([v for _, v in pts[:fifth]])) * bad_sign
    net_needed = max(2 * trend.sigma, 0.5 * abs(trend.slope) * prepared.span_s)
    meaningful = (
        slope_h * bad_sign >= target.min_slope_per_h
        and ci_excludes_zero
        and agreement >= 0.6
        and net >= net_needed
    )
    # physical levelling-off: a warm-up or allocation burst that is decelerating is not extrapolated
    decelerating = False
    half = len(pts) // 2
    if target.saturates and half >= 4:
        early = fit_trend(pts[:half], target.bucket_s, floor).slope * bad_sign
        late = fit_trend(pts[half:], target.bucket_s, floor).slope * bad_sign
        decelerating = early > 0 and late < 0.5 * early
        a.context["saturation"] = {
            "early_per_hour": _r(early * 3600 * bad_sign, 3),
            "recent_per_hour": _r(late * 3600 * bad_sign, 3),
            "decelerating": decelerating,
        }
    use_trend = meaningful and not decelerating
    # online check of the trend against persistence (rolling origins inside the window, no future data);
    # only needed when a trend is going to be used (it feeds the confidence's skill factor)
    sel = (
        select(pts, ("naive", "trend"), target.bucket_s, target.trend_horizon_s, floor)
        if use_trend
        else Selection("", {}, 0, "not evaluated: no usable trend")
    )
    model = "trend" if use_trend else target.level_model
    fitted = trend if use_trend else FITTERS[model](pts, target.bucket_s, floor)
    a.fitted, a.selection = fitted, sel
    # the expected range: trend when it is meaningful (unless backtests showed the level model is more
    # accurate for this metric, e.g. CPU), otherwise the metric's level model
    ranger = (
        fitted
        if (target.trend_range or not use_trend)
        else FITTERS[target.level_model](pts, target.bucket_s, floor)
    )
    lo, hi = ranger.bounds(target.trend_horizon_s)
    clamp = target.plausible
    a.forecast = {
        "horizon_s": target.trend_horizon_s,
        "expected": _r(min(max(ranger.mean(target.trend_horizon_s), clamp[0]), clamp[1])),
        "lower": _r(max(lo, clamp[0])),
        "upper": _r(min(hi, clamp[1])),
    }
    if target.exceedance:
        p_ex = exceedance_probability(pts, target, target.thresholds[0])
        if p_ex is not None:
            a.forecast["exceedance_probability"] = p_ex[0]
            a.context["exceedance"] = {
                "windows": p_ex[1],
                "threshold": target.thresholds[0],
                "method": "share of recent windows of the horizon length that exceeded it",
            }
    a.context["model_selection"] = {
        "model": model,
        "mae": sel.mae,
        "origins": sel.origins,
        "reason": "trend gates passed" if use_trend else f"no usable trend: level model {target.level_model}",
    }
    a.context["net_change"] = {"observed": _r(net, 3), "required": _r(net_needed, 3)}
    a.context["trend"] = {
        "per_hour": _r(slope_h, 4),
        "sign_agreement": agreement,
        "slope_ci_per_hour": [_r((trend.slope_lo or 0) * 3600, 4), _r((trend.slope_hi or 0) * 3600, 4)],
        "residual_sigma": _r(trend.sigma, 3),
    }
    if not use_trend:
        skill = 0.5
    elif sel.mae.get("naive"):
        skill = 0.5 + 0.5 * max(0.0, min(1.0, 1 - sel.mae["trend"] / sel.mae["naive"]))
    else:
        skill = 0.6  # too little history to evaluate the trend against persistence
    threshold = target.thresholds[0]
    crossing: Crossing | None = None
    already = (current >= threshold) if bad_sign > 0 else (current <= threshold)
    if target.crossing and not already and use_trend:
        reach = min(target.horizon_s, target.max_extrapolation * prepared.span_s)  # no claims beyond the data
        crossing = time_to_threshold(fitted, threshold, target.direction, reach)
        if crossing is not None and len(target.thresholds) > 1:
            crit = time_to_threshold(fitted, target.thresholds[-1], target.direction, reach)
            if crit is not None:
                a.context["critical_crossing_s"] = round(crit.eta_s)
    conf = confidence(
        span_s=prepared.span_s,
        min_history_s=target.min_history_s,
        coverage=prepared.coverage,
        skill=skill,
        sign_agreement=agreement,
        eta_s=crossing.eta_s if crossing else None,
        crossing=crossing,
        volatile_anomaly=volatile_anomaly,
    )
    a.conf = conf
    a.curve = _curve(fitted, target, now, crossing)
    unit = target.unit
    rate = _rate(slope_h, unit, target.horizon_s)
    if already:
        a.reason = f"already at or beyond {threshold:g}{unit} (an observed state, not a forecast)"
    elif crossing is not None and not meaningful:
        a.status, crossing = "UNSTABLE", None
        a.reason = (
            f"no consistent trend (trend {rate}, {agreement:.0%} of the data "
            "agrees with it): no threshold crossing is estimated"
        )
    elif crossing is not None and conf.value < policy.create_min_confidence:
        a.status = "LOW_CONFIDENCE"
        a.crossing = crossing
        a.reason = (
            f"a crossing of {threshold:g}{unit} in ~{_fmt_dur(crossing.eta_s)} is possible, "
            "but confidence is low"
        )
    elif crossing is not None:
        a.crossing = crossing
        a.severity = severity(crossing.eta_s, target.severity_bands_s, conf.value, False)
        a.reason = (
            f"{target.title} {current:.1f}{unit}, trend {rate}: expected to reach "
            f"{threshold:g}{unit} in ~{_fmt_dur(crossing.eta_s)} ({_fmt_range(crossing)})"
        )
        cs = a.context.get("critical_crossing_s")
        if cs is not None:
            crit_sev = severity(cs, target.severity_bands_s, conf.value, True)
            if crit_sev == "CRITICAL":
                a.severity = crit_sev
    elif meaningful and decelerating:
        a.reason = (
            f"rising but levelling off ({rate} earlier, slower recently): no threshold crossing is estimated"
        )
    elif meaningful and not target.crossing:
        fc = a.forecast
        a.reason = (
            f"sustained trend {rate}: expected {fc['lower']:.0f}-{fc['upper']:.0f}{unit} "
            f"in {_fmt_dur(target.trend_horizon_s)} (trend forecast, no time-to-threshold claim)"
        )
    elif (pe := (a.forecast or {}).get("exceedance_probability") or 0.0) >= 0.5 and not already:
        h = _fmt_dur(target.trend_horizon_s)
        a.reason = (
            f"no steady trend, but {target.title.lower()} exceeded {threshold:g}{unit} in {pe:.0%} of "
            f"comparable {h} periods recently: exceeding it again within {h} is likely "
            "(recurring spikes, not a trend)"
        )
    elif meaningful:
        reach_s = min(target.horizon_s, target.max_extrapolation * prepared.span_s)
        a.reason = (
            f"trend {rate}; {threshold:g}{unit} is not reached within "
            f"{_fmt_dur(reach_s)} (forecast limit for this much history)"
        )
    else:
        expected = a.forecast["expected"]
        a.reason = (
            f"no meaningful trend ({model} forecast {expected}{unit} in {_fmt_dur(target.trend_horizon_s)})"
        )
    return a


def exceedance_probability(
    pts: list[tuple[float, float]], target: Target, threshold: float, min_windows: int = 6
) -> tuple[float, int] | None:
    """Empirical probability that the metric exceeds ``threshold`` within the next horizon: the share of
    windows of that length in the recent history (step = one bucket) whose maximum exceeded it."""
    k = max(1, round(target.trend_horizon_s / target.bucket_s))
    if len(pts) < k + min_windows:
        return None
    up = target.direction == "up"
    vals = [v for _, v in pts]
    hits = 0
    n = 0
    for i in range(len(vals) - k):
        window = vals[i + 1 : i + 1 + k]
        hits += any((v >= threshold) if up else (v <= threshold) for v in window)
        n += 1
    return round(hits / n, 3), n


def _rate(per_hour: float, unit: str, horizon_s: float) -> str:
    if horizon_s >= 2 * 86400:
        return f"{per_hour * 24:+.2f} {unit}/day"
    if abs(per_hour) >= 60:
        return f"{per_hour / 60:+.2f} {unit}/min"
    return f"{per_hour:+.2f} {unit}/h"


def _curve(f: Fitted, target: Target, now: float, crossing: Crossing | None) -> list[dict[str, float]]:
    span = (
        target.trend_horizon_s
        if crossing is None
        else min(target.horizon_s, max(target.trend_horizon_s, crossing.eta_s * 1.3))
    )
    out = []
    lo_c, hi_c = target.plausible
    for i in range(25):
        h = (now - f.t_last) + span * i / 24
        m = f.mean(h)
        lo, hi = f.bounds(h)
        out.append(
            {
                "t": round(f.t_last + h, 1),
                "mean": round(min(max(m, lo_c), hi_c), 3),
                "lower": round(max(lo, lo_c), 3),
                "upper": round(min(hi, hi_c), 3),
            }
        )
    return out


# ------------------------------------------------------------------------------------ lifecycle
@dataclass
class Prediction:
    prediction_id: str
    device_id: str
    correlation_key: str
    target_id: str
    prediction_type: str
    metric_field: str
    unit: str
    direction: str
    status: str
    severity: str | None
    current_value: float | None
    threshold: float
    forecast_value: float | None
    forecast_at: datetime | None
    time_to_threshold_s: float | None
    crossing_at: datetime | None
    crossing_earliest: datetime | None
    crossing_latest: datetime | None
    lower_bound: float | None
    upper_bound: float | None
    confidence: float
    confidence_band: str
    model_type: str
    model_version: str
    feature_version: str
    history_start: datetime | None
    history_end: datetime | None
    statement: str
    evidence: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    closed_at: datetime | None = None
    reason: str | None = None
    revisions: int = 0
    first_crossing_at: datetime | None = None
    actual_crossing_at: datetime | None = None
    timing_error_s: float | None = None
    first_timing_error_s: float | None = None
    lead_time_s: float | None = None
    baseline_version: str | None = None

    @property
    def active(self) -> bool:
        return self.status in ("ACTIVE", "UPDATED", "LOW_CONFIDENCE")

    def to_dict(self) -> dict[str, Any]:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        return {
            "prediction_id": self.prediction_id,
            "device_id": self.device_id,
            "correlation_key": self.correlation_key,
            "target_id": self.target_id,
            "prediction_type": self.prediction_type,
            "metric": self.metric_field,
            "unit": self.unit,
            "direction": self.direction,
            "status": self.status,
            "severity": self.severity,
            "current_value": self.current_value,
            "threshold": self.threshold,
            "forecast_value": self.forecast_value,
            "forecast_at": iso(self.forecast_at),
            "time_to_threshold_s": self.time_to_threshold_s,
            "crossing_at": iso(self.crossing_at),
            "crossing_earliest": iso(self.crossing_earliest),
            "crossing_latest": iso(self.crossing_latest),
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "confidence": self.confidence,
            "confidence_band": self.confidence_band,
            "model_type": self.model_type,
            "model_version": self.model_version,
            "feature_version": self.feature_version,
            "baseline_version": self.baseline_version,
            "history_start": iso(self.history_start),
            "history_end": iso(self.history_end),
            "statement": self.statement,
            "evidence": self.evidence,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "expires_at": iso(self.expires_at),
            "closed_at": iso(self.closed_at),
            "reason": self.reason,
            "revisions": self.revisions,
            "first_crossing_at": iso(self.first_crossing_at),
            "actual_crossing_at": iso(self.actual_crossing_at),
            "timing_error_s": self.timing_error_s,
            "first_timing_error_s": self.first_timing_error_s,
            "lead_time_s": self.lead_time_s,
        }


@dataclass(frozen=True, slots=True)
class Transition:
    kind: str  # created | updated | invalidated | expired | confirmed | cancelled
    prediction: Prediction
    changed: tuple[str, ...] = ()


@dataclass
class _Track:
    prediction: Prediction | None = None
    misses: int = 0
    last_published: float = 0.0
    rearm_below: float | None = None  # after CONFIRMED: no new prediction until the value is back below this


class PredictionTracker:
    """Lifecycle of predictions for all devices; deterministic ids are injectable for replay."""

    def __init__(self, policy: PredictionPolicy, id_factory: Any = None) -> None:
        self.policy = policy
        self._new_id = id_factory or (lambda: str(uuid.uuid4()))
        self._tracks: dict[str, _Track] = {}
        self.stats = {
            "created": 0,
            "updated": 0,
            "invalidated": 0,
            "expired": 0,
            "confirmed": 0,
            "cancelled": 0,
        }

    @staticmethod
    def key(device_id: str, target: Target) -> str:
        return f"{device_id}:{target.target_id}:{target.prediction_type}:{target.thresholds[0]:g}"

    def active(self, device_id: str) -> list[Prediction]:
        return [
            t.prediction
            for k, t in self._tracks.items()
            if k.startswith(device_id + ":") and t.prediction is not None
        ]

    def adopt(self, p: Prediction) -> None:
        self._tracks.setdefault(p.correlation_key, _Track()).prediction = p

    def step(self, device_id: str, a: Assessment, now: datetime) -> list[Transition]:
        p_ = self.policy
        t = a.target
        tr = self._tracks.setdefault(self.key(device_id, t), _Track())
        p = tr.prediction
        ts = now.timestamp()
        # 1. confirmation: the observed value reached the threshold while a prediction was active
        if p is not None and a.current is not None:
            reached = a.current >= p.threshold if p.direction == "up" else a.current <= p.threshold
            if reached:
                tr.rearm_below = p.threshold - t.rearm if p.direction == "up" else p.threshold + t.rearm
                return [
                    self._close(tr, now, "CONFIRMED", f"observed {a.current:g}{t.unit} reached the threshold")
                ]
        if not t.enabled:
            return [self._close(tr, now, "CANCELLED", "forecasting disabled for this metric")] if p else []
        supported = (
            a.status in ("AVAILABLE", "LOW_CONFIDENCE") and a.crossing is not None and a.conf is not None
        )
        if p is None:
            if tr.rearm_below is not None and a.current is not None:
                back = a.current < tr.rearm_below if t.direction == "up" else a.current > tr.rearm_below
                if not back:
                    return []  # still at the confirmed level: a new forecast would only repeat reality
                tr.rearm_below = None
            if a.status == "AVAILABLE" and supported:
                tr.prediction = self._new(device_id, a, now)
                tr.misses, tr.last_published = 0, ts
                self.stats["created"] += 1
                return [Transition("created", tr.prediction)]
            return []
        # 2. hard invalidation: the situation the prediction was based on no longer exists
        if a.status == "NOT_APPLICABLE":
            return [self._close(tr, now, "INVALIDATED", a.reason)]
        regime = a.context.get("regime_change")
        if regime and p.history_start and regime["at"] > p.history_start.isoformat() and not supported:
            return [self._close(tr, now, "INVALIDATED", f"regime change ({regime['jump']:+g}{t.unit} jump)")]
        # 3. expiry: the latest likely crossing passed without a crossing / no current data
        if p.expires_at and now > p.expires_at:
            return [
                self._close(
                    tr, now, "EXPIRED", "the likely crossing time passed without the threshold being reached"
                )
            ]
        if a.status == "STALE_DATA":
            tr.misses += 1
            if tr.misses >= p_.invalidate_after * 5:
                return [
                    self._close(tr, now, "EXPIRED", "no current data to confirm or update the prediction")
                ]
            return []
        # 4. soft invalidation with hysteresis
        if not supported or (a.conf is not None and a.conf.value < p_.keep_min_confidence):
            tr.misses += 1
            if tr.misses >= p_.invalidate_after:
                return [
                    self._close(
                        tr, now, "INVALIDATED", a.reason or "the forecast no longer supports a crossing"
                    )
                ]
            return []
        tr.misses = 0
        assert a.crossing is not None and a.conf is not None
        changed = self._refresh(p, a, now)
        low = a.conf.value < p_.create_min_confidence
        if low != (p.status == "LOW_CONFIDENCE"):
            changed.append("status")
        material = "severity" in changed or "status" in changed or "eta" in changed
        due = ts - tr.last_published >= t.update_interval_s * p_.min_publish_interval_factor
        if material and (due or "severity" in changed or "status" in changed):
            p.status = "LOW_CONFIDENCE" if low else "UPDATED"  # UPDATED = revised and re-published
            p.revisions += 1
            tr.last_published = ts
            self.stats["updated"] += 1
            return [Transition("updated", p, tuple(changed))]
        return []

    # -------------------------------------------------------------------------------------- helpers
    def _new(self, device_id: str, a: Assessment, now: datetime) -> Prediction:
        t = a.target
        cr = a.crossing
        assert cr is not None and a.conf is not None and a.fitted is not None
        hist = a.prepared.points if a.prepared else []
        crossing_at = now + timedelta(seconds=cr.eta_s)
        p = Prediction(
            prediction_id=self._new_id(),
            device_id=device_id,
            correlation_key=self.key(device_id, t),
            target_id=t.target_id,
            prediction_type=t.prediction_type,
            metric_field=t.field,
            unit=t.unit,
            direction=t.direction,
            status="ACTIVE",
            severity=a.severity,
            current_value=_r(a.current),
            threshold=cr.threshold,
            forecast_value=(a.forecast or {}).get("expected"),
            forecast_at=now + timedelta(seconds=t.trend_horizon_s),
            time_to_threshold_s=round(cr.eta_s),
            crossing_at=crossing_at,
            crossing_earliest=now + timedelta(seconds=cr.earliest_s),
            crossing_latest=now + timedelta(seconds=cr.latest_s) if cr.latest_s is not None else None,
            lower_bound=(a.forecast or {}).get("lower"),
            upper_bound=(a.forecast or {}).get("upper"),
            confidence=a.conf.value,
            confidence_band=a.conf.band,
            model_type=a.fitted.model,
            model_version=a.fitted.version,
            feature_version=FEATURE_VERSION,
            history_start=datetime.fromtimestamp(hist[0][0], UTC) if hist else None,
            history_end=datetime.fromtimestamp(hist[-1][0], UTC) if hist else None,
            statement=a.reason,
            evidence=evidence(a),
            created_at=now,
            updated_at=now,
            expires_at=self._expiry(now, cr),
            first_crossing_at=crossing_at,
        )
        return p

    def _expiry(self, now: datetime, cr: Crossing) -> datetime:
        latest = cr.latest_s if cr.latest_s is not None else cr.eta_s * 2
        return now + timedelta(seconds=latest + cr.eta_s * self.policy.expiry_grace_factor)

    def _refresh(self, p: Prediction, a: Assessment, now: datetime) -> list[str]:
        cr = a.crossing
        assert cr is not None and a.conf is not None and a.fitted is not None
        changed: list[str] = []
        # smooth the ETA (no thrashing): blend with the previous estimate projected to now
        prev_eta = (p.crossing_at - now).total_seconds() if p.crossing_at else cr.eta_s
        eta = 0.5 * cr.eta_s + 0.5 * max(prev_eta, 0.0)
        if p.time_to_threshold_s and abs(eta - prev_eta) > self.policy.material_change * max(prev_eta, 1.0):
            changed.append("eta")
        if a.severity != p.severity:
            changed.append("severity")
        if abs(a.conf.value - p.confidence) >= 0.1:
            changed.append("confidence")
        p.time_to_threshold_s = round(eta)
        p.crossing_at = now + timedelta(seconds=eta)
        p.crossing_earliest = now + timedelta(seconds=min(cr.earliest_s, eta))
        p.crossing_latest = (
            now + timedelta(seconds=max(cr.latest_s, eta)) if cr.latest_s is not None else None
        )
        p.expires_at = self._expiry(now, cr)
        p.severity, p.current_value = a.severity, _r(a.current)
        p.confidence, p.confidence_band = a.conf.value, a.conf.band
        p.model_type, p.model_version = a.fitted.model, a.fitted.version
        p.forecast_value = (a.forecast or {}).get("expected")
        p.lower_bound, p.upper_bound = (a.forecast or {}).get("lower"), (a.forecast or {}).get("upper")
        p.forecast_at = now + timedelta(seconds=a.target.trend_horizon_s)
        p.statement, p.evidence, p.updated_at = a.reason, evidence(a), now
        if a.prepared and a.prepared.points:
            p.history_end = datetime.fromtimestamp(a.prepared.points[-1][0], UTC)
        return changed

    def _close(self, tr: _Track, now: datetime, status: str, reason: str) -> Transition:
        p = tr.prediction
        assert p is not None
        p.status, p.reason, p.closed_at, p.updated_at = status, reason, now, now
        if status == "CONFIRMED":
            p.actual_crossing_at = now
            if p.crossing_at:
                p.timing_error_s = round((now - p.crossing_at).total_seconds())
            if p.first_crossing_at:
                p.first_timing_error_s = round((now - p.first_crossing_at).total_seconds())
            p.lead_time_s = round((now - p.created_at).total_seconds())
        tr.prediction, tr.misses = None, 0
        self.stats[status.lower()] += 1
        return Transition(status.lower(), p, ("status",))


def evidence(a: Assessment) -> dict[str, Any]:
    t = a.target
    return {
        "summary": a.reason,
        "observed": {
            "current": _r(a.current),
            "unit": t.unit,
            "history": [[round(x, 1), round(y, 3)] for x, y in a.history[-60:]],
        },
        "forecast": a.forecast,
        "curve": a.curve,
        "crossing": {
            "threshold": a.crossing.threshold,
            "eta_s": round(a.crossing.eta_s),
            "earliest_s": round(a.crossing.earliest_s),
            "latest_s": round(a.crossing.latest_s) if a.crossing.latest_s is not None else None,
        }
        if a.crossing
        else None,
        "confidence": {"value": a.conf.value, "band": a.conf.band, "factors": a.conf.factors}
        if a.conf
        else None,
        "model": {"type": a.fitted.model, "version": a.fitted.version, "feature_version": FEATURE_VERSION}
        if a.fitted
        else None,
        "context": a.context,
        "impact": t.impact,
        "wording": "estimate based on the recent trend; not a guarantee",
    }
