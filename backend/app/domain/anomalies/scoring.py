"""Explainable severity and evidence-based confidence (deterministic; no opaque score).

Severity = sum of points, each with a stated reason:

    deviation       robust z < 5: 1   5-8: 2   >= 8: 3     (multivariate: margin over threshold)
    persistence     >= 5 min: +1      >= 15 min: +2
    correlation     +1 per other active anomaly of the same incident family (max +2)
    safety          value >= the twin's warning level: +1, >= critical level: +2
    impact          signal impact weight 0-2 (memory/temperature 2, CPU/latency/drive activity 1)

    points: < LOW band -> INFO, LOW, MEDIUM, HIGH, CRITICAL (bands in the policy, default 2/3/5/7)
    CRITICAL needs safety proximity (value >= the warning level); >= critical level forces >= HIGH

Confidence (0-1) = evidence^0.40 x persistence^0.20 x baseline^0.25 x data-quality^0.15

    evidence     logistic in how far the deviation exceeds the trigger (z = trigger -> 0.5)
    persistence  held / (2 x persistence window), capped at 1 (just opened -> 0.5)
    baseline     STABLE 0.95, DEVELOPING 0.6-0.9 by sample count, DEGRADED 0.5, COLD/fleet 0.3
                 (COLD: confidence is also capped at 0.45 - a new device never gets a confident verdict)
    data quality share of expected samples present in the observation window

Bands: < 0.4 LOW, 0.4-0.7 MODERATE, 0.7-0.9 HIGH, >= 0.9 VERY HIGH (policy.confidence_bands).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.domain.anomalies.baseline import BaselineStatus
from app.domain.anomalies.models import Level
from app.domain.anomalies.policy import AnomalyPolicy

COLD_CONFIDENCE_CAP = 0.45


@dataclass(frozen=True, slots=True)
class Scored:
    level: Level
    points: int
    breakdown: list[dict[str, Any]]
    confidence: float
    confidence_band: str
    confidence_factors: dict[str, float]


def _logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def evidence_strength(z: float, trigger: float) -> float:
    return _logistic((z - trigger) * 1.2)


def baseline_factor(status: BaselineStatus, samples: int, policy: AnomalyPolicy) -> float:
    if status is BaselineStatus.STABLE:
        return 0.95
    if status is BaselineStatus.DEVELOPING:
        return 0.6 + 0.3 * min(1.0, samples / max(1, policy.stable_min_samples))
    if status is BaselineStatus.DEGRADED:
        return 0.5
    return 0.3


def confidence_band(c: float, policy: AnomalyPolicy) -> str:
    mod, high, very = policy.confidence_bands
    return "VERY HIGH" if c >= very else "HIGH" if c >= high else "MODERATE" if c >= mod else "LOW"


def score(
    *,
    policy: AnomalyPolicy,
    deviation_points: int,
    deviation_reason: str,
    evidence: float,
    held_s: float,
    correlated: int,
    value: float | None,
    warning_level: float | None,
    critical_level: float | None,
    impact: int,
    baseline_status: BaselineStatus,
    baseline_samples: int,
    coverage: float,
) -> Scored:
    breakdown: list[dict[str, Any]] = [
        {"factor": "deviation", "points": deviation_points, "reason": deviation_reason}
    ]
    persist_pts = 2 if held_s >= 900 else 1 if held_s >= 300 else 0
    breakdown.append(
        {"factor": "persistence", "points": persist_pts, "reason": f"abnormal for {held_s / 60:.0f} min"}
    )
    corr_pts = min(2, correlated)
    if corr_pts:
        breakdown.append(
            {
                "factor": "correlation",
                "points": corr_pts,
                "reason": f"{correlated} related signal(s) also abnormal",
            }
        )
    safety_pts = 0
    if value is not None and critical_level is not None and value >= critical_level:
        safety_pts = 2
        breakdown.append(
            {"factor": "safety", "points": 2, "reason": f"at/above the critical level {critical_level:g}"}
        )
    elif value is not None and warning_level is not None and value >= warning_level:
        safety_pts = 1
        breakdown.append(
            {"factor": "safety", "points": 1, "reason": f"at/above the warning level {warning_level:g}"}
        )
    if impact:
        breakdown.append({"factor": "impact", "points": impact, "reason": "signal with direct user impact"})
    points = deviation_points + persist_pts + corr_pts + safety_pts + impact
    low, med, high, crit = policy.severity_bands
    level = (
        Level.CRITICAL
        if points >= crit
        else Level.HIGH
        if points >= high
        else Level.MEDIUM
        if points >= med
        else Level.LOW
        if points >= low
        else Level.INFO
    )
    if safety_pts == 2 and level.rank < Level.HIGH.rank:
        level = Level.HIGH  # a value beyond a critical safety level is never routine
    if safety_pts == 0 and level is Level.CRITICAL:
        level = Level.HIGH  # unusual but within safety limits: never CRITICAL
        breakdown.append({"factor": "cap", "points": 0, "reason": "capped at HIGH: within safety limits"})

    # no persistence window configured: persistence carries no extra evidence either way
    persistence = 1.0 if policy.persistence_s <= 0 else min(1.0, held_s / (2 * policy.persistence_s))
    base = baseline_factor(baseline_status, baseline_samples, policy)
    quality = max(0.0, min(1.0, coverage))
    factors = {
        "evidence": round(evidence, 3),
        "persistence": round(persistence, 3),
        "baseline": round(base, 3),
        "data_quality": round(quality, 3),
    }
    conf = (
        max(evidence, 1e-6) ** 0.40 * max(persistence, 1e-6) ** 0.20 * base**0.25 * max(quality, 1e-6) ** 0.15
    )
    if baseline_status is BaselineStatus.COLD:
        conf = min(conf, COLD_CONFIDENCE_CAP)  # no device baseline: never a confident judgement
    conf = round(min(1.0, conf), 3)
    return Scored(level, points, breakdown, conf, confidence_band(conf, policy), factors)


def deviation_points(z: float) -> int:
    return 3 if z >= 8 else 2 if z >= 5 else 1
