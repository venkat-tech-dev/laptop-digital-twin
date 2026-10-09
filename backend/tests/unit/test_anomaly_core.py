"""Phase 4 - statistics, baselines, scoring, data quality and the Isolation Forest (pure units)."""

from __future__ import annotations

import json
import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.anomalies.baseline import BaselineStatus, build_signal_baseline, context_keys, fleet_baseline
from app.domain.anomalies.iforest import (
    IsolationForest,
    MultivariateModel,
    RobustScaler,
    c_factor,
    train_model,
)
from app.domain.anomalies.models import Level
from app.domain.anomalies.observation import QualityIssue, observe
from app.domain.anomalies.policy import AnomalyPolicy
from app.domain.anomalies.scoring import COLD_CONFIDENCE_CAP, confidence_band, deviation_points, score
from app.domain.anomalies.signals import SIGNALS_BY_ID
from app.domain.anomalies.stats import (
    Ewma,
    Summary,
    mad,
    median,
    quantile,
    robust_scale,
    robust_z,
    slope_per_minute,
)

P = AnomalyPolicy()
T0 = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)  # a Monday


# ------------------------------------------------------------------------------------ stats
def test_median_mad_quantile_robust_z() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert median(xs) == 3.0
    assert median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert mad(xs) == 1.0  # |x - 3| = 2,1,0,1,97 -> median 1: the outlier does not inflate the spread
    s = sorted(xs)
    assert quantile(s, 0.0) == 1.0 and quantile(s, 1.0) == 100.0
    assert quantile(s, 0.5) == 3.0
    assert quantile([0.0, 10.0], 0.25) == 2.5  # linear interpolation
    assert robust_scale(0.0, 1.5) == 1.5  # flat history: floor, never division by zero
    assert robust_scale(2.0, 0.1) == pytest.approx(2.9652)
    assert robust_z(10.0, 4.0, 2.0) == 3.0
    with pytest.raises(ValueError):
        median([])


def test_summary_rolling_quantiles_and_ewma_and_slope() -> None:
    xs = [float(i) for i in range(101)]
    s = Summary.of(xs)
    assert (s.count, s.median, s.p05, s.p95, s.p99) == (101, 50.0, 5.0, 95.0, 99.0)
    assert Summary.of([1.0, float("nan"), 3.0]).count == 2  # non-finite values are ignored
    e = Ewma(0.5)
    assert e.update(10.0) == 10.0 and e.update(20.0) == 15.0 and e.n == 2
    with pytest.raises(ValueError):
        Ewma(0.0)
    pts = [(t * 60.0, 2.0 * t + 1) for t in range(10)]
    assert slope_per_minute(pts) == pytest.approx(2.0)
    assert slope_per_minute(pts[:2]) is None


# -------------------------------------------------------------------------------- baselines
def _minutes(n: int, start: datetime = T0, f=lambda i: 20.0 + (i % 7)) -> list[tuple[datetime, float]]:
    return [(start + timedelta(minutes=i), f(i)) for i in range(n)]


def test_baseline_status_cold_developing_stable_degraded() -> None:
    assert build_signal_baseline("cpu", _minutes(30), [], P, "v").status is BaselineStatus.COLD
    dev = build_signal_baseline("cpu", _minutes(600), [], P, "v")
    assert dev.status is BaselineStatus.DEVELOPING and dev.sample_count == 600
    stable = build_signal_baseline("cpu", _minutes(8 * 1440), [], P, "v")
    assert stable.status is BaselineStatus.STABLE
    # most of the history is an incident: excluded, and the baseline is flagged DEGRADED
    pts = _minutes(1000)
    incident = [(T0 + timedelta(minutes=100), T0 + timedelta(minutes=600))]
    deg = build_signal_baseline("cpu", pts, incident, P, "v")
    assert deg.status is BaselineStatus.DEGRADED and deg.excluded_count == 501


def test_contamination_prevention_excludes_incident_minutes() -> None:
    normal = _minutes(600, f=lambda i: 20.0 + (i % 5))
    with_incident = [(t, 95.0 if 200 <= i < 260 else v) for i, (t, v) in enumerate(normal)]
    window = [(T0 + timedelta(minutes=200), T0 + timedelta(minutes=259))]
    clean = build_signal_baseline("cpu", with_incident, window, P, "v")
    dirty = build_signal_baseline("cpu", with_incident, [], P, "v")
    assert clean.contexts["all"].stats.p99 < 30  # the outage did not redefine "normal"
    assert dirty.contexts["all"].stats.p99 > 90


def test_context_is_time_of_day_and_day_type_aware_with_fallback() -> None:
    assert context_keys(datetime(2026, 10, 5, 10, tzinfo=UTC)) == ["how:wd:10", "dt:wd", "all"]
    assert context_keys(datetime(2026, 10, 10, 23, tzinfo=UTC))[0] == "how:we:23"
    # 3 weekdays: busy 09-17 (60 %), quiet otherwise (10 %)
    pts = _minutes(3 * 1440, f=lambda i: 60.0 if 9 <= (i // 60) % 24 < 17 else 10.0)
    b = build_signal_baseline("cpu", pts, [], P, "v")
    busy = b.for_time(datetime(2026, 10, 7, 10, tzinfo=UTC), P.min_context_samples)
    quiet = b.for_time(datetime(2026, 10, 7, 3, tzinfo=UTC), P.min_context_samples)
    assert busy is not None and quiet is not None
    assert (busy.context, busy.stats.median) == ("how:wd:10", 60.0)
    assert quiet.stats.median == 10.0
    weekend = b.for_time(datetime(2026, 10, 10, 10, tzinfo=UTC), P.min_context_samples)
    assert weekend is not None and weekend.context == "all"  # no weekend history yet: fallback


def test_fleet_baseline_for_cold_start() -> None:
    devices = [
        build_signal_baseline("cpu", _minutes(600, f=lambda i, k=k: 10.0 + k + (i % 3)), [], P, "v")
        for k in (0, 5, 10)
    ]
    fb = fleet_baseline("cpu", devices, "fleet", T0)
    assert fb is not None and fb.source == "fleet" and fb.status is BaselineStatus.COLD
    assert fleet_baseline("cpu", [], "fleet", T0) is None


# ---------------------------------------------------------------------------------- scoring
def _score(**kw):
    base = dict(
        policy=P,
        deviation_points=1,
        deviation_reason="r",
        evidence=0.9,
        held_s=200,
        correlated=0,
        value=50.0,
        warning_level=90.0,
        critical_level=95.0,
        impact=1,
        baseline_status=BaselineStatus.STABLE,
        baseline_samples=5000,
        coverage=1.0,
    )
    base.update(kw)
    return score(**base)


def test_severity_points_are_deterministic_and_explained() -> None:
    assert deviation_points(4) == 1 and deviation_points(6) == 2 and deviation_points(9) == 3
    low = _score()
    assert low.level is Level.LOW and low.points == 2
    high = _score(deviation_points=3, held_s=1000, correlated=1)  # 3 + 2 + 1 + 1 = 7
    assert high.level is Level.HIGH  # within safety limits: CRITICAL is capped to HIGH
    assert any(b["factor"] == "cap" for b in high.breakdown)
    crit = _score(deviation_points=3, held_s=1000, correlated=1, value=96.0)
    assert crit.level is Level.CRITICAL
    beyond = _score(deviation_points=1, held_s=0, impact=0, value=96.0)  # 1 + 2 safety = 3 -> MEDIUM -> HIGH
    assert beyond.level is Level.HIGH
    assert {b["factor"] for b in crit.breakdown} >= {
        "deviation",
        "persistence",
        "correlation",
        "safety",
        "impact",
    }
    assert _score() == _score()  # same inputs, same verdict


def test_confidence_is_evidence_based_with_bands_and_cold_cap() -> None:
    strong = _score(evidence=0.99, held_s=400, coverage=1.0)
    weak = _score(
        evidence=0.5,
        held_s=180,
        coverage=0.5,
        baseline_status=BaselineStatus.DEVELOPING,
        baseline_samples=200,
    )
    assert strong.confidence > weak.confidence
    assert set(strong.confidence_factors) == {"evidence", "persistence", "baseline", "data_quality"}
    cold = _score(evidence=0.99, held_s=1000, baseline_status=BaselineStatus.COLD)
    assert cold.confidence <= COLD_CONFIDENCE_CAP and cold.confidence_band in ("LOW", "MODERATE")
    assert [confidence_band(c, P) for c in (0.2, 0.5, 0.8, 0.95)] == ["LOW", "MODERATE", "HIGH", "VERY HIGH"]


# ------------------------------------------------------------------------------ data quality
CPU = SIGNALS_BY_ID["cpu"]


def test_observation_quality_gates() -> None:
    now = 10_000.0
    good = [(now - 120 + 5 * i, 20.0) for i in range(1, 25)]
    obs, q = observe(CPU, good, now, 120, 5, 25, 0.5, True)
    assert q is QualityIssue.OK and obs is not None and obs.value == 20.0 and obs.coverage == 1.0
    assert observe(CPU, good, now, 120, 5, 25, 0.5, False) == (None, QualityIssue.DEVICE_NOT_CONNECTED)
    assert observe(CPU, [], now, 120, 5, 25, 0.5, True)[1] is QualityIssue.NO_DATA
    stale = [(t - 100, v) for t, v in good if t < now - 100]
    assert observe(CPU, stale, now, 120, 5, 25, 0.5, True)[1] is QualityIssue.STALE
    sparse = good[-5:]
    assert observe(CPU, sparse, now, 120, 5, 25, 0.5, True)[1] is QualityIssue.INSUFFICIENT
    bad = [(t, 250.0) for t, _ in good]
    assert observe(CPU, bad, now, 120, 5, 25, 0.5, True)[1] is QualityIssue.IMPOSSIBLE
    mixed = [(t, float("nan") if i % 4 == 0 else 20.0) for i, (t, _) in enumerate(good)]
    obs2, q2 = observe(CPU, mixed, now, 120, 5, 25, 0.5, True)
    assert q2 is QualityIssue.OK and obs2 is not None and obs2.dropped_impossible == 6 and obs2.value == 20.0
    future = [(now + 60, 99.0), *good]  # a sample from the future (clock skew) is never used
    obs3, _ = observe(CPU, sorted(future), now, 120, 5, 25, 0.5, True)
    assert obs3 is not None and obs3.value == 20.0


# ----------------------------------------------------------------------------- Isolation Forest
def _rows(n: int, seed: int = 1) -> list[list[float]]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        cpu = rng.uniform(5, 40)
        out.append([cpu, 42 + 0.35 * cpu + rng.gauss(0, 0.5), rng.gauss(55, 1)])
    return out


def test_isolation_forest_scores_outliers_higher_and_is_deterministic() -> None:
    assert c_factor(1) == 0.0 and c_factor(2) == 1.0 and c_factor(256) > c_factor(128)
    rows = _rows(800)
    for extended in (False, True):
        a = IsolationForest.fit(rows, 50, 128, seed=3, extended=extended)
        b = IsolationForest.fit(rows, 50, 128, seed=3, extended=extended)
        normal, outlier = [20.0, 49.0, 55.0], [95.0, 49.0, 80.0]
        assert a.score(outlier) > a.score(normal) + 0.1
        assert a.score(normal) == b.score(normal)  # deterministic for a seed
    with pytest.raises(ValueError):
        IsolationForest.fit([], 10, 64, 1)


def test_relation_feature_detects_broken_relationships() -> None:
    rows = _rows(1500)
    m = train_model(
        "dev",
        1,
        ["cpu", "temperature", "memory"],
        rows,
        [3.0, 1.5, 1.5],
        "a",
        "b",
        64,
        128,
        0.995,
        7,
        relations=[("temperature", "cpu", 0.75)],
    )
    assert m.relations and m.relations[0].slope == pytest.approx(0.35, abs=0.02)
    consistent, broken = [30.0, 52.5, 55.0], [30.0, 44.0, 55.0]
    assert m.score(consistent) < m.threshold < m.score(broken)
    assert m.contributions(broken)[0][0] == "temperature~cpu"


def test_model_persistence_roundtrip_versioning_and_artifact_validation() -> None:
    rows = _rows(900)
    m = train_model(
        "dev",
        4,
        ["cpu", "temperature", "memory"],
        rows,
        [3.0, 1.5, 1.5],
        "a",
        "b",
        16,
        64,
        0.99,
        7,
        relations=[("temperature", "cpu", 0.75)],
    )
    blob = json.loads(json.dumps(m.to_dict()))  # JSON only: no pickle, nothing executable
    m2 = MultivariateModel.from_dict(blob)
    assert (m2.model_id, m2.version, m2.params["algorithm"]) == (
        "iforest-dev-v4",
        4,
        "extended_isolation_forest",
    )
    for x in ([20.0, 49.0, 55.0], [80.0, 40.0, 70.0]):
        assert m2.score(x) == pytest.approx(m.score(x), abs=1e-6)
    bad = json.loads(json.dumps(blob))
    bad["forest"]["trees"][0]["l"][0] = 10**6  # child index out of range
    with pytest.raises(ValueError):
        MultivariateModel.from_dict(bad)
    bad2 = json.loads(json.dumps(blob))
    bad2["scaler"]["centers"] = [0.0]  # dimension mismatch
    with pytest.raises(ValueError):
        MultivariateModel.from_dict(bad2)


def test_scaler_handles_constant_features_with_floor() -> None:
    sc = RobustScaler.fit([[1.0, 5.0], [2.0, 5.0], [3.0, 5.0]], [0.1, 2.0])
    assert sc.scales[1] == 2.0  # constant column: floor, not zero
    assert all(math.isfinite(v) for v in sc.transform([10.0, 9.0]))


def test_feature_selection_drops_the_shortest_history_first() -> None:
    from app.domain.anomalies.iforest import select_features

    rows = {
        "cpu": dict.fromkeys(range(1000), 1.0),
        "memory": dict.fromkeys(range(1000), 1.0),
        "net_latency": dict.fromkeys(range(300), 1.0),
    }
    features, common = select_features(rows, 720)
    assert features == ["cpu", "memory"] and len(common) == 1000
    short = {"cpu": dict.fromkeys(range(10), 1.0), "gpu": dict.fromkeys(range(10), 1.0)}
    assert select_features(short, 720)[1] == []
