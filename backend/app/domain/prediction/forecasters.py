"""Forecasting models (pure Python, deterministic) and simplest-adequate model selection.

    naive   persistence: the last value. The reference every other model must beat.
    ewma    simple exponential smoothing (level only); alpha chosen on one-step errors.
    trend   robust linear trend: Theil-Sen slope (median of pairwise slopes - insensitive to spikes
            and missing samples, works on irregular time stamps) with Sen's non-parametric 90 %
            slope interval; level = robust line value at the newest point.
    holt    damped additive trend (Holt / Gardner-McKenzie, phi = 0.98); alpha/beta on a grid.

Every model returns a ``Fitted`` with a mean path and a prediction interval that widens with the
horizon (random-walk growth for level models, slope uncertainty for the trend).

Model selection (``select``): rolling-origin evaluation inside the history window - each origin is
fitted only on data *before* it (no future leakage), errors are measured at the target's trend
horizon, and the simplest model whose MAE is within ``tolerance`` (10 %) of the best wins
(complexity order naive < ewma < trend < holt). A complex model is only used when it is measurably
better.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.domain.anomalies.stats import median

MODEL_VERSIONS = {"naive": "naive-v1", "ewma": "ewma-v1", "trend": "theilsen-v1", "holt": "holt-damped-v1"}
COMPLEXITY = ("naive", "ewma", "trend", "holt")
Z80 = 1.2816  # 80 % prediction interval
Points = Sequence[tuple[float, float]]


@dataclass(frozen=True, slots=True)
class Fitted:
    model: str
    t_last: float  # epoch s of the newest observation
    level: float  # model value at t_last
    slope: float  # units per second (0 for level models)
    sigma: float  # one-step / residual scale (units)
    step_s: float  # bucket length
    phi: float = 1.0  # trend damping per step (holt)
    alpha: float = 0.0
    beta: float = 0.0
    slope_lo: float | None = None
    slope_hi: float | None = None
    n: int = 0
    extra: dict[str, float] = field(default_factory=dict)

    @property
    def version(self) -> str:
        return MODEL_VERSIONS[self.model]

    def mean(self, h_s: float) -> float:
        h_s = max(0.0, h_s)
        if self.model == "holt" and self.phi < 1.0:
            steps = h_s / self.step_s
            damp = self.phi * (1 - self.phi**steps) / (1 - self.phi) if steps > 0 else 0.0
            return self.level + self.slope * self.step_s * damp
        return self.level + self.slope * h_s

    def half_width(self, h_s: float, z: float = Z80) -> float:
        steps = max(1.0, h_s / self.step_s)
        if self.model in ("naive", "ewma"):
            return z * self.sigma * math.sqrt(steps)
        if self.model == "trend":
            se_slope = 0.0
            if self.slope_lo is not None and self.slope_hi is not None:
                se_slope = (self.slope_hi - self.slope_lo) / (2 * 1.645)
            # structural uncertainty floor: the future trend is never known better than +-10 %
            se_slope = max(se_slope, 0.1 * abs(self.slope))
            return z * math.sqrt(self.sigma**2 * (1 + 1 / max(self.n, 1)) + (h_s * se_slope) ** 2)
        # holt: classic approximation of the h-step variance
        acc = 1.0 + sum((self.alpha + self.beta * j) ** 2 for j in range(1, int(min(steps, 500))))
        return z * self.sigma * math.sqrt(acc)

    def bounds(self, h_s: float) -> tuple[float, float]:
        m, w = self.mean(h_s), self.half_width(h_s)
        return m - w, m + w


def _robust_sigma(residuals: Sequence[float], floor: float) -> float:
    if not residuals:
        return floor
    m = median(residuals)
    return max(1.4826 * median([abs(r - m) for r in residuals]), floor)


def fit_naive(pts: Points, step_s: float, floor: float) -> Fitted:
    diffs = [b[1] - a[1] for a, b in itertools.pairwise(pts)]
    return Fitted("naive", pts[-1][0], pts[-1][1], 0.0, _robust_sigma(diffs, floor), step_s, n=len(pts))


def fit_ewma(pts: Points, step_s: float, floor: float) -> Fitted:
    best: tuple[float, float, float, list[float]] | None = None
    for alpha in (0.1, 0.2, 0.3, 0.5, 0.7, 0.9):
        level, sse, errs = pts[0][1], 0.0, []
        for _, v in pts[1:]:
            e = v - level
            errs.append(e)
            sse += e * e
            level += alpha * e
        if best is None or sse < best[0]:
            best = (sse, alpha, level, errs)
    assert best is not None
    _, alpha, level, errs = best
    return Fitted("ewma", pts[-1][0], level, 0.0, _robust_sigma(errs, floor), step_s, alpha=alpha, n=len(pts))


def _block_reduce(pts: Points, max_n: int) -> list[tuple[float, float]]:
    if len(pts) <= max_n:
        return list(pts)
    size = math.ceil(len(pts) / max_n)
    out = []
    for i in range(0, len(pts), size):
        blk = pts[i : i + size]
        out.append((sum(t for t, _ in blk) / len(blk), sum(v for _, v in blk) / len(blk)))
    return out


def fit_trend(pts: Points, step_s: float, floor: float) -> Fitted:
    red = _block_reduce(pts, 80)  # Theil-Sen on <= 80 block means: robust, O(80^2) pairs
    n = len(red)
    slopes = sorted(
        (red[j][1] - red[i][1]) / (red[j][0] - red[i][0])
        for i in range(n)
        for j in range(i + 1, n)
        if red[j][0] > red[i][0]
    )
    if not slopes:
        return fit_naive(pts, step_s, floor)
    slope = median(slopes)
    # Sen (1968) 90 % confidence interval of the slope from the ranks of the pairwise slopes
    big_n = len(slopes)
    c = 1.645 * math.sqrt(n * (n - 1) * (2 * n + 5) / 18)
    lo_i = max(0, math.floor((big_n - c) / 2) - 1)
    hi_i = min(big_n - 1, math.ceil((big_n + c) / 2))
    intercept = median([v - slope * t for t, v in pts])
    residuals = [v - (intercept + slope * t) for t, v in pts]
    agree = sum(1 for s in slopes if (s > 0) == (slope > 0) and s != 0) / big_n if slope else 0.0
    t_last = pts[-1][0]
    return Fitted(
        "trend",
        t_last,
        intercept + slope * t_last,
        slope,
        _robust_sigma(residuals, floor),
        step_s,
        slope_lo=slopes[lo_i],
        slope_hi=slopes[hi_i],
        n=len(pts),
        extra={"sign_agreement": round(agree, 3)},
    )


def fit_holt(pts: Points, step_s: float, floor: float, phi: float = 0.98) -> Fitted:
    best: tuple[float, float, float, float, float, list[float]] | None = None
    for alpha in (0.2, 0.4, 0.6, 0.8):
        for beta in (0.05, 0.1, 0.2):
            level, trend = pts[0][1], (pts[1][1] - pts[0][1]) if len(pts) > 1 else 0.0
            sse, errs = 0.0, []
            for _, v in pts[1:]:
                f = level + phi * trend
                e = v - f
                errs.append(e)
                sse += e * e
                new_level = f + alpha * e
                trend = phi * trend + beta * (new_level - level - phi * trend)
                level = new_level
            if best is None or sse < best[0]:
                best = (sse, alpha, beta, level, trend, errs)
    assert best is not None
    _, alpha, beta, level, trend, errs = best
    return Fitted(
        "holt",
        pts[-1][0],
        level,
        trend / step_s,
        _robust_sigma(errs, floor),
        step_s,
        phi=phi,
        alpha=alpha,
        beta=beta,
        n=len(pts),
    )


FITTERS: dict[str, Callable[[Points, float, float], Fitted]] = {
    "naive": fit_naive,
    "ewma": fit_ewma,
    "trend": fit_trend,
    "holt": fit_holt,
}


@dataclass(frozen=True, slots=True)
class Selection:
    model: str
    mae: dict[str, float]  # rolling-origin MAE per candidate (empty: too little history to evaluate)
    origins: int
    reason: str


def select(
    pts: Points,
    candidates: Sequence[str],
    step_s: float,
    horizon_s: float,
    floor: float,
    tolerance: float = 0.10,
    max_origins: int = 5,
) -> Selection:
    """Rolling-origin evaluation: fit on pts[:i], score the forecast at pts[i + k] (k = horizon)."""
    k = max(1, round(horizon_s / step_s))
    first = max(6, int(len(pts) * 0.5))
    origins = list(range(first, len(pts) - k))
    if len(origins) > max_origins:
        stride = len(origins) / max_origins
        origins = [origins[int(i * stride)] for i in range(max_origins)]
    if len(origins) < 3:
        return Selection("", {}, len(origins), "too little history for a rolling-origin evaluation")
    mae: dict[str, float] = {}
    for name in candidates:
        errs = []
        for i in origins:
            f = FITTERS[name](pts[:i], step_s, floor)
            target_t, actual = pts[i - 1 + k]
            errs.append(abs(f.mean(target_t - f.t_last) - actual))
        mae[name] = sum(errs) / len(errs)
    best = min(mae.values())
    for name in COMPLEXITY:
        if name in mae and mae[name] <= best * (1 + tolerance) + 1e-9:
            simpler = name != min(mae, key=lambda m: mae[m])
            why = "simplest model within 10 % of the best error" if simpler else "lowest rolling-origin error"
            return Selection(name, {m: round(v, 4) for m, v in mae.items()}, len(origins), why)
    return Selection(min(mae, key=lambda m: mae[m]), mae, len(origins), "lowest error")
