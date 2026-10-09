"""Robust statistics used by the behavioral detectors (pure Python, deterministic, no numpy).

MAD-based scale: ``1.4826 * MAD`` estimates the standard deviation of normally distributed data but
is not dragged by outliers, so a past incident cannot inflate the notion of "normal spread".
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

MAD_TO_SIGMA = 1.4826


def median(xs: Sequence[float]) -> float:
    if not xs:
        raise ValueError("median of empty sequence")
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def quantile(sorted_xs: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile of an already sorted sequence (q in [0, 1])."""
    if not sorted_xs:
        raise ValueError("quantile of empty sequence")
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    pos = (len(sorted_xs) - 1) * min(1.0, max(0.0, q))
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def mad(xs: Sequence[float], center: float | None = None) -> float:
    c = median(xs) if center is None else center
    return median([abs(x - c) for x in xs])


def robust_scale(mad_value: float, floor: float) -> float:
    """Robust sigma with a floor: a perfectly flat history (MAD = 0) must not make every tiny change
    look infinitely abnormal."""
    return max(MAD_TO_SIGMA * mad_value, floor)


def robust_z(x: float, center: float, scale: float) -> float:
    return (x - center) / scale if scale > 0 else 0.0


@dataclass(frozen=True, slots=True)
class Summary:
    """Distribution summary of a clean training sample."""

    count: int
    median: float
    mad: float
    mean: float
    std: float
    p05: float
    p25: float
    p50: float
    p75: float
    p90: float
    p95: float
    p99: float

    @staticmethod
    def of(xs: Sequence[float]) -> Summary:
        s = sorted(x for x in xs if math.isfinite(x))
        if not s:
            raise ValueError("no finite values")
        n = len(s)
        m = median(s)
        mean = sum(s) / n
        var = sum((x - mean) ** 2 for x in s) / (n - 1) if n > 1 else 0.0
        return Summary(
            count=n,
            median=m,
            mad=mad(s, m),
            mean=mean,
            std=math.sqrt(var),
            p05=quantile(s, 0.05),
            p25=quantile(s, 0.25),
            p50=quantile(s, 0.50),
            p75=quantile(s, 0.75),
            p90=quantile(s, 0.90),
            p95=quantile(s, 0.95),
            p99=quantile(s, 0.99),
        )


class Ewma:
    """Exponentially weighted mean (level) of a series of observations."""

    def __init__(self, alpha: float) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = alpha
        self.value: float | None = None
        self.n = 0

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.alpha * x + (1 - self.alpha) * self.value
        self.n += 1
        return self.value


def slope_per_minute(points: Sequence[tuple[float, float]]) -> float | None:
    """Least-squares slope (value units per minute) of (epoch_s, value) points."""
    if len(points) < 3:
        return None
    t0 = points[0][0]
    xs = [(t - t0) / 60.0 for t, _ in points]
    ys = [v for _, v in points]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den
