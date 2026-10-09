from datetime import UTC, datetime, timedelta

from app.domain.anomalies.engine import AnomalyEngine
from app.domain.anomalies.models import Severity
from app.domain.anomalies.rules import ThresholdRule
from app.domain.anomalies.statistical import StatisticalDetector, StatisticalSpec
from app.domain.components.models import HealthStatus
from app.domain.health.engine import HealthEngine, status_for
from app.domain.telemetry.models import MetricReading, MetricWindow, Quality
from app.schemas.ingest import InventoryEnvelopeIn, TelemetryBatchIn
from app.services.digital_twin import DigitalTwinService
from tests.conftest import batch, inventory_envelope, sample, settings, typical_samples

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def reading(metric: str, value: float | str, t: float, labels: dict[str, str] | None = None) -> MetricReading:
    key = (
        metric if not labels else metric + "{" + ",".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}"
    )
    return MetricReading(
        key,
        metric,
        "cpu",
        value,
        "celsius",
        T0 + timedelta(seconds=t),
        "test",
        Quality.GOOD,
        True,
        "measured",
        labels=labels or {},
    )


# ---------------------------------------------------------------- health
def _twin_with(samples: list) -> DigitalTwinService:  # type: ignore[type-arg]
    svc = DigitalTwinService(settings())
    svc.apply_inventory(InventoryEnvelopeIn.model_validate(inventory_envelope()))
    svc.update(TelemetryBatchIn.model_validate(batch(samples)))
    return svc


def test_health_is_explainable_with_reasons() -> None:
    svc = _twin_with(typical_samples(mem=91.0))
    t = svc.get()
    assert t is not None
    mem = t.components["memory"].health
    assert mem.score == 90 and mem.status is HealthStatus.HEALTHY
    assert any("Memory utilization elevated" in r.message and r.impact == -10 for r in mem.reasons)
    bat = t.components["battery"].health
    assert any("Battery health reduced by 4.4%" in r.message for r in bat.reasons)
    assert t.overall.score is not None and t.overall.reasons


def test_unobservable_component_excluded_not_assumed_healthy() -> None:
    svc = _twin_with(typical_samples())
    t = svc.get()
    assert t is not None
    fan = t.components["fan"].health
    assert fan.score is None and fan.status is HealthStatus.UNKNOWN


def test_hot_thermal_zone_lowers_cpu_and_thermal_health() -> None:
    svc = _twin_with(typical_samples(zone_c=93.0))
    t = svc.get()
    assert t is not None
    assert t.components["cpu"].health.score == 75
    assert t.components["thermal_sensors"].health.score == 80


def test_status_bands() -> None:
    assert status_for(None) is HealthStatus.UNKNOWN
    assert status_for(85) is HealthStatus.HEALTHY
    assert status_for(60) is HealthStatus.WARNING
    assert status_for(59) is HealthStatus.CRITICAL


def test_health_engine_sustained_cpu_uses_window() -> None:
    w = MetricWindow()
    for i in range(61):
        w.add("cpu.usage_percent", 1000.0 + i, 97.0)
    svc = _twin_with([sample("cpu.usage_percent", 97.0)])
    t = svc.get()
    assert t is not None
    res = HealthEngine(w).evaluate(t.components, 1060.0)
    assert any("Sustained CPU load" in r.message for r in res["cpu"].reasons)


# --------------------------------------------------------------- anomaly
def test_rule_requires_duration_and_uses_hysteresis() -> None:
    rule = ThresholdRule(
        "hot", "x.temp", ">=", 90.0, 10, Severity.WARNING, "Hot", "{value}", clear_threshold=85.0
    )
    eng = AnomalyEngine("d", rules=[rule], specs=[])
    assert not eng.evaluate([reading("x.temp", 95, 0)], {}).opened
    assert not eng.evaluate([reading("x.temp", 95, 5)], {}).opened
    opened = eng.evaluate([reading("x.temp", 95, 10)], {}).opened
    assert len(opened) == 1 and opened[0].severity is Severity.WARNING
    assert not eng.evaluate([reading("x.temp", 88, 11)], {}).resolved  # inside hysteresis band
    resolved = eng.evaluate([reading("x.temp", 80, 12)], {}).resolved
    assert len(resolved) == 1 and resolved[0].resolved_at is not None


def test_rule_interrupted_breach_resets_timer() -> None:
    rule = ThresholdRule(
        "hot", "x.temp", ">=", 90.0, 10, Severity.WARNING, "Hot", "{value}", clear_threshold=85.0
    )
    eng = AnomalyEngine("d", rules=[rule], specs=[])
    eng.evaluate([reading("x.temp", 95, 0)], {})
    eng.evaluate([reading("x.temp", 70, 5)], {})
    assert not eng.evaluate([reading("x.temp", 95, 11)], {}).opened


def test_string_rule_disk_health() -> None:
    rule = ThresholdRule(
        "dh", "disk.health_status", "!=", "Healthy", 0, Severity.CRITICAL, "Drive", "{value}"
    )
    eng = AnomalyEngine("d", rules=[rule], specs=[])
    assert eng.evaluate([reading("disk.health_status", "Warning", 0)], {}).opened


def test_duplicate_or_replayed_samples_ignored() -> None:
    rule = ThresholdRule("hot", "x.temp", ">=", 90.0, 0, Severity.WARNING, "Hot", "{value}")
    eng = AnomalyEngine("d", rules=[rule], specs=[])
    assert eng.evaluate([reading("x.temp", 95, 5)], {}).opened
    assert not eng.evaluate([reading("x.temp", 20, 4)], {}).resolved  # older sample ignored


def test_statistical_spike_detected_and_resolved() -> None:
    spec = StatisticalSpec("cpu.usage_percent", "Spike", min_std=2.0, min_delta=30.0)
    eng = AnomalyEngine(
        "d", rules=[], specs=[spec], detector=StatisticalDetector(alpha=0.02, warmup=50, confirm=3, resolve=3)
    )
    t = 0
    for i in range(80):
        eng.evaluate([reading("cpu.usage_percent", 10.0 + (i % 3), t)], {})
        t += 1
    opened = []
    for _ in range(5):
        opened += eng.evaluate([reading("cpu.usage_percent", 95.0, t)], {}).opened
        t += 1
    assert len(opened) == 1 and opened[0].context["method"] == "EWMA z-score"
    resolved = []
    for _ in range(10):
        resolved += eng.evaluate([reading("cpu.usage_percent", 10.0, t)], {}).resolved
        t += 1
    assert len(resolved) == 1


def test_statistical_needs_warmup_before_alarming() -> None:
    spec = StatisticalSpec("cpu.usage_percent", "Spike", min_std=2.0, min_delta=30.0)
    eng = AnomalyEngine("d", rules=[], specs=[spec], detector=StatisticalDetector(warmup=120, confirm=1))
    eng.evaluate([reading("cpu.usage_percent", 5.0, 0)], {})
    assert not eng.evaluate([reading("cpu.usage_percent", 100.0, 1)], {}).opened
