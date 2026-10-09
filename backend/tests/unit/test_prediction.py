"""Phase 5 - forecasting domain: acceptance tests of the specification, models, crossing, confidence,
severity, data quality, lifecycle, calibration and backtest leakage."""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.prediction.backtest import actual_crossings, backtest
from app.domain.prediction.engine import Assessment, PredictionPolicy, PredictionTracker, assess
from app.domain.prediction.forecasters import fit_ewma, fit_holt, fit_naive, fit_trend, select
from app.domain.prediction.preprocess import BucketSeries, prepare
from app.domain.prediction.scoring import band, confidence, severity, time_to_threshold
from app.domain.prediction.targets import TARGETS_BY_ID, Target
from app.repositories.predictions import calibration

P = PredictionPolicy()
NOW = 1_760_000_000.0


def interp(anchors: list[float], per: int) -> list[float]:
    out: list[float] = []
    for a, b in itertools.pairwise(anchors):
        out += [a + (b - a) * k / per for k in range(per)]
    return [*out, anchors[-1]]


def series_of(
    tid: str, values: list[float], step: float, noise: float = 0.0, stale: float = 0.0, now: float = NOW
) -> tuple[Target, BucketSeries]:
    t = TARGETS_BY_ID[tid]
    rng = random.Random(1)
    s = BucketSeries(t.bucket_s, 5000)
    n = len(values)
    s.add(
        [(now - stale - (n - 1 - i) * step, v + rng.gauss(0, noise)) for i, v in enumerate(values)],
        t.plausible,
        now,
    )
    return t, s


def run(tid: str, values: list[float], step: float, **kw: float) -> Assessment:
    t, s = series_of(tid, values, step, **kw)
    return assess(t, prepare(s, t, NOW), NOW, P)


# ---------------------------------------------------------------- acceptance tests (spec section 51)
def test_1_disk_growth_gives_a_crossing_with_a_range() -> None:
    a = run("disk", interp([70, 71, 72, 73, 74, 75], 96), 900, noise=0.05)
    assert a.status == "AVAILABLE" and a.crossing is not None and a.fitted and a.fitted.model == "trend"
    assert 10 * 86400 < a.crossing.eta_s < 20 * 86400  # +1 %/day from 75 % -> ~15 days
    assert a.crossing.earliest_s < a.crossing.eta_s < (a.crossing.latest_s or 0)  # a range, never exact
    assert a.conf is not None and a.conf.band in ("MEDIUM", "HIGH")


def test_2_stable_disk_has_no_meaningful_crossing() -> None:
    a = run("disk", interp([72, 72, 72, 73, 72], 120), 900, noise=0.05)
    assert a.crossing is None


def test_3_ram_spike_is_not_imminent_exhaustion() -> None:
    a = run("memory", interp([50, 52, 91, 55, 51], 6), 60, noise=0.5)
    assert a.crossing is None and a.fitted and a.fitted.model != "trend"


def test_4_sustained_ram_growth_predicts_a_crossing() -> None:
    a = run("memory", interp([60, 64, 68, 73, 78, 83], 5), 60, noise=0.4)
    assert a.status == "AVAILABLE" and a.crossing is not None and a.crossing.eta_s < 20 * 60
    assert a.severity in ("MEDIUM", "HIGH")


def test_oscillating_ram_is_not_a_trend() -> None:
    a = run("memory", [65, 82, 61, 85, 63] * 6, 60)
    assert a.crossing is None


def test_5_battery_charging_is_not_applicable() -> None:
    t, s = series_of("battery", interp([20, 22, 24, 25], 5), 60)
    a = assess(t, prepare(s, t, NOW), NOW, P, not_applicable="on AC power / charging: no discharge forecast")
    assert a.status == "NOT_APPLICABLE" and a.crossing is None


def test_6_battery_discharging_predicts_time_to_critical() -> None:
    a = run("battery", interp([60, 55, 50, 45, 40], 5), 60)
    assert a.status == "AVAILABLE" and a.crossing is not None
    assert 25 * 60 < a.crossing.eta_s < 35 * 60  # 1 %/min from 40 % to 10 %


def test_7_stale_sensor_gives_no_prediction() -> None:
    a = run("temperature", [80 + i * 0.2 for i in range(40)], 15, stale=30 * 60)
    assert a.status == "STALE_DATA" and a.crossing is None and "30 min" in a.reason


def test_8_disk_cleanup_restarts_history_and_invalidates() -> None:
    t = TARGETS_BY_ID["disk"]
    tracker = PredictionTracker(P, id_factory=lambda: "p1")
    before = interp([86, 87, 88, 89], 96)
    s = BucketSeries(t.bucket_s, 5000)
    t0 = NOW - len(before) * 900
    s.add([(t0 + i * 900, v) for i, v in enumerate(before)], t.plausible, NOW - 900)
    a1 = assess(t, prepare(s, t, NOW - 900), NOW - 900, P)
    trs = tracker.step("dev", a1, datetime.fromtimestamp(NOW - 900, UTC))
    assert [x.kind for x in trs] == ["created"]
    s.add([(NOW - 899 + i * 60, 72.0) for i in range(14)], t.plausible, NOW)  # cleanup: -17 points
    a2 = assess(t, prepare(s, t, NOW), NOW, P)
    assert a2.status == "INSUFFICIENT_HISTORY" and "regime" in a2.reason
    trs2 = tracker.step("dev", a2, datetime.fromtimestamp(NOW, UTC))
    assert [x.kind for x in trs2] == ["invalidated"] and "regime change" in (trs2[0].prediction.reason or "")


def test_thermal_warmup_that_levels_off_is_not_extrapolated() -> None:
    # 50 -> 75 C quickly, then flattening (fans): a saturating curve must not become "90 C in 3 min"
    vals = [75 - 25 * 0.82**i for i in range(30)]
    a = run("temperature", vals, 30, noise=0.1)
    assert a.crossing is None


# ---------------------------------------------------------------- models
def _line(n: int, slope: float, noise: float = 0.0, seed: int = 2) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    return [(i * 60.0, 10 + slope * i + rng.gauss(0, noise)) for i in range(n)]


def test_models_fit_known_series() -> None:
    pts = _line(60, 0.5, 0.2)
    tr = fit_trend(pts, 60, 0.05)
    assert tr.slope * 60 == pytest.approx(0.5, abs=0.02)
    assert tr.slope_lo is not None and tr.slope_hi is not None and tr.slope_lo < tr.slope < tr.slope_hi
    assert tr.mean(600) == pytest.approx(10 + 0.5 * 69, abs=0.6)
    assert fit_naive(pts, 60, 0.05).mean(600) == pts[-1][1]
    ew = fit_ewma(pts, 60, 0.05)
    assert abs(ew.mean(600) - pts[-1][1]) < 2
    holt = fit_holt(pts, 60, 0.05)
    assert holt.mean(600) > pts[-1][1]  # follows the trend (damped)
    for f in (tr, ew, holt):
        lo1, hi1 = f.bounds(60)
        lo2, hi2 = f.bounds(3600)
        assert hi2 - lo2 > hi1 - lo1  # uncertainty widens with the horizon


def test_robust_trend_ignores_a_spike() -> None:
    pts = _line(40, 0.0, 0.1)
    pts[20] = (pts[20][0], 95.0)
    assert abs(fit_trend(pts, 60, 0.05).slope * 3600) < 1.0


def test_selection_prefers_the_simplest_adequate_model() -> None:
    flat = _line(60, 0.0, 0.5)
    sel = select(flat, ("naive", "ewma", "trend", "holt"), 60, 600, 0.05)
    best = min(sel.mae.values())
    order = ("naive", "ewma", "trend", "holt")
    assert sel.mae[sel.model] <= best * 1.1 + 1e-9 and sel.origins >= 3
    # no simpler model was within the 10 % tolerance
    assert all(sel.mae[m] > best * 1.1 + 1e-9 for m in order[: order.index(sel.model)])
    growing = _line(60, 1.0, 0.2)
    assert select(growing, ("naive", "trend"), 60, 600, 0.05).model == "trend"
    assert select(growing[:8], ("naive", "trend"), 60, 600, 0.05).model == ""  # too little to evaluate


# ---------------------------------------------------------------- crossing, confidence, severity
def test_crossing_is_none_when_never_reached_or_wrong_direction() -> None:
    up = fit_trend(_line(60, 0.5), 60, 0.05)
    cr = time_to_threshold(up, 60.0, "up", 24 * 3600)
    assert cr is not None and cr.eta_s == pytest.approx((60 - up.level) / up.slope, rel=0.01)
    assert time_to_threshold(up, 60.0, "up", 60) is None  # beyond the horizon: no invented time
    assert time_to_threshold(up, 0.0, "down", 24 * 3600) is None  # rising never reaches a lower bound
    flat = fit_naive(_line(60, 0.0), 60, 0.05)
    assert time_to_threshold(flat, 90.0, "up", 86400) is None


def test_confidence_factors_and_bands() -> None:
    c_good = confidence(
        span_s=3 * 86400,
        min_history_s=86400,
        coverage=1.0,
        skill=0.9,
        sign_agreement=0.95,
        eta_s=86400,
        crossing=None,
        volatile_anomaly=False,
    )
    c_bad = confidence(
        span_s=86400,
        min_history_s=86400,
        coverage=0.5,
        skill=0.5,
        sign_agreement=0.65,
        eta_s=30 * 86400,
        crossing=None,
        volatile_anomaly=True,
    )
    assert c_good.value > 0.8 > c_bad.value
    assert set(c_good.factors) == {"history", "quality", "skill", "stability", "horizon", "precision"}
    assert c_bad.factors["volatile_anomaly"] == 0.85
    assert [band(x) for x in (0.3, 0.6, 0.8)] == ["LOW", "MEDIUM", "HIGH"]


def test_severity_is_deterministic_and_confidence_aware() -> None:
    bands = TARGETS_BY_ID["disk"].severity_bands_s
    day = 86400
    assert severity(200 * day, bands, 0.9, False) is None
    assert severity(100 * day, bands, 0.9, False) == "INFO"
    assert severity(20 * day, bands, 0.9, False) == "LOW"
    assert severity(3 * day, bands, 0.9, False) == "MEDIUM"
    assert severity(8 * 3600, bands, 0.9, False) == "HIGH"
    assert severity(8 * 3600, bands, 0.9, True) == "CRITICAL"
    assert severity(8 * 3600, bands, 0.3, True) == "MEDIUM"  # low confidence: never critical, one step down
    assert severity(8 * 3600, bands, 0.3, False) == "MEDIUM"


# ---------------------------------------------------------------- data quality
def test_preprocessing_rejects_bad_samples() -> None:
    t = TARGETS_BY_ID["battery"]
    s = BucketSeries(t.bucket_s, 100)
    s.add([(NOW - 120, 50.0), (NOW - 60, 49.0)], t.plausible, NOW)
    s.add([(NOW - 90, 70.0), (NOW - 60, 49.0)], t.plausible, NOW)  # out of order + duplicate
    s.add([(NOW + 600, 10.0)], t.plausible, NOW)  # clock skew: from the future
    s.add([(NOW - 30, float("nan")), (NOW - 20, 4294967295.0), (NOW - 10, 140.0)], t.plausible, NOW)
    assert (s.dropped_out_of_order, s.dropped_future, s.dropped_invalid) == (2, 1, 3)
    assert [round(v) for _, v in s.series("last", 0)] == [50, 49]


def test_insufficient_history_is_explicit() -> None:
    a = run("memory", [60, 61, 62], 60)
    assert a.status == "INSUFFICIENT_HISTORY" and "needs" in a.reason


# ---------------------------------------------------------------- lifecycle
def _assessment(eta_min: float) -> Assessment:
    values = interp([60, 66, 72, 78, 84], 5)
    return run("memory", values, 60, noise=0.3) if eta_min else run("memory", [70.0] * 25, 60)


def test_lifecycle_dedupes_updates_confirms_and_records_calibration() -> None:
    tracker = PredictionTracker(P, id_factory=lambda: "p-1")
    t = TARGETS_BY_ID["memory"]
    now = datetime.fromtimestamp(NOW, UTC)
    a = _assessment(10)
    created = tracker.step("dev", a, now)
    assert [x.kind for x in created] == ["created"]
    p = created[0].prediction
    assert p.status == "ACTIVE" and p.correlation_key == f"dev:memory:resource_exhaustion:{t.thresholds[0]:g}"
    # the same forecast again a minute later: no new record, no event (no thrashing)
    assert tracker.step("dev", a, now + timedelta(seconds=60)) == []
    assert len(tracker.active("dev")) == 1
    # reality: memory reaches 90 % -> CONFIRMED with timing error and lead time
    hit = run("memory", interp([60, 70, 80, 91], 6), 60)
    trs = tracker.step("dev", hit, now + timedelta(minutes=7))
    assert [x.kind for x in trs] == ["confirmed"]
    c = trs[0].prediction
    assert c.status == "CONFIRMED" and c.lead_time_s == 420 and c.timing_error_s is not None
    stats = calibration([c])
    assert stats["overall"]["confirmed"] == 1 and stats["overall"]["hit_rate"] == 1.0


def test_lifecycle_invalidates_with_hysteresis_and_expires() -> None:
    tracker = PredictionTracker(P, id_factory=lambda: "p-2")
    now = datetime.fromtimestamp(NOW, UTC)
    assert tracker.step("dev", _assessment(10), now)[0].kind == "created"
    flat = _assessment(0)  # trend stopped
    assert tracker.step("dev", flat, now + timedelta(minutes=1)) == []  # one weak evaluation is not enough
    assert tracker.step("dev", flat, now + timedelta(minutes=2)) == []
    trs = tracker.step("dev", flat, now + timedelta(minutes=3))
    assert [x.kind for x in trs] == ["invalidated"] and trs[0].prediction.reason
    # expiry: the latest likely crossing passes without the threshold being reached
    assert tracker.step("dev", _assessment(10), now)[0].kind == "created"
    p = tracker.active("dev")[0]
    assert p.expires_at is not None
    trs2 = tracker.step("dev", _assessment(10), p.expires_at + timedelta(seconds=1))
    assert [x.kind for x in trs2] == ["expired"]


def test_charging_invalidates_a_battery_prediction() -> None:
    tracker = PredictionTracker(P, id_factory=lambda: "p-3")
    now = datetime.fromtimestamp(NOW, UTC)
    assert tracker.step("dev", run("battery", interp([60, 55, 50, 45, 40], 5), 60), now)[0].kind == "created"
    t, s = series_of("battery", interp([60, 55, 50, 45, 40], 5), 60)
    plugged = assess(
        t, prepare(s, t, NOW), NOW, P, not_applicable="on AC power / charging: no discharge forecast"
    )
    trs = tracker.step("dev", plugged, now + timedelta(seconds=60))
    assert [x.kind for x in trs] == ["invalidated"] and "charging" in (trs[0].prediction.reason or "")


# ---------------------------------------------------------------- backtesting
def test_crossing_episodes_use_a_rearm_margin() -> None:
    pts = [(float(i), v) for i, v in enumerate([80, 91, 89, 91, 89.5, 92, 85, 91])]
    assert actual_crossings(pts, 90, True, 3.0) == [1.0, 7.0]


def test_backtest_has_no_future_leakage_and_scores_crossings() -> None:
    t = TARGETS_BY_ID["battery"]
    rng = random.Random(3)
    raw = [(NOW + i * 5.0, 100 - i * 5 / 60 + rng.gauss(0, 0.02)) for i in range(int(95 * 60 / 5))]
    r1 = backtest(raw, t)
    cr = r1["crossings"]
    assert cr["actual_crossings"] == 1 and cr["missed_crossings"] == 0  # 1 %/min reaches 10 % once
    assert cr["matched_forecasts"] > 0 and cr["median_abs_timing_error_s"] < 300
    assert r1["models"]["trend"]["mae"] < r1["models"]["naive"]["mae"]
    # changing the data after a point must not change any forecast issued before it
    cut_t = raw[len(raw) // 2][0]
    altered = [(tt, v if tt < cut_t else 100.0) for tt, v in raw]
    r2 = backtest(altered, t)
    before1 = [x for x in r1["forecast_log"] if x[0] < cut_t]
    before2 = [x for x in r2["forecast_log"] if x[0] < cut_t]
    assert before1 and before1 == before2


def test_recurring_spikes_give_an_exceedance_probability_not_a_trend() -> None:
    # RAM on a high plateau with short excursions above 90 % (the real pattern of this laptop)
    vals = [87.0, 86.5, 88.0, 92.0, 87.5, 86.0, 87.0, 91.5, 86.5, 87.0] * 6
    a = run("memory", vals, 60)
    assert a.crossing is None and a.forecast is not None
    pe = a.forecast["exceedance_probability"]
    assert pe is not None and pe >= 0.9 and "recurring spikes" in a.reason
    calm = run("memory", [70.0, 71.0, 70.5] * 20, 60)
    assert calm.forecast is not None and calm.forecast["exceedance_probability"] == 0.0


def test_no_new_prediction_until_the_value_falls_back_after_confirmation() -> None:
    tracker = PredictionTracker(P, id_factory=lambda: "p-r")
    now = datetime.fromtimestamp(NOW, UTC)
    assert tracker.step("dev", _assessment(10), now)[0].kind == "created"
    hit = run("memory", interp([60, 70, 80, 91], 6), 60)
    assert tracker.step("dev", hit, now + timedelta(minutes=5))[0].kind == "confirmed"
    # still near the threshold with a rising trend: repeating the forecast would only restate reality
    near = run("memory", interp([75, 80, 85, 89], 6), 60)  # 89 %: above 90 - rearm (87 %)
    assert tracker.step("dev", near, now + timedelta(minutes=6)) == []
    low = run("memory", [70.0] * 25, 60)  # back well below 90 - rearm
    assert tracker.step("dev", low, now + timedelta(minutes=30)) == []
    assert tracker.step("dev", _assessment(10), now + timedelta(minutes=31))[0].kind == "created"
