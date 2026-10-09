"""Small, dependency-free statistics used by analytics and predictions."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LinearFit:
    slope: float  # units per second
    intercept: float
    r2: float
    n: int
    span_s: float

    def predict(self, t: float) -> float:
        return self.intercept + self.slope * t

    def time_to_reach(self, target: float, from_t: float) -> float | None:
        """Seconds from ``from_t`` until the fitted line reaches ``target`` (None if never)."""
        if self.slope == 0:
            return None
        t = (target - self.intercept) / self.slope
        dt = t - from_t
        return dt if dt > 0 else None


def linear_fit(points: Sequence[tuple[float, float]]) -> LinearFit | None:
    n = len(points)
    if n < 3:
        return None
    t0 = points[0][0]
    xs = [p[0] - t0 for p in points]
    ys = [p[1] for p in points]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx
    intercept_rel = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept_rel + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    # Express intercept in absolute time so predict(t) takes epoch seconds.
    return LinearFit(slope, intercept_rel - slope * t0, max(0.0, r2), n, xs[-1])


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


@dataclass(frozen=True, slots=True)
class Summary:
    count: int
    mean: float | None
    minimum: float | None
    maximum: float | None
    p95: float | None
    stddev: float | None


def summarize(values: Sequence[float]) -> Summary:
    if not values:
        return Summary(0, None, None, None, None, None)
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return Summary(n, mean, min(values), max(values), percentile(values, 0.95), math.sqrt(var))


def confidence_from_fit(fit: LinearFit | None, min_points: int, min_span_s: float) -> tuple[str, float]:
    """Heuristic confidence: needs enough points, enough time span, and a good fit."""
    if fit is None or fit.n < min_points or fit.span_s < min_span_s:
        return "insufficient", 0.0
    score = min(1.0, fit.r2) * min(1.0, fit.n / (min_points * 4)) * min(1.0, fit.span_s / (min_span_s * 4))
    if score >= 0.5:
        return "high", round(score, 2)
    if score >= 0.2:
        return "medium", round(score, 2)
    return "low", round(score, 2)
