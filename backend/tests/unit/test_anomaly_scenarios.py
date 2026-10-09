"""Phase 4 - evaluation scenarios A-G replayed through the live detection code (synthetic data,
evaluation only). Thresholds here are the acceptance criteria of the specification."""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.anomalies.replay import run_scenario, scenarios


@pytest.fixture(scope="module")
def results() -> dict[str, dict[str, Any]]:
    return {sc.name[0]: run_scenario(sc) for sc in scenarios()}


def test_a_sustained_cpu_is_detected_quickly(results: dict[str, dict[str, Any]]) -> None:
    m = results["A"]["metrics"]
    assert m["recall"] == 1.0 and m["false_positives"] == 0
    assert m["detection_latency_s"]["cpu_80_90_20min"] <= 300  # persistence 180 s + 2-min window


def test_b_two_second_spike_is_not_a_behavioral_anomaly(results: dict[str, dict[str, Any]]) -> None:
    assert results["B"]["metrics"]["anomalies_reported"] == 0


def test_c_cpu_and_temperature_form_one_correlated_incident(results: dict[str, dict[str, Any]]) -> None:
    r = results["C"]
    assert r["metrics"]["recall"] == 1.0 and r["metrics"]["false_positives"] == 0
    signals = {a["signal_id"] for a in r["anomalies"]}
    assert {"cpu", "temperature"} <= signals
    keys = {a["correlation_key"] for a in r["anomalies"] if a["signal_id"] in ("cpu", "temperature")}
    assert len(keys) == 1 and None not in keys


def test_d_new_device_has_cold_baseline_and_no_confident_anomalies(
    results: dict[str, dict[str, Any]],
) -> None:
    r = results["D"]
    assert set(r["baseline_status"].values()) == {"COLD"}
    assert all((a["confidence"] or 0) <= 0.45 for a in r["anomalies"])


def test_e_memory_leak_level_shift(results: dict[str, dict[str, Any]]) -> None:
    m = results["E"]["metrics"]
    assert m["recall"] == 1.0 and m["false_positives"] == 0


def test_f_erratic_cpu_is_a_volatility_anomaly(results: dict[str, dict[str, Any]]) -> None:
    r = results["F"]
    assert r["metrics"]["recall"] == 1.0 and r["metrics"]["false_positives"] == 0
    assert "volatility_anomaly" in {a["type"] for a in r["anomalies"]}


def test_g_broken_relationship_needs_the_multivariate_model(results: dict[str, dict[str, Any]]) -> None:
    r = results["G"]
    assert r["metrics"]["recall"] == 1.0 and r["metrics"]["false_positives"] == 0
    assert {a["type"] for a in r["anomalies"]} == {"multivariate_anomaly"}  # no single value is unusual
    assert r["model"] is not None


def test_alert_volume_is_bounded(results: dict[str, dict[str, Any]]) -> None:
    for r in results.values():
        alerts = r["anomalies"]
        incidents = {a["correlation_key"] or a["title"] for a in alerts}
        assert len(incidents) <= 2  # one injected incident -> at most two correlated groups
        assert len(alerts) <= 4
        assert r["data"] == "synthetic"


def test_peak_confidence_is_kept_after_recovery(results: dict[str, dict[str, Any]]) -> None:
    a = results["A"]
    cpu = next(x for x in a["anomalies"] if x["signal_id"] == "cpu")
    assert cpu["lifecycle"] == "RESOLVED" and cpu["confidence"] >= 0.9
