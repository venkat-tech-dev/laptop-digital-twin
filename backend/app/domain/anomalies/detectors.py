"""Univariate and multivariate detectors (pure functions over an observation + a baseline).

All univariate detectors share one contract: ``assess_signal`` returns an ``Assessment`` saying
whether the observation *triggers* (enters abnormal) and whether it has *recovered* (hysteresis:
the recovery bar is lower than the trigger bar, so values hovering at a boundary do not flap).

    robust_z    (x - median) / max(1.4826 * MAD, floor)   >= z_trigger      recover <= z_recover
    quantile    x above the context's p99 (and z >= z_recover)              recover <= p95
    ewma_shift  EWMA of observations >= shift_z robust sigmas: a sustained level shift, also when
                single observations stay under the z trigger
    volatility  robust minute-to-minute volatility (median |successive difference| of 1-minute means)
                >= volatility_ratio x the baseline robust sigma; a single level step does not count

Every trigger also needs a *practically relevant* absolute deviation (``Signal.min_delta``) so a very
stable device does not alarm on statistically significant but meaningless changes.
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.domain.anomalies.baseline import BaselineStatus, ContextStats, SignalBaseline
from app.domain.anomalies.iforest import MultivariateModel
from app.domain.anomalies.observation import Observation
from app.domain.anomalies.policy import AnomalyPolicy
from app.domain.anomalies.signals import Signal
from app.domain.anomalies.stats import Ewma, median, robust_scale, robust_z, slope_per_minute


@dataclass
class SignalMemory:
    """Per (device, signal) detector memory (EWMA level, recent observations)."""

    ewma: Ewma
    recent: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=90))  # ~15 min @ 10 s
    minutes: dict[int, list[float]] = field(default_factory=dict)  # minute -> [sum, count] of raw samples
    last_sample_ts: float = 0.0

    def add_samples(self, points: tuple[tuple[float, float], ...], keep_minutes: int = 15) -> None:
        for t, v in points:
            if t <= self.last_sample_ts:
                continue
            self.last_sample_ts = t
            acc = self.minutes.setdefault(int(t // 60), [0.0, 0.0])
            acc[0] += v
            acc[1] += 1
        if self.minutes:
            newest = max(self.minutes)
            for m in [m for m in self.minutes if m <= newest - keep_minutes]:
                del self.minutes[m]

    def minute_volatility(self, now_ts: float, min_minutes: int = 8) -> float | None:
        """Robust minute-to-minute volatility: median |successive difference| of complete 1-minute
        means / 0.954 (= sigma for independent noise). A single level step is one outlier difference
        and does not count as volatility; a sustained erratic pattern does."""
        current = int(now_ts // 60)
        means = [(m, acc[0] / acc[1]) for m, acc in sorted(self.minutes.items()) if m < current and acc[1]]
        diffs = [abs(b - a) for (ma, a), (mb, b) in itertools.pairwise(means) if mb == ma + 1]
        if len(diffs) < min_minutes - 1:
            return None
        return median(diffs) / 0.954


@dataclass(frozen=True, slots=True)
class Assessment:
    signal: Signal
    observation: Observation
    context: ContextStats
    baseline: SignalBaseline
    cold: bool
    z: float
    ewma_z: float
    delta: float
    triggers: tuple[str, ...]  # detectors that fired
    abnormal: bool
    recovered: bool
    volatility_ratio: float | None
    volatile: bool
    volatility_recovered: bool
    trend_per_min: float | None

    @property
    def expected(self) -> tuple[float, float, float]:
        s = self.context.stats
        return s.median, s.p05, s.p95


def assess_signal(
    signal: Signal,
    obs: Observation,
    baseline: SignalBaseline,
    memory: SignalMemory,
    policy: AnomalyPolicy,
    when_ts: float,
    when_dt: Any,
) -> Assessment | None:
    ctx = baseline.for_time(when_dt, policy.min_context_samples)
    if ctx is None:
        return None
    s = ctx.stats
    cold = baseline.status is BaselineStatus.COLD
    scale = robust_scale(s.mad, signal.min_scale)
    sign = 1.0 if signal.direction == "up" else None
    raw_z = robust_z(obs.value, s.median, scale)
    z = raw_z if sign else abs(raw_z)
    delta = obs.value - s.median
    practical = (delta if sign else abs(delta)) >= signal.min_delta
    level = memory.ewma.update(obs.value)
    ewz_raw = robust_z(level, s.median, scale)
    ewz = ewz_raw if sign else abs(ewz_raw)
    memory.recent.append((when_ts, obs.value))

    enabled = set(policy.enabled_detectors)
    trigger_z = policy.z_trigger_cold if cold else policy.z_trigger
    q_trig = getattr(s, policy.quantile_trigger)
    q_rec = getattr(s, policy.quantile_recover)
    fired: list[str] = []
    if practical:
        if "robust_z" in enabled and z >= trigger_z:
            fired.append("robust_z")
        if "quantile" in enabled and not cold and obs.value > q_trig and z >= policy.z_recover:
            fired.append("quantile")
        if (
            "ewma_shift" in enabled
            and memory.ewma.n >= 3
            and ewz >= max(policy.shift_z, policy.z_recover + 0.5)
            and (not cold or ewz >= trigger_z)
        ):
            fired.append("ewma_shift")
    abnormal = bool(fired)
    # hysteresis: recovery needs a lower bar than the trigger on both the statistical and the
    # practical side (half of min_delta), so values hovering at a boundary do not flap
    negligible = (delta if sign else abs(delta)) < signal.min_delta * 0.5
    recovered = negligible or (
        z <= policy.z_recover and ewz < policy.shift_z and (cold or obs.value <= q_rec)
    )

    vol_ratio: float | None = None
    volatile = False
    vol_recovered = True
    memory.add_samples(obs.points)
    spread = memory.minute_volatility(when_ts) if "robust_z" in enabled else None
    if spread is not None:
        vol_ratio = spread / scale
        big_enough = spread >= signal.min_delta / 2
        volatile = (not cold) and big_enough and vol_ratio >= policy.volatility_ratio and not abnormal
        vol_recovered = vol_ratio < policy.volatility_ratio * 0.6 or not big_enough
    trend = slope_per_minute(list(memory.recent)[-30:])
    return Assessment(
        signal,
        obs,
        ctx,
        baseline,
        cold,
        z,
        ewz,
        delta,
        tuple(fired),
        abnormal,
        recovered,
        vol_ratio,
        volatile,
        vol_recovered,
        trend,
    )


@dataclass(frozen=True, slots=True)
class MultivariateAssessment:
    model: MultivariateModel
    score: float
    margin: float
    abnormal: bool
    recovered: bool
    contributions: list[tuple[str, float]]
    imputed: list[str]
    coverage: float


def assess_multivariate(
    model: MultivariateModel, observations: dict[str, Observation | None], min_margin: float = 0.0
) -> MultivariateAssessment | None:
    """Score the current feature vector. At most one missing feature is imputed with its median."""
    raw: list[float] = []
    imputed: list[str] = []
    coverages: list[float] = []
    for i, f in enumerate(model.features):
        obs = observations.get(f)
        if obs is None:
            imputed.append(f)
            raw.append(model.scaler.centers[i])
        else:
            raw.append(obs.value)
            coverages.append(obs.coverage)
    if len(imputed) > 1 or not coverages:
        return None
    sc = model.score(raw)
    margin = sc - model.threshold
    contributions = [(f, round(v, 2)) for f, v in model.contributions(raw) if f not in imputed]
    return MultivariateAssessment(
        model,
        sc,
        margin,
        sc > model.threshold + min_margin,
        sc <= model.threshold - 0.02,
        contributions,
        imputed,
        sum(coverages) / len(coverages),
    )
