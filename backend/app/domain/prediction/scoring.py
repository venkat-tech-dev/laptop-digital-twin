"""Threshold crossing, confidence and severity for forecasts (deterministic, documented).

Time to threshold
    first time the model's *mean* path reaches the threshold within the horizon (None: it does not -
    no crossing time is ever invented). The likely range comes from the prediction interval: the
    pessimistic bound gives the earliest, the optimistic bound the latest crossing (None = "not within
    the horizon"). Found by scanning the horizon and bisecting, so it works for curved (damped) paths.

Confidence (0-1) = history^0.15 x quality^0.15 x skill^0.20 x stability^0.25 x horizon^0.15 x precision^0.10
    history    usable span / (3 x the target's minimum history), capped at 1
    quality    share of expected buckets present (missing samples lower it)
    skill      0.5 + 0.5 x (1 - MAE_model / MAE_naive) from the rolling-origin evaluation; 0.6 when the
               history was too short to evaluate; naive-only forecasts score 0.5
    stability  how consistently the data moves in the trend direction (share of pairwise slopes with
               the trend's sign, 0.5 -> 0, 1.0 -> 1)
    horizon    1 while the forecast reaches no further than the history it is based on; 1/sqrt(ratio) beyond
    precision  1 / (1 + width of the crossing range / time to threshold); no latest bound -> 0.5
    x 0.85 when Phase 4 reports the signal as unusually *volatile* (context, not a forecast input)
Bands: >= 0.75 HIGH, >= 0.5 MEDIUM, else LOW.

Severity (from time to threshold, per target bands; configurable)
    <= band[3]: HIGH, <= band[2]: MEDIUM, <= band[1]: LOW, <= band[0]: INFO
    CRITICAL only for the target's critical threshold within band[3] *and* confidence >= 0.75
    LOW confidence lowers the severity one step (a weak forecast is never urgent)
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from app.domain.prediction.forecasters import Fitted

LEVELS = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")


@dataclass(frozen=True, slots=True)
class Crossing:
    threshold: float
    eta_s: float
    earliest_s: float
    latest_s: float | None


def _first_hit(
    fn: Callable[[float], float], threshold: float, up: bool, horizon_s: float, steps: int = 64
) -> float | None:
    def hit(h: float) -> bool:
        v = fn(h)
        return v >= threshold if up else v <= threshold

    if hit(0.0):
        return 0.0
    prev = 0.0
    for k in range(1, steps + 1):
        h = horizon_s * (k / steps) ** 2  # finer near "now"
        if hit(h):
            lo, hi = prev, h
            for _ in range(24):
                mid = (lo + hi) / 2
                lo, hi = (lo, mid) if hit(mid) else (mid, hi)
            return hi
        prev = h
    return None


def time_to_threshold(f: Fitted, threshold: float, direction: str, horizon_s: float) -> Crossing | None:
    up = direction == "up"
    eta = _first_hit(f.mean, threshold, up, horizon_s)
    if eta is None:
        return None
    pessimistic = (lambda h: f.bounds(h)[1]) if up else (lambda h: f.bounds(h)[0])
    optimistic = (lambda h: f.bounds(h)[0]) if up else (lambda h: f.bounds(h)[1])
    earliest = _first_hit(pessimistic, threshold, up, horizon_s) or 0.0
    latest = _first_hit(optimistic, threshold, up, horizon_s)
    return Crossing(threshold, eta, min(earliest, eta), max(latest, eta) if latest is not None else None)


@dataclass(frozen=True, slots=True)
class Confidence:
    value: float
    band: str
    factors: dict[str, float]


def confidence(
    *,
    span_s: float,
    min_history_s: float,
    coverage: float,
    skill: float,
    sign_agreement: float,
    eta_s: float | None,
    crossing: Crossing | None,
    volatile_anomaly: bool,
) -> Confidence:
    history = min(1.0, span_s / max(1.0, 3 * min_history_s))
    quality = max(0.0, min(1.0, coverage))
    stability = max(0.0, min(1.0, (sign_agreement - 0.5) / 0.5))
    if eta_s is None or eta_s <= 0:
        horizon = 1.0
    else:
        ratio = eta_s / max(span_s, 1.0)
        horizon = 1.0 if ratio <= 1 else 1 / math.sqrt(ratio)
    if crossing is None:
        precision = 1.0
    elif crossing.latest_s is None:
        precision = 0.5
    else:
        width = crossing.latest_s - crossing.earliest_s
        precision = 1 / (1 + width / max(crossing.eta_s, 1.0))
    factors = {
        "history": history,
        "quality": quality,
        "skill": skill,
        "stability": stability,
        "horizon": horizon,
        "precision": precision,
    }
    value = (
        max(history, 1e-6) ** 0.15
        * max(quality, 1e-6) ** 0.15
        * max(skill, 1e-6) ** 0.20
        * max(stability, 1e-6) ** 0.25
        * max(horizon, 1e-6) ** 0.15
        * max(precision, 1e-6) ** 0.10
    )
    if volatile_anomaly:
        value *= 0.85
        factors["volatile_anomaly"] = 0.85
    value = round(min(1.0, value), 3)
    return Confidence(value, band(value), {k: round(v, 3) for k, v in factors.items()})


def band(value: float) -> str:
    return "HIGH" if value >= 0.75 else "MEDIUM" if value >= 0.5 else "LOW"


def severity(
    eta_s: float, bands_s: tuple[int, int, int, int], conf: float, critical_threshold: bool
) -> str | None:
    if eta_s <= bands_s[3]:
        level = "CRITICAL" if critical_threshold and conf >= 0.75 else "HIGH"
    elif eta_s <= bands_s[2]:
        level = "MEDIUM"
    elif eta_s <= bands_s[1]:
        level = "LOW"
    elif eta_s <= bands_s[0]:
        level = "INFO"
    else:
        return None
    if conf < 0.5 and level != "INFO":
        level = LEVELS[LEVELS.index(level) - 1]
    return level
