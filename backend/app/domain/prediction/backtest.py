"""Backtesting: replay a recorded (or synthetic) series through the live forecasting code.

Walk forward in time. At each origin the series contains only samples up to that origin - the same
incremental ``BucketSeries`` the live service uses - so no future data can leak into a forecast.
The future after the origin is used only to score it:

    regression     MAE / RMSE / MAPE at the target's trend horizon, per model (naive, ewma, trend,
                   holt, and the automatically selected one)
    crossing       actual crossings of the primary threshold vs. forecast crossings issued before them:
                   timing error, share of actual times inside the predicted range, lead time, false
                   predictions (a crossing forecast within the horizon that did not happen), missed
                   crossings (an actual crossing with no forecast issued at least one bucket earlier)
    calibration    hit rate per confidence band (are HIGH-confidence forecasts right more often?)
    lifecycle      the same tracker as production: created / updated / confirmed / expired /
                   invalidated counts and updates per prediction (stability, no thrashing)
"""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.domain.prediction.engine import PredictionPolicy, PredictionTracker, assess
from app.domain.prediction.forecasters import FITTERS
from app.domain.prediction.preprocess import BucketSeries, prepare
from app.domain.prediction.targets import Target


@dataclass
class _Acc:
    abs_err: list[float] = field(default_factory=list)
    sq_err: list[float] = field(default_factory=list)
    pct_err: list[float] = field(default_factory=list)

    def add(self, pred: float, actual: float) -> None:
        e = pred - actual
        self.abs_err.append(abs(e))
        self.sq_err.append(e * e)
        if abs(actual) > 1.0:
            self.pct_err.append(abs(e) / abs(actual) * 100)

    def summary(self) -> dict[str, float | int | None]:
        n = len(self.abs_err)
        return {
            "n": n,
            "mae": round(sum(self.abs_err) / n, 4) if n else None,
            "rmse": round(math.sqrt(sum(self.sq_err) / n), 4) if n else None,
            "mape_percent": round(sum(self.pct_err) / len(self.pct_err), 2) if self.pct_err else None,
        }


def actual_crossings(
    points: Sequence[tuple[float, float]], threshold: float, up: bool, rearm: float
) -> list[float]:
    """Crossing *episodes*: after a crossing the metric must fall ``rearm`` below the threshold before
    another crossing counts (jitter around the threshold is one event, not dozens)."""
    out = []
    armed = False
    for t, v in points:
        bad = v >= threshold if up else v <= threshold
        safe = v < threshold - rearm if up else v > threshold + rearm
        if armed and bad:
            out.append(t)
            armed = False
        elif safe:
            armed = True
    return out


def backtest(
    raw: Sequence[tuple[float, float]],
    target: Target,
    *,
    step_s: float | None = None,
    policy: PredictionPolicy | None = None,
    regime_starts: Sequence[float] = (),
    not_applicable_at: Any = None,
) -> dict[str, Any]:
    """``raw`` = (epoch s, value) samples sorted by time. ``regime_starts`` = external regime boundaries
    (battery: unplug times); ``not_applicable_at(t)`` -> reason or None (battery: charging)."""
    policy = policy or PredictionPolicy()
    step_s = step_s or target.update_interval_s
    raw = sorted(raw)
    if len(raw) < 3:
        return {"skipped": "too little data"}
    up = target.direction == "up"
    threshold = target.thresholds[0]
    # ground truth on the target's own resampling (what "reached the threshold" means operationally)
    truth_series = BucketSeries(target.bucket_s, 10**7)
    truth_series.add(raw, target.plausible, raw[-1][0] + 1)
    truth = truth_series.series(target.aggregation, -math.inf)
    truth_t = [t for t, _ in truth]
    crossings = actual_crossings(truth, threshold, up, target.rearm)
    na_times = [t for t, _ in truth if not_applicable_at and not_applicable_at(t)]
    # conditions that make a forecast unverifiable: regime changes (cleanup, unplug) and data gaps
    breaks = [b[0] for a_, b in itertools.pairwise(truth) if abs(b[1] - a_[1]) > target.max_regime_jump]
    gaps = [(a_[0], b[0]) for a_, b in itertools.pairwise(truth) if b[0] - a_[0] > target.stale_after_s]
    exceed: list[tuple[float, bool]] = []  # (predicted probability, actually exceeded within the horizon)

    series = BucketSeries(target.bucket_s, max(10, target.window_s // target.bucket_s + 4))
    counter = iter(range(1, 10**9))
    tracker = PredictionTracker(policy, id_factory=lambda: f"bt-{next(counter)}")
    per_model = {m: _Acc() for m in (*target.models, "selected")}
    issued: list[dict[str, Any]] = []
    statuses: dict[str, int] = {}
    i = 0
    t = raw[0][0] + target.min_history_s
    end = raw[-1][0]
    k = max(1, round(target.trend_horizon_s / target.bucket_s))
    while t <= end:
        j = bisect.bisect_right(raw, (t, math.inf), lo=i)
        series.add(raw[i:j], target.plausible, t)
        i = j
        regime = max((r for r in regime_starts if r <= t), default=None)
        na = not_applicable_at(t) if not_applicable_at else None
        prepared = prepare(series, target, t, regime)
        a = assess(target, prepared, t, policy, not_applicable=na)
        statuses[a.status] = statuses.get(a.status, 0) + 1
        now = datetime.fromtimestamp(t, UTC)
        for tr in tracker.step("bt", a, now):
            issued.append({"kind": tr.kind, "t": t, "prediction": tr.prediction})
        # regression at the trend horizon (only when the future value is known)
        h_t = t + target.trend_horizon_s
        idx = bisect.bisect_left(truth_t, h_t)
        if a.fitted is not None and idx < len(truth) and abs(truth[idx][0] - h_t) <= target.bucket_s:
            actual = truth[idx][1]
            pts = prepared.points
            floor = 0.05 if target.unit == "%" else 0.1
            for m in target.models:
                f = FITTERS[m](pts, target.bucket_s, floor)
                per_model[m].add(f.mean(h_t - f.t_last), actual)
            expected = (a.forecast or {}).get("expected")
            if expected is not None:  # exactly what is reported to users at this origin
                per_model["selected"].add(float(expected), actual)
        # exceedance probability vs what actually happened in the next horizon
        pe = (a.forecast or {}).get("exceedance_probability")
        if pe is not None and h_t <= end and a.current is not None:
            below_now = a.current < threshold if up else a.current > threshold
            if below_now:
                lo_i, hi_i = bisect.bisect_right(truth_t, t), bisect.bisect_right(truth_t, h_t)
                fut = [v for _, v in truth[lo_i:hi_i]]
                if fut and truth[hi_i - 1][0] - t >= target.trend_horizon_s * 0.8:
                    exceed.append((pe, any((v >= threshold) if up else (v <= threshold) for v in fut)))
        # crossing forecasts issued at this origin
        if a.status == "AVAILABLE" and a.crossing is not None and a.conf is not None:
            issued.append(
                {
                    "kind": "forecast",
                    "t": t,
                    "eta": a.crossing.eta_s,
                    "earliest": a.crossing.earliest_s,
                    "latest": a.crossing.latest_s,
                    "band": a.conf.band,
                    "conf": a.conf.value,
                }
            )
        t += step_s
    del k

    # ---- crossing evaluation (each forecast vs the next actual crossing after its origin)
    forecasts = [x for x in issued if x["kind"] == "forecast"]
    censored = 0
    data_end = raw[-1][0]
    timing: list[float] = []
    leads: list[float] = []
    in_range, false_pred = 0, 0
    band_hits: dict[str, list[int]] = {}
    for fc in forecasts:
        nxt = next((c for c in crossings if c > fc["t"]), None)
        deadline = fc["t"] + (fc["latest"] if fc["latest"] is not None else fc["eta"] * 2) + fc["eta"] * 0.25
        hit = nxt is not None and nxt <= deadline
        if not hit:
            interrupted = next((x for x in na_times if x > fc["t"]), None)  # e.g. charger connected
            broke = any(fc["t"] < b < deadline for b in breaks)
            gap = any(g0 < deadline and g1 > fc["t"] for g0, g1 in gaps)
            if deadline > data_end or broke or gap or (interrupted is not None and interrupted < deadline):
                censored += 1  # cannot be judged: data ended / conditions changed before the deadline
                continue
        b = band_hits.setdefault(fc["band"], [0, 0])
        b[1] += 1
        if hit:
            b[0] += 1
            timing.append((fc["t"] + fc["eta"]) - nxt)
            leads.append(nxt - fc["t"])
            lo_t = fc["t"] + fc["earliest"]
            hi_t = fc["t"] + (fc["latest"] if fc["latest"] is not None else math.inf)
            in_range += lo_t <= nxt <= hi_t
        else:
            false_pred += 1
    missed = 0
    for c in crossings:
        if not any(c - target.bucket_s >= fc["t"] >= c - target.horizon_s for fc in forecasts):
            missed += 1
    abs_timing = sorted(abs(x) for x in timing)
    lifecycle = {
        k2: sum(1 for x in issued if x["kind"] == k2)
        for k2 in ("created", "updated", "confirmed", "invalidated", "expired")
    }
    created = max(1, lifecycle["created"])
    return {
        "target": target.target_id,
        "samples": len(raw),
        "span_hours": round((raw[-1][0] - raw[0][0]) / 3600, 1),
        "origins": sum(statuses.values()),
        "status_counts": statuses,
        "regression_at_horizon_s": target.trend_horizon_s,
        "models": {m: acc.summary() for m, acc in per_model.items()},
        "crossings": {
            "threshold": threshold,
            "actual_crossings": len(crossings),
            "forecasts_issued": len(forecasts),
            "matched_forecasts": len(timing),
            "censored_forecasts": censored,
            "false_prediction_rate": round(false_pred / (len(timing) + false_pred), 3)
            if (len(timing) + false_pred)
            else None,
            "missed_crossings": missed,
            "missed_crossing_rate": round(missed / len(crossings), 3) if crossings else None,
            "median_abs_timing_error_s": round(abs_timing[len(abs_timing) // 2]) if abs_timing else None,
            "mean_timing_error_s": round(sum(timing) / len(timing)) if timing else None,
            "actual_within_range": round(in_range / len(timing), 3) if timing else None,
            "mean_lead_time_s": round(sum(leads) / len(leads)) if leads else None,
            "hit_rate_by_band": {k2: round(v[0] / v[1], 3) for k2, v in sorted(band_hits.items())},
        },
        "exceedance": _classification(exceed),
        "lifecycle": {**lifecycle, "updates_per_prediction": round(lifecycle["updated"] / created, 2)},
        "forecast_log": [(round(x["t"], 1), round(x["eta"], 1)) for x in forecasts[:5000]],
    }


def _classification(pairs: list[tuple[float, bool]]) -> dict[str, Any]:
    """ "Likely to exceed" (p >= 0.5) as a classifier: precision, recall, F1, base rate, calibration."""
    if not pairs:
        return {"n": 0}
    tp = sum(1 for p, y in pairs if p >= 0.5 and y)
    fp = sum(1 for p, y in pairs if p >= 0.5 and not y)
    fn = sum(1 for p, y in pairs if p < 0.5 and y)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else None
    bins: dict[str, list[int]] = {}
    for p, y in pairs:
        key = f"{min(0.8, int(p * 5) / 5):.1f}-{min(1.0, int(p * 5) / 5 + 0.2):.1f}"
        b = bins.setdefault(key, [0, 0])
        b[0] += y
        b[1] += 1
    return {
        "n": len(pairs),
        "base_rate": round(sum(y for _, y in pairs) / len(pairs), 3),
        "precision": round(prec, 3) if prec is not None else None,
        "recall": round(rec, 3) if rec is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
        "calibration": {
            k: {"observed_rate": round(v[0] / v[1], 3), "n": v[1]} for k, v in sorted(bins.items())
        },
    }
