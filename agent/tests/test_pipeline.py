from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.contracts import Availability, MetricSample, Quality
from app.platform.lhm import parse_data_json
from app.platform.powercfg import parse_battery_report
from app.platform.worker import WorkerPool
from app.providers.base import MetricSpec, Reading, TelemetryProvider
from app.publisher.pipeline import BatchAccumulator
from app.scheduler import CollectionScheduler


def sample(metric: str = "cpu.usage_percent", value: float = 1.0) -> MetricSample:
    return MetricSample(
        metric=metric,
        component="cpu",
        value=value,
        unit="percent",
        timestamp=datetime.now(UTC),
        source="t",
        quality=Quality.GOOD,
        availability=Availability.AVAILABLE,
    )


def test_accumulator_keeps_latest_per_series() -> None:
    acc = BatchAccumulator()
    acc.add([sample(value=1), sample(value=2), sample("memory.usage_percent", 3)])
    samples, procs = acc.drain()
    assert {s.metric: s.value for s in samples} == {"cpu.usage_percent": 2, "memory.usage_percent": 3}
    assert procs is None and len(acc) == 0 and not acc.pending


class Boom(TelemetryProvider):
    name = "boom"
    component = "gpu"

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("gpu.usage_percent", "percent", "x")]

    def collect(self) -> list[Reading]:
        raise RuntimeError("driver crashed")


class Ok(TelemetryProvider):
    name = "ok"
    component = "cpu"

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return []

    def collect(self) -> list[Reading]:
        return [Reading("cpu.usage_percent", "cpu", 5.0, "percent", "x")]


async def test_one_provider_failing_does_not_stop_others() -> None:
    got: list[MetricSample] = []
    worker = WorkerPool(init=lambda: None)
    sched = CollectionScheduler(
        [Boom(1000), Ok(1000)], worker, lambda s, _: got.extend(s), cpu_budget_percent=50
    )
    stop = asyncio.Event()
    task = asyncio.create_task(sched.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    await task
    worker.shutdown()
    by = {s.metric: s for s in got}
    assert by["gpu.usage_percent"].quality is Quality.ERROR
    assert "driver crashed" in (by["gpu.usage_percent"].reason or "")
    assert by["cpu.usage_percent"].value == 5.0
    # schema 1.2: each sample carries its collector's effective interval (drives twin freshness)
    assert by["cpu.usage_percent"].interval_ms == 1000 and by["gpu.usage_percent"].interval_ms == 1000


def test_lhm_data_json_parsing() -> None:
    tree = {
        "Text": "Sensor",
        "Children": [
            {
                "Text": "Intel Core i5",
                "HardwareId": "/intelcpu/0",
                "ImageURL": "cpu.png",
                "Children": [
                    {
                        "Text": "Temperatures",
                        "Children": [
                            {
                                "Text": "CPU Package",
                                "SensorId": "/intelcpu/0/temperature/0",
                                "Type": "Temperature",
                                "Value": "61,5 °C",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    sensors = parse_data_json(tree)
    assert sensors[0].value == 61.5
    assert sensors[0].hardware_kind == "intelcpu"
    assert sensors[0].hardware == "Intel Core i5"


def test_battery_report_parsing() -> None:
    xml = """<?xml version="1.0"?><BatteryReport xmlns="http://schemas.microsoft.com/battery/2012"><Batteries><Battery>
      <Id>5B11M90000</Id><Manufacturer>Sunwoda</Manufacturer><Chemistry>LiP</Chemistry>
      <DesignCapacity>46500</DesignCapacity><FullChargeCapacity>44450</FullChargeCapacity><CycleCount>168</CycleCount>
      </Battery></Batteries></BatteryReport>"""
    (entry,) = parse_battery_report(xml)
    assert entry.design_capacity_mwh == 46500 and entry.cycle_count == 168
