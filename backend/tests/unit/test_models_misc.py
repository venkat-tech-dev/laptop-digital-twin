import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.security import SlidingWindowRateLimiter
from app.domain.analytics.stats import confidence_from_fit, linear_fit, percentile, summarize
from app.domain.devices.models import DeviceStatus, status_from_age
from app.domain.simulation.model import PROFILES, Baseline, Scenario, power_class, simulate
from app.domain.telemetry.models import MetricReading, Quality
from app.repositories.memory import MemoryTelemetryRepository
from app.schemas.ingest import MetricSampleIn, ProcessInfoIn, TelemetryBatchIn
from app.services.persistence import SamplePersister
from tests.conftest import settings


def test_status_from_age() -> None:
    assert status_from_age(1, 3, 10, 30) is DeviceStatus.LIVE
    assert status_from_age(5, 3, 10, 30) is DeviceStatus.DEGRADED
    assert status_from_age(12, 3, 10, 30) is DeviceStatus.STALE
    assert status_from_age(None, 3, 10, 30) is DeviceStatus.OFFLINE


def test_linear_fit_and_confidence() -> None:
    pts = [(float(t), 50 + 0.1 * t) for t in range(0, 600, 5)]
    fit = linear_fit(pts)
    assert fit is not None and abs(fit.slope - 0.1) < 1e-9 and fit.r2 > 0.999
    assert fit.time_to_reach(110, 595) == pytest.approx(5.0)
    assert confidence_from_fit(fit, 30, 300)[0] in ("medium", "high")
    assert confidence_from_fit(linear_fit(pts[:2]), 30, 300)[0] == "insufficient"
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert summarize([]).count == 0


def test_power_class_from_sku() -> None:
    assert power_class("13th Gen Intel(R) Core(TM) i5-1335U").sustained_w == 15.0
    assert power_class("AMD Ryzen 9 7945HX").sustained_w == 55.0


def _baseline(**kw: object) -> Baseline:
    base = dict(
        cpu_usage=20.0,
        temperature_c=60.0,
        temperature_label="ACPI thermal zone _TZ.THM0",
        memory_used_gb=10.0,
        memory_total_gb=16.0,
        gpu_usage=5.0,
        battery_percent=80.0,
        battery_remaining_wh=35.0,
        battery_full_wh=44.0,
        measured_system_power_w=None,
        on_battery=False,
        cpu_model="i5-1335U",
        has_discrete_gpu=False,
    )
    base.update(kw)
    return Baseline(**base)  # type: ignore[arg-type]


def test_simulation_is_deterministic_and_labelled_assumptions() -> None:
    a = simulate(_baseline(), PROFILES[Scenario.CPU_INTENSIVE], Scenario.CPU_INTENSIVE, 600)
    b = simulate(_baseline(), PROFILES[Scenario.CPU_INTENSIVE], Scenario.CPU_INTENSIVE, 600)
    assert a.trajectory == b.trajectory  # no randomness
    assert a.predicted["temperature_c"] > a.current["temperature_c"]
    assert any("ACPI" in x for x in a.assumptions)
    assert a.confidence == "low"


def test_simulation_memory_paging_and_battery_runtime() -> None:
    r = simulate(
        _baseline(memory_used_gb=14.0), PROFILES[Scenario.RAM_INTENSIVE], Scenario.RAM_INTENSIVE, 600
    )
    assert r.predicted["paged_memory_gb"] > 0 and r.warnings
    batt = simulate(
        _baseline(on_battery=True, measured_system_power_w=10.0),
        PROFILES[Scenario.BATTERY_ONLY],
        Scenario.BATTERY_ONLY,
        3600,
    )
    assert batt.predicted["battery_runtime_h"] is not None
    assert batt.predicted["battery_percent_at_end"] < 80.0


def test_simulation_without_temperature_skips_thermal() -> None:
    r = simulate(_baseline(temperature_c=None), PROFILES[Scenario.GAMING], Scenario.GAMING, 60)
    assert r.predicted["temperature_c"] is None and any("thermal prediction skipped" in w for w in r.warnings)


def test_rate_limiter() -> None:
    rl = SlidingWindowRateLimiter(2, window_s=60)
    assert rl.allow("a", 0)[0] and rl.allow("a", 1)[0]
    assert not rl.allow("a", 2)[0]
    assert rl.allow("a", 61)[0]


def test_production_settings_guards() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE=none"):
        Settings(_env_file=None, APP_ENV="production", AUTH_MODE="none", AGENT_INGEST_KEY="x" * 30)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="AGENT_INGEST_KEY"):
        Settings(
            _env_file=None,
            APP_ENV="production",
            AUTH_MODE="api_key",
            API_KEYS="k" * 20,  # type: ignore[call-arg]
            AGENT_INGEST_KEY="short",
        )


def test_ingest_schema_rejects_bad_metric_names() -> None:
    with pytest.raises(ValueError):
        MetricSampleIn(
            metric="DROP TABLE",
            component="cpu",
            value=1,
            unit="x",
            timestamp=datetime.now(UTC),
            source="s",
            quality="GOOD",
            availability="available",
        )  # type: ignore[arg-type]


async def test_persister_downsamples_and_survives_db_failure() -> None:
    repo = MemoryTelemetryRepository()
    p = SamplePersister(repo, settings(PERSIST_SAMPLE_INTERVAL_S=5))
    t0 = datetime.now(UTC)

    def r(dt: float, value: float = 1.0) -> MetricReading:
        return MetricReading(
            "cpu.usage_percent",
            "cpu.usage_percent",
            "cpu",
            value,
            "percent",
            t0 + timedelta(seconds=dt),
            "s",
            Quality.GOOD,
            True,
            "measured",
        )

    assert p.enqueue("d", [r(0), r(1), r(2), r(6)]) == 2
    failing_calls = {"n": 0}
    original = repo.write_samples

    async def flaky(*args: object, **kw: object) -> int:
        failing_calls["n"] += 1
        raise ConnectionError("db down")

    repo.write_samples = flaky  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        await p.flush()
    assert p.depth == 2 and p.last_error
    repo.write_samples = original  # type: ignore[method-assign]
    assert await p.flush() == 2 and p.depth == 0


def test_agent_and_backend_contracts_in_sync() -> None:
    """Field names of the agent wire models must match the backend ingest schema."""
    src = Path(__file__).resolve().parents[3] / "agent" / "app" / "contracts.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    agent_fields: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            agent_fields[node.name] = {
                n.target.id
                for n in node.body
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            }
    assert agent_fields["MetricSample"] == set(MetricSampleIn.model_fields)
    assert agent_fields["TelemetryBatch"] == set(TelemetryBatchIn.model_fields)
    assert agent_fields["ProcessInfo"] == set(ProcessInfoIn.model_fields)


async def test_persister_keeps_rows_added_while_a_write_is_in_flight() -> None:
    """Regression (Phase 10 audit): the bounded queue overflowing during the awaited write must not make
    flush() pop newer, unwritten rows."""
    import asyncio

    repo = MemoryTelemetryRepository()
    p = SamplePersister(repo, settings(PERSIST_SAMPLE_INTERVAL_S=1, PERSIST_QUEUE_MAX=1000))
    t0 = datetime.now(UTC)

    def r(i: int) -> MetricReading:
        return MetricReading(
            f"m.k{i}",
            "cpu.usage_percent",
            "cpu",
            float(i),
            "percent",
            t0,
            "s",
            Quality.GOOD,
            True,
            "measured",
        )

    p.enqueue("d", [r(i) for i in range(1000)])  # full queue: rows 0..999
    written: list[float] = []
    gate = asyncio.Event()
    original = repo.write_samples

    async def slow(device_id: str, defs: object, rows: list) -> int:  # type: ignore[type-arg]
        await gate.wait()
        written.extend(row.value for row in rows)
        return await original(device_id, defs, rows)  # type: ignore[arg-type]

    repo.write_samples = slow  # type: ignore[method-assign]
    task = asyncio.create_task(p.flush())
    await asyncio.sleep(0)
    p.enqueue("d", [r(i) for i in range(1000, 1300)])  # overflow: rows 0..299 evicted, 1000..1299 new
    gate.set()
    await task
    remaining = sorted(row.value for _, _, row, _ in p._queue)
    assert remaining == [float(i) for i in range(1000, 1300)]  # the new rows are still queued
    await p.flush()
    assert {float(i) for i in range(1000, 1300)} <= set(written)
