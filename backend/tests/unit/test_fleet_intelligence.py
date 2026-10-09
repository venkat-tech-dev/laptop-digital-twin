"""Phase 10 fleet intelligence: explainable health, correlation statistics, recurring issues, capacity."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

from app.domain.fleet import intelligence as fi

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def dev(i: int, **kw: object) -> fi.DeviceHealthFacts:
    base: dict[str, object] = {"device_id": f"d{i:03d}", "health": "OK", "telemetry_age_s": 10.0}
    base.update(kw)
    return fi.DeviceHealthFacts(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------------ health
def test_healthy_average_never_hides_a_critical_device() -> None:
    facts = [dev(i) for i in range(19)] + [dev(19, health="CRITICAL", alerts={"CRITICAL": 1})]
    h = fi.fleet_health(facts)
    assert h["score"] >= 95 and h["band"] == "HEALTHY"  # 19 of 20 are fine
    assert h["critical_conditions"] == [
        {"device_id": "d019", "reasons": ["twin health CRITICAL", "1 critical open alert(s)"]}
    ]
    assert h["worst_devices"][0]["device_id"] == "d019" and h["worst_devices"][0]["score"] == 40
    assert isinstance(h["score"], int)  # no false precision


def test_stale_or_unknown_devices_lower_coverage_and_are_not_scored() -> None:
    facts = [dev(0), dev(1, telemetry_age_s=3600.0), dev(2, health=None), dev(3, telemetry_age_s=None)]
    h = fi.fleet_health(facts)
    assert h["scored"] == 1 and h["coverage"] == 0.25 and h["confidence"] == "LOW"
    assert sorted(h["unknown_devices"]) == ["d001", "d002", "d003"]
    assert fi.fleet_health([dev(0, telemetry_age_s=None)])["status"] == "INSUFFICIENT_DATA"


def test_deductions_are_capped_explained_and_weights_configurable() -> None:
    f = dev(0, health="WARNING", anomalies={"critical": 5}, predictions_24h=4, compliance="NON_COMPLIANT")
    score, ded = fi.device_score(f, fi.DEFAULT_WEIGHTS)
    by = {d["factor"]: d["points"] for d in ded}
    assert by == {"device_health": 15.0, "anomalies": 30.0, "predictions": 20.0, "compliance": 15.0}
    assert score == 20
    h = fi.fleet_health([f], weights={"non_compliant": 0.0})
    assert (
        h["score"] == 35 and h["weights"]["non_compliant"] == 0.0 and h["version"] == fi.HEALTH_MODEL_VERSION
    )


# ------------------------------------------------------------------ correlation
def test_hypergeometric_tail_matches_a_hand_computed_value() -> None:
    # N=10, K=4 successes, draw 3, P(X>=3) = C(4,3)C(6,0)/C(10,3) = 4/120
    assert math.isclose(fi.hypergeom_sf(3, 10, 4, 3), 4 / 120)
    assert fi.hypergeom_sf(0, 10, 4, 3) == 1.0


def _fleet(n: int, model_of: dict[int, str]) -> dict[str, dict[str, str]]:
    return {f"d{i:03d}": {"model": model_of.get(i, "Model-A"), "agent_version": "1.7.0"} for i in range(n)}


def test_burst_on_one_hardware_model_is_reported_as_an_association_not_a_cause() -> None:
    attrs = _fleet(60, {i: "Model-Z" for i in range(10)})  # 10 of 60 devices are Model-Z
    events = [fi.AnomalyFact(f"d{i:03d}", "memory:pressure", NOW + timedelta(minutes=i)) for i in range(8)]
    out = fi.correlate(events, attrs, org_id="acme")
    assert out["status"] == "OK" and len(out["insights"]) == 1
    ins = out["insights"][0]
    assert ins["observed_fact"].startswith("8 devices reported memory pressure within 7 minutes")
    top = ins["statistical_associations"][0]
    assert (top["dimension"], top["value"], top["devices_in_burst"]) == ("model", "Model-Z", 8)
    assert top["p_adjusted"] < 0.001 and top["strength"] == "STRONG"
    assert ins["causation"] == "NOT_ESTABLISHED" and "may be involved" in ins["possible_explanation"]
    # the attribute every device shares is never reported as an association
    assert all(a["dimension"] != "agent_version" for a in ins["statistical_associations"])
    again = fi.correlate(events, attrs, org_id="acme")
    assert again["insights"][0]["insight_id"] == ins["insight_id"]  # stable: no duplicate insights


def test_a_random_mix_of_devices_yields_no_association() -> None:
    rng = random.Random(4)
    attrs = {f"d{i:03d}": {"model": rng.choice(["A", "B", "C"])} for i in range(90)}
    burst = rng.sample(sorted(attrs), 12)
    events = [fi.AnomalyFact(d, "cpu:spike", NOW + timedelta(minutes=j)) for j, d in enumerate(burst)]
    ins = fi.correlate(events, attrs)["insights"][0]
    assert ins["statistical_associations"] == [] and "coincidence" in ins["possible_explanation"]


def test_small_fleets_and_small_bursts_produce_no_claims() -> None:
    assert fi.correlate([], _fleet(3, {}))["status"] == "INSUFFICIENT_DATA"
    attrs = _fleet(40, {})
    two = [fi.AnomalyFact("d000", "disk:full", NOW), fi.AnomalyFact("d001", "disk:full", NOW)]
    assert fi.correlate(two, attrs)["insights"] == []  # below the minimum burst size
    spread = [fi.AnomalyFact(f"d{i:03d}", "disk:full", NOW + timedelta(hours=i)) for i in range(5)]
    assert fi.correlate(spread, attrs)["insights"] == []  # not within one window
    other_tenant = [fi.AnomalyFact(f"x{i}", "disk:full", NOW) for i in range(5)]
    assert (
        fi.correlate(other_tenant, attrs)["insights"] == []
    )  # devices outside the attribute set are ignored


# ------------------------------------------------------------------ recurring + capacity
def test_recurring_issues_count_repeats_and_trend() -> None:
    alerts = [fi.AlertFact("d1", "disk_low", "HIGH", NOW - timedelta(days=d)) for d in (1, 2, 3, 4)]
    alerts += [fi.AlertFact("d2", "disk_low", "MEDIUM", NOW - timedelta(days=10))]
    alerts += [fi.AlertFact("d3", "once", "LOW", NOW)]
    out = fi.recurring_issues(alerts, NOW)
    assert len(out) == 1
    r = out[0]
    assert (r["alert_type"], r["occurrences"], r["devices_affected"], r["devices_with_repeats"]) == (
        "disk_low",
        5,
        2,
        1,
    )
    assert (r["last_7_days"], r["previous_7_days"], r["trend"], r["worst_severity"]) == (
        4,
        1,
        "RISING",
        "HIGH",
    )


def test_capacity_projection_needs_history_and_states_uncertainty() -> None:
    short = [(NOW - timedelta(days=d), 100.0) for d in range(3)]
    assert fi.project(short, limit=1000)["status"] == "INSUFFICIENT_DATA"
    series = [(NOW - timedelta(days=13 - d), 100.0 + 10 * d + (1 if d % 2 else -1)) for d in range(14)]
    p = fi.project(series, limit=500)
    assert p["status"] == "OK" and 9.5 < p["growth_per_day"] < 10.5
    lo, hi = p["growth_per_day_range"]
    assert lo < p["growth_per_day"] < hi
    assert 25 <= p["days_to_limit"] <= 28 and p["days_to_limit_earliest"] <= p["days_to_limit"]
    assert p["kind"] == "PREDICTION" and "assumptions" in p
    flat = fi.project([(NOW - timedelta(days=d), 50.0) for d in range(10)], limit=100)
    assert flat["days_to_limit"] is None  # no growth: no invented exhaustion date
