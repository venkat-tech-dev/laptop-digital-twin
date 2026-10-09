"""Fleet intelligence (Phase 10): explainable fleet health, cross-device correlation, recurring issues and
capacity projection. Pure functions over facts gathered for ONE organization's visible devices; nothing
here can see another tenant's data.

Every output separates what was observed from what is inferred:
  OBSERVED_FACT            counted directly from records (e.g. "18 devices reported memory pressure")
  STATISTICAL_ASSOCIATION  an attribute is over-represented among them, with a significance test
  POSSIBLE_EXPLANATION     a hypothesis worth investigating; causation is never claimed
  PREDICTION               a projection with stated assumptions and uncertainty
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

HEALTH_MODEL_VERSION = "fleet-health-v1"
CORRELATION_VERSION = "fleet-correlation-v1"

# ------------------------------------------------------------------------------------- health score
#: points deducted from a device's 100 (configurable per call; the version identifies the defaults)
DEFAULT_WEIGHTS: dict[str, float] = {
    "health_critical": 40.0,
    "health_warning": 15.0,
    "anomaly_critical": 15.0,  # per active anomaly, capped by anomaly_cap
    "anomaly_warning": 5.0,
    "anomaly_cap": 30.0,
    "prediction_24h": 10.0,  # per predicted threshold crossing within 24 h, capped
    "prediction_cap": 20.0,
    "alert_critical": 20.0,  # per open alert, capped
    "alert_high": 10.0,
    "alert_cap": 30.0,
    "non_compliant": 15.0,
    "partially_compliant": 5.0,
    "agent_outdated": 5.0,
}
STALE_AFTER_S = 900.0  # devices without telemetry for longer are excluded from the score (unknown state)
BANDS = ((85, "HEALTHY"), (70, "WATCH"), (50, "DEGRADED"), (0, "POOR"))


@dataclass
class DeviceHealthFacts:
    device_id: str
    health: str | None  # OK | WARNING | CRITICAL | None (twin state)
    telemetry_age_s: float | None
    anomalies: dict[str, int] = field(default_factory=dict)  # level -> active count
    predictions_24h: int = 0
    alerts: dict[str, int] = field(default_factory=dict)  # severity -> open count
    compliance: str | None = None
    agent_outdated: bool = False
    group_names: list[str] = field(default_factory=list)


def device_score(f: DeviceHealthFacts, w: dict[str, float]) -> tuple[int, list[dict[str, Any]]]:
    deductions: list[dict[str, Any]] = []

    def take(factor: str, points: float, detail: str) -> None:
        if points > 0:
            deductions.append({"factor": factor, "points": round(points, 1), "detail": detail})

    if f.health == "CRITICAL":
        take("device_health", w["health_critical"], "twin health CRITICAL")
    elif f.health == "WARNING":
        take("device_health", w["health_warning"], "twin health WARNING")
    crit, warn = f.anomalies.get("critical", 0), f.anomalies.get("warning", 0)
    take(
        "anomalies",
        min(w["anomaly_cap"], crit * w["anomaly_critical"] + warn * w["anomaly_warning"]),
        f"{crit} critical, {warn} warning active anomalies",
    )
    take(
        "predictions",
        min(w["prediction_cap"], f.predictions_24h * w["prediction_24h"]),
        f"{f.predictions_24h} threshold crossings predicted within 24 h",
    )
    ac, ah = f.alerts.get("CRITICAL", 0), f.alerts.get("HIGH", 0)
    take(
        "alerts",
        min(w["alert_cap"], ac * w["alert_critical"] + ah * w["alert_high"]),
        f"{ac} critical, {ah} high open alerts",
    )
    if f.compliance == "NON_COMPLIANT":
        take("compliance", w["non_compliant"], "non-compliant with the organization's policy")
    elif f.compliance == "PARTIALLY_COMPLIANT":
        take("compliance", w["partially_compliant"], "compliance checks unknown or partial")
    if f.agent_outdated:
        take("agent", w["agent_outdated"], "agent below the recommended version")
    score = max(0, round(100 - sum(d["points"] for d in deductions)))
    return score, deductions


def band(score: int) -> str:
    return next(name for floor, name in BANDS if score >= floor)


def fleet_health(facts: list[DeviceHealthFacts], weights: dict[str, float] | None = None) -> dict[str, Any]:
    """Mean of device scores over devices with current telemetry; critical conditions are listed apart
    so a healthy average can never hide them."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    scored: list[dict[str, Any]] = []
    unknown: list[str] = []
    critical: list[dict[str, Any]] = []
    for f in facts:
        if f.telemetry_age_s is None or f.telemetry_age_s > STALE_AFTER_S or f.health is None:
            unknown.append(f.device_id)
        else:
            s, ded = device_score(f, w)
            scored.append(
                {
                    "device_id": f.device_id,
                    "score": s,
                    "band": band(s),
                    "deductions": ded,
                    "groups": f.group_names,
                }
            )
        reasons = []
        if f.health == "CRITICAL":
            reasons.append("twin health CRITICAL")
        if f.alerts.get("CRITICAL"):
            reasons.append(f"{f.alerts['CRITICAL']} critical open alert(s)")
        if f.anomalies.get("critical"):
            reasons.append(f"{f.anomalies['critical']} critical active anomaly(ies)")
        if f.predictions_24h:
            reasons.append(f"{f.predictions_24h} threshold crossing(s) predicted within 24 h")
        if reasons:
            critical.append({"device_id": f.device_id, "reasons": reasons})
    n = len(facts)
    coverage = round(len(scored) / n, 3) if n else 0.0
    if not scored:
        return {
            "version": HEALTH_MODEL_VERSION,
            "status": "INSUFFICIENT_DATA",
            "score": None,
            "band": None,
            "devices": n,
            "scored": 0,
            "coverage": coverage,
            "unknown_devices": unknown,
            "critical_conditions": critical,
            "contributors": [],
            "weights": w,
            "worst_devices": [],
        }
    mean = sum(d["score"] for d in scored) / len(scored)
    factor_points: dict[str, float] = defaultdict(float)
    factor_devices: dict[str, int] = defaultdict(int)
    for d in scored:
        for x in d["deductions"]:
            factor_points[x["factor"]] += x["points"] / len(scored)
            factor_devices[x["factor"]] += 1
    contributors: list[dict[str, Any]] = sorted(
        (
            {"factor": k, "avg_points_deducted": round(v, 1), "devices": factor_devices[k]}
            for k, v in factor_points.items()
        ),
        key=lambda c: -c["avg_points_deducted"],
    )
    confidence = "HIGH" if coverage >= 0.9 and len(scored) >= 5 else "MEDIUM" if coverage >= 0.6 else "LOW"
    return {
        "version": HEALTH_MODEL_VERSION,
        "status": "OK",
        "score": round(mean),  # whole points: the inputs do not support decimals
        "band": band(round(mean)),
        "devices": n,
        "scored": len(scored),
        "coverage": coverage,
        "confidence": confidence,
        "unknown_devices": unknown,
        "critical_conditions": critical,
        "contributors": contributors,
        "worst_devices": sorted(scored, key=lambda d: d["score"])[:10],
        "weights": w,
        "formula": "device = 100 - capped deductions; fleet = mean over devices with telemetry < 15 min old",
    }


# ------------------------------------------------------------------------------- correlation
@dataclass(frozen=True)
class AnomalyFact:
    device_id: str
    signal: str  # e.g. "memory:pressure" (anomaly type + signal / metric)
    started_at: datetime
    level: str | None = None


def hypergeom_sf(k: int, n_total: int, n_success: int, draws: int) -> float:
    """P(X >= k) for X ~ Hypergeometric(N=n_total, K=n_success, n=draws), exact."""
    if k <= 0:
        return 1.0
    denom = math.comb(n_total, draws)
    top = min(n_success, draws)
    return min(
        1.0,
        sum(math.comb(n_success, i) * math.comb(n_total - n_success, draws - i) for i in range(k, top + 1))
        / denom,
    )


def _clusters(events: list[AnomalyFact], window: timedelta) -> list[list[AnomalyFact]]:
    """Per signal, maximal groups of distinct devices whose anomalies started within ``window``."""
    out: list[list[AnomalyFact]] = []
    by_signal: dict[str, list[AnomalyFact]] = defaultdict(list)
    for e in events:
        by_signal[e.signal].append(e)
    for evs in by_signal.values():
        evs.sort(key=lambda e: e.started_at)
        i = 0
        while i < len(evs):
            j = i
            seen: dict[str, AnomalyFact] = {}
            while j < len(evs) and evs[j].started_at - evs[i].started_at <= window:
                seen.setdefault(evs[j].device_id, evs[j])
                j += 1
            out.append(list(seen.values()))
            i = j  # non-overlapping windows: one insight per burst, no duplicates
    return out


def correlate(
    events: list[AnomalyFact],
    attributes: dict[str, dict[str, str]],
    *,
    window: timedelta = timedelta(minutes=30),
    min_devices: int = 3,
    min_fleet: int = 5,
    alpha: float = 0.05,
    org_id: str = "",
) -> dict[str, Any]:
    """Fleet insights: bursts of the same anomaly signal on several devices, and the device attributes
    over-represented among them (exact hypergeometric test, Bonferroni-corrected)."""
    n_fleet = len(attributes)
    if n_fleet < min_fleet:
        return {
            "version": CORRELATION_VERSION,
            "status": "INSUFFICIENT_DATA",
            "reason": f"{n_fleet} device(s) with attributes; at least {min_fleet} are needed "
            "to compare cohorts",
            "insights": [],
        }
    fleet_values: dict[str, Counter[str]] = defaultdict(Counter)
    for attrs in attributes.values():
        for dim, val in attrs.items():
            if val:
                fleet_values[dim][val] += 1
    insights: list[dict[str, Any]] = []
    for cluster in _clusters([e for e in events if e.device_id in attributes], window):
        k = len(cluster)
        if k < min_devices:
            continue
        t0 = min(e.started_at for e in cluster)
        t1 = max(e.started_at for e in cluster)
        tests: list[dict[str, Any]] = []
        for dim, counts in fleet_values.items():
            in_cluster: Counter[str] = Counter(
                str(attributes[e.device_id][dim]) for e in cluster if attributes[e.device_id].get(dim)
            )
            for val, c in in_cluster.items():
                n_val = counts[val]
                if n_val == n_fleet:  # every device shares it: no contrast possible
                    continue
                p = hypergeom_sf(c, n_fleet, n_val, k)
                tests.append(
                    {
                        "dimension": dim,
                        "value": val,
                        "devices_in_burst": c,
                        "of_burst": k,
                        "devices_in_fleet": n_val,
                        "of_fleet": n_fleet,
                        "lift": round((c / k) / (n_val / n_fleet), 2),
                        "p_value": p,
                    }
                )
        m = max(1, len(tests))
        associations: list[dict[str, Any]] = []
        for t in sorted(tests, key=lambda t: t["p_value"]):
            p_adj = min(1.0, t["p_value"] * m)
            if p_adj > alpha or t["devices_in_burst"] < min_devices or t["lift"] < 1.5:
                continue
            strength = (
                "STRONG" if p_adj < 0.001 and t["lift"] >= 3 else "MODERATE" if p_adj < 0.01 else "WEAK"
            )
            associations.append(
                {**t, "p_value": round(t["p_value"], 6), "p_adjusted": round(p_adj, 6), "strength": strength}
            )
        signal = cluster[0].signal
        key = hashlib.sha256(
            f"{org_id}|{signal}|{t0.replace(second=0, microsecond=0).isoformat()}".encode()
        ).hexdigest()[:16]
        insight = {
            "insight_id": key,  # stable across evaluations of the same burst: no duplicate insights
            "signal": signal,
            "observed_fact": f"{k} devices reported {signal.replace(':', ' ')} within "
            f"{max(1, round((t1 - t0).total_seconds() / 60))} minutes",
            "devices": sorted(e.device_id for e in cluster),
            "window": {"start": t0.isoformat(), "end": t1.isoformat()},
            "statistical_associations": associations,
            "possible_explanation": (
                f"A factor shared by devices with {associations[0]['dimension']} = "
                f"{associations[0]['value']} may be involved; investigate it first."
                if associations
                else "No device attribute is over-represented; consider a shared external cause "
                "(network, policy change, time of day) or coincidence."
            ),
            "causation": "NOT_ESTABLISHED",
            "tests_run": len(tests),
            "method": f"exact hypergeometric test, Bonferroni over {len(tests)} test(s), alpha {alpha}",
        }
        insights.append(insight)
    return {
        "version": CORRELATION_VERSION,
        "status": "OK",
        "fleet_devices": n_fleet,
        "window_minutes": int(window.total_seconds() // 60),
        "min_devices": min_devices,
        "insights": sorted(insights, key=lambda x: -len(x["devices"])),
    }


# ----------------------------------------------------------------------------- recurring issues
@dataclass(frozen=True)
class AlertFact:
    device_id: str
    alert_type: str
    severity: str
    created_at: datetime


def recurring_issues(
    alerts: list[AlertFact], now: datetime, min_occurrences: int = 3
) -> list[dict[str, Any]]:
    """Alert types seen repeatedly: occurrences, devices affected, devices with repeats, 7-day trend."""
    by_type: dict[str, list[AlertFact]] = defaultdict(list)
    for a in alerts:
        by_type[a.alert_type].append(a)
    out = []
    week = now - timedelta(days=7)
    prior = now - timedelta(days=14)
    for t, items in by_type.items():
        if len(items) < min_occurrences:
            continue
        per_device = Counter(a.device_id for a in items)
        recent = sum(1 for a in items if a.created_at >= week)
        before = sum(1 for a in items if prior <= a.created_at < week)
        out.append(
            {
                "alert_type": t,
                "occurrences": len(items),
                "devices_affected": len(per_device),
                "devices_with_repeats": sum(1 for c in per_device.values() if c > 1),
                "worst_severity": max(
                    (a.severity for a in items),
                    key=lambda s: (
                        ["LOW", "MEDIUM", "HIGH", "CRITICAL"].index(s)
                        if s in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
                        else -1
                    ),
                ),
                "first_seen": min(a.created_at for a in items).isoformat(),
                "last_seen": max(a.created_at for a in items).isoformat(),
                "last_7_days": recent,
                "previous_7_days": before,
                "trend": "RISING"
                if recent > before * 1.5 and recent - before >= 2
                else "FALLING"
                if before > recent * 1.5 and before - recent >= 2
                else "STABLE",
                "kind": "OBSERVED_FACT",
            }
        )
    return sorted(out, key=lambda x: (-x["devices_affected"], -x["occurrences"]))


# ---------------------------------------------------------------------------- capacity projection
def project(
    series: list[tuple[datetime, float]], *, limit: float | None, min_points: int = 7, horizon_days: int = 90
) -> dict[str, Any]:
    """Least-squares trend of a daily series with a residual-based band; INSUFFICIENT_DATA below
    ``min_points`` days. Never extrapolates beyond ``horizon_days``."""
    pts = sorted(series)
    if len(pts) < min_points:
        return {
            "status": "INSUFFICIENT_DATA",
            "points": len(pts),
            "needed": min_points,
            "current": pts[-1][1] if pts else None,
        }
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 86400 for t, _ in pts]
    ys = [v for _, v in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx if sxx else 0.0
    intercept = my - slope * mx
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys, strict=True)]
    sd = math.sqrt(sum(r * r for r in resid) / max(1, n - 2))
    slope_se = sd / math.sqrt(sxx) if sxx else float("inf")
    current = ys[-1]
    out: dict[str, Any] = {
        "status": "OK",
        "points": n,
        "current": current,
        "growth_per_day": round(slope, 3),
        "growth_per_day_range": [round(slope - 2 * slope_se, 3), round(slope + 2 * slope_se, 3)],
        "assumptions": "linear trend of the observed daily values continues; ±2 standard errors of the slope",
        "kind": "PREDICTION",
    }
    if limit is not None:
        out["limit"] = limit
        out["utilization"] = round(current / limit, 4) if limit else None
        if slope <= 0 or current >= limit:
            out["days_to_limit"] = 0 if current >= limit else None
        else:
            days = (limit - current) / slope
            fast = slope + 2 * slope_se
            out["days_to_limit"] = round(days) if days <= horizon_days else f">{horizon_days}"
            out["days_to_limit_earliest"] = round((limit - current) / fast) if fast > 0 else None
    review = pts[-1][0] + timedelta(days=30)
    out["review_by"] = review.date().isoformat()
    return out


def daily(points: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Sum values per UTC day (helper for event counts)."""
    acc: dict[datetime, float] = defaultdict(float)
    for t, v in points:
        acc[datetime(t.year, t.month, t.day, tzinfo=UTC)] += v
    return sorted(acc.items())
