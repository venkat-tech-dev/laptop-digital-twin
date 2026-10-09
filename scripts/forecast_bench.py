"""Forecasting benchmark (Phase 5): cost of the forecasting path by device count (in-process).

Builds N synthetic devices with full-size bounded series for every target (disk 14 days of 15-min
buckets, memory 60 x 1 min, battery 30 x 1 min, temperature 30 x 30 s, CPU 30 x 1 min) and runs the
same per-target pipeline the service runs (prepare -> assess -> lifecycle). Reports the cost of one
evaluation of every target and the steady-state CPU share given each target's cadence. Synthetic data,
in memory only.

    backend/.venv/Scripts/python scripts/forecast_bench.py 1 10 100 500 1000
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.domain.prediction.engine import PredictionPolicy, PredictionTracker, assess  # noqa: E402
from app.domain.prediction.preprocess import BucketSeries, prepare  # noqa: E402
from app.domain.prediction.targets import TARGETS  # noqa: E402

NOW = 1_760_000_000.0


def rss_mb() -> float | None:
    try:
        import psutil  # type: ignore[import-untyped]

        return round(psutil.Process(os.getpid()).memory_info().rss / 2**20, 1)
    except Exception:
        return None


def build(rng: random.Random) -> dict[str, BucketSeries]:
    out = {}
    for t in TARGETS:
        s = BucketSeries(t.bucket_s, t.window_s // t.bucket_s + 4)
        n = t.window_s // t.bucket_s
        base = {"disk": 70.0, "memory": 70.0, "battery": 80.0, "temperature": 55.0, "cpu": 25.0}[t.target_id]
        slope = {"disk": 0.002, "memory": 0.3, "battery": -0.8, "temperature": 0.1, "cpu": 0.2}[t.target_id]
        s.load_history([(NOW - (n - i) * t.bucket_s, base + slope * i + rng.gauss(0, 0.5)) for i in range(n)])
        out[t.target_id] = s
    return out


def main() -> None:
    counts = [int(a) for a in sys.argv[1:]] or [1, 10, 100, 500, 1000]
    policy = PredictionPolicy()
    results = []
    for n in counts:
        rng = random.Random(n)
        devices = [build(rng) for _ in range(n)]
        tracker = PredictionTracker(policy)
        per_target = {t.target_id: 0.0 for t in TARGETS}
        now = datetime.fromtimestamp(NOW, UTC)
        cpu0 = time.process_time()
        t0 = time.perf_counter()
        for d, series in enumerate(devices):
            for t in TARGETS:
                s0 = time.perf_counter()
                a = assess(t, prepare(series[t.target_id], t, NOW), NOW, policy)
                tracker.step(f"bench-{d}", a, now)
                per_target[t.target_id] += time.perf_counter() - s0
        wall = time.perf_counter() - t0
        cpu = time.process_time() - cpu0
        # steady state: each target runs once per its update interval
        per_second = sum(per_target[t.target_id] / t.update_interval_s for t in TARGETS)
        results.append({
            "devices": n,
            "full_round_s": round(wall, 3),
            "per_device_ms": round(wall / n * 1000, 2),
            "per_target_ms": {k: round(v / n * 1000, 2) for k, v in per_target.items()},
            "steady_state_cpu_share_of_one_core": round(per_second, 4),
            "cpu_s": round(cpu, 2),
            "rss_mb": rss_mb(),
        })
        print(json.dumps(results[-1]), flush=True)


if __name__ == "__main__":
    main()
