"""Anomaly-engine benchmark (Phase 4): cost of one evaluation round and of training, by device count.

Runs the real IntelligenceService evaluation path in-process (twin windows -> key resolution ->
data-quality gates -> BehaviorEngine incl. Extended Isolation Forest scoring) against N synthetic
twins with trained baselines and a model each. Synthetic data, in memory only - nothing is
persisted or published.

    backend/.venv/Scripts/python scripts/anomaly_bench.py 1 10 100 500 1000
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import Settings  # noqa: E402
from app.domain.anomalies.replay import DAY, generate, train_baselines, train_replay_model  # noqa: E402
from app.repositories.intelligence import MemoryIntelligenceRepository, StoredBaseline  # noqa: E402
from app.repositories.memory import MemoryEventRepository, MemoryTelemetryRepository  # noqa: E402
from app.schemas.ingest import InventoryEnvelopeIn, TelemetryBatchIn  # noqa: E402
from app.services.digital_twin import DigitalTwinService  # noqa: E402
from app.services.intelligence import DeviceIntel, IntelligenceService  # noqa: E402
from app.services.presence import PresenceService  # noqa: E402

INVENTORY = {"manufacturer": "LENOVO", "model": "ThinkPad L14 Gen 4", "cpu": {"model": "i5"},
             "memory": {"total_bytes": 16 * 2**30}, "gpu": [{"name": "Intel UHD"}], "storage": [{"disk": "PhysicalDrive0"}]}
METRICS = [("cpu.usage_percent", "cpu", "percent", {}), ("memory.usage_percent", "memory", "percent", {}),
           ("thermal.zone_temperature_c", "thermal", "celsius", {"zone": "TZ0"}),
           ("disk.active_time_percent", "disk", "percent", {"disk": "PhysicalDrive0"}),
           ("network.gateway_latency_ms", "network", "ms", {}), ("gpu.usage_percent", "gpu", "percent", {})]


class Noop:
    async def get_setting(self, _k: str) -> None:
        return None


def sample(metric: str, comp: str, unit: str, labels: dict[str, str], v: float, ts: datetime) -> dict[str, Any]:
    return {"metric": metric, "component": comp, "value": v, "unit": unit, "timestamp": ts.isoformat(),
            "source": "bench", "quality": "GOOD", "availability": "available", "kind": "measured",
            "labels": labels, "interval_ms": 5000}


def rss_mb() -> float | None:
    try:
        import psutil  # type: ignore[import-untyped]

        return round(psutil.Process(os.getpid()).memory_info().rss / 2**20, 1)
    except Exception:
        return None


async def run(n: int, baselines: Any, model: Any) -> dict[str, Any]:
    settings = Settings(_env_file=None, DATABASE_URL="", REDIS_URL="")  # type: ignore[call-arg]
    twins = DigitalTwinService(settings)
    presence = PresenceService(30, 90)
    now = datetime.now(UTC)
    rng = random.Random(n)  # noqa: S311
    for i in range(n):
        did = f"bench-{i:05d}"
        twins.apply_inventory(InventoryEnvelopeIn.model_validate(
            {"device_id": did, "agent_version": "b", "discovered_at": now.isoformat(), "inventory": INVENTORY}))
        for k in range(24):  # a full 2-minute window at 5 s
            ts = now - timedelta(seconds=115 - 5 * k)
            vals = [20 + rng.gauss(0, 3), 55 + rng.gauss(0, 1), 49 + rng.gauss(0, 1), 5.0, 6.0, 5.0]
            samples = [sample(m, c, u, lab, v, ts) for (m, c, u, lab), v in zip(METRICS, vals, strict=True)]
            twins.update(TelemetryBatchIn.model_validate({"device_id": did, "agent_version": "b", "sequence": k + 1,
                                                          "sent_at": ts.isoformat(), "samples": samples}), ts)
        presence.batch_received(did, now)

    async def publish(_events: list[Any]) -> None:
        return None

    svc = IntelligenceService(settings, twins, presence, MemoryTelemetryRepository(), MemoryEventRepository(),
                              MemoryIntelligenceRepository(), Noop(), publish, lambda _a: None)
    for d in twins.devices():
        svc._devices[d.device_id] = DeviceIntel(  # noqa: SLF001 - benchmark seam (no DB training)
            baselines={sid: StoredBaseline(b, None) for sid, b in baselines.items()}, model=model, loaded=True,
            trained_at=time.monotonic())
    await svc.evaluate_all(now)  # warm-up (key resolution cache)
    rounds = []
    cpu0 = time.process_time()
    for r in range(3):
        t = time.perf_counter()
        await svc.evaluate_all(now + timedelta(seconds=r + 1))  # fresh data: the full path runs
        rounds.append(time.perf_counter() - t)
    cpu = time.process_time() - cpu0
    best = min(rounds)
    quality = {q for d in svc._devices.values() for q in d.quality.values()}  # noqa: SLF001
    assert quality == {"ok"} or quality <= {"ok", "no_data"}, quality
    return {
        "devices": n,
        "round_s": round(best, 3),
        "per_device_ms": round(best / n * 1000, 3),
        "cpu_share_of_10s_interval": round(cpu / 3 / 10.0, 3),
        "max_devices_per_core_at_10s": int(10.0 / (best / n)) if best else None,
        "rss_mb": rss_mb(),
        "quality": sorted(quality),
        "model_scored": svc.engine.stats["evaluations"],
    }


def main() -> None:
    counts = [int(a) for a in sys.argv[1:]] or [1, 10, 100, 500, 1000]
    t0 = 1_760_000_000.0 - (1_760_000_000.0 % DAY)
    hist = generate(7, t0 - 8 * DAY, 8 * DAY)
    from app.domain.anomalies.policy import AnomalyPolicy
    from app.domain.anomalies.replay import _dt

    policy = AnomalyPolicy()
    tt = time.perf_counter()
    baselines = train_baselines(hist, policy, _dt(t0))
    t_base = time.perf_counter() - tt
    tt = time.perf_counter()
    model = train_replay_model("bench", hist, baselines, policy)
    t_model = time.perf_counter() - tt
    print(json.dumps({"training_per_device": {"baseline_s": round(t_base, 2), "model_s": round(t_model, 2),
                                              "history_minutes": 8 * 1440, "signals": len(hist)}}))
    for n in counts:
        print(json.dumps(asyncio.run(run(n, baselines, model))), flush=True)


if __name__ == "__main__":
    main()
