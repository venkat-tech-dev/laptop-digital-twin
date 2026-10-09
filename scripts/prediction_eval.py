"""Forecast evaluation (Phase 5): walk-forward backtests through the live forecasting code.

    backend/.venv/Scripts/python scripts/prediction_eval.py --device <id> --json docs/prediction-eval-results.json

* Recorded telemetry (read-only, DATABASE_URL): memory, temperature, CPU, battery (charging periods
  are NOT_APPLICABLE; each unplug starts a new discharge regime) and disk.
* Synthetic scenarios (labelled ``synthetic``; evaluation only) for behavior the recorded history does
  not contain: steady disk growth, a disk cleanup, a large file write, battery discharge under load.

No future data: each origin sees only samples up to itself (see app/domain/prediction/backtest.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.domain.prediction.backtest import backtest  # noqa: E402
from app.domain.prediction.targets import TARGETS_BY_ID  # noqa: E402

KEYS = {
    "memory": "memory.usage_percent",
    "cpu": "cpu.usage_percent",
    "temperature": "thermal.zone_temperature_c",  # first matching series (labelled)
    "battery": "battery.charge_percent",
    "disk": "disk.usage_percent",
}


async def load_device(device_id: str, hours: float) -> dict[str, list[tuple[float, float]]]:
    from app.core.config import Settings
    from app.infrastructure.database.engine import Database
    from app.repositories.sql import SqlTelemetryRepository

    db = Database(Settings().database_url)
    repo = SqlTelemetryRepository(db)
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    out: dict[str, list[tuple[float, float]]] = {}
    try:
        metrics = sorted(m.key for m in await repo.list_metrics(device_id))
        for name, prefix in {**KEYS, "plugged": "battery.power_plugged"}.items():
            key = next((k for k in metrics if k == prefix or k.startswith(prefix + "{")), None)
            if key is None:
                continue
            rows = await repo.raw_values(device_id, key, start, end, limit=200_000)
            out[name] = [(t.timestamp(), v) for t, v in rows]
            out[name + "#key"] = [(0.0, 0.0)]
            print(f"  loaded {key}: {len(rows)} samples")
    finally:
        await db.dispose()
    return out


def battery_regimes(plugged: list[tuple[float, float]]) -> tuple[list[float], Any]:
    starts = [t for (_, a), (t, b) in zip(plugged, plugged[1:], strict=False) if a >= 0.5 and b < 0.5]
    times = [t for t, _ in plugged]

    def na(t: float) -> str | None:
        import bisect

        i = bisect.bisect_right(times, t) - 1
        return "charging" if i >= 0 and plugged[i][1] >= 0.5 else None

    return starts, na


def synthetic() -> dict[str, dict[str, Any]]:
    rng = random.Random(5)
    out: dict[str, dict[str, Any]] = {}
    t0 = 1_760_000_000.0
    day = 86400.0
    # steady disk growth 0.4 %/day from 80 % (crosses 90 % on day 25), 40 days, 10-min samples
    growth = [(t0 + i * 600, 80 + 0.4 * i * 600 / day + rng.gauss(0, 0.05)) for i in range(int(40 * day / 600))]
    out["disk_growth"] = {"target": "disk", "raw": growth}
    # growth to 88 %, then cleanup to 72 % at day 25, then growth again
    clean = []
    for i in range(int(40 * day / 600)):
        t = t0 + i * 600
        d = (t - t0) / day
        v = 80 + 0.35 * d if d < 25 else 72 + 0.35 * (d - 25)
        clean.append((t, v + rng.gauss(0, 0.05)))
    out["disk_cleanup"] = {"target": "disk", "raw": clean}
    # large file write: flat 60 % then +15 % within an hour at day 10
    big = [(t0 + i * 600, (60 if i * 600 < 10 * day else 75) + rng.gauss(0, 0.05)) for i in range(int(20 * day / 600))]
    out["disk_large_file"] = {"target": "disk", "raw": big}
    # battery: 5 discharge cycles 100 -> 5 % at varying load, charging in between
    raw, plug, t = [], [], t0
    for c in range(5):
        rate = (0.6 + 0.4 * c) / 60  # % per second-minute: 0.6-2.2 %/min
        v = 100.0
        while v > 5:
            raw.append((t, v))
            plug.append((t, 0.0))
            v -= rate * 5 + rng.gauss(0, 0.02)
            t += 5
        for _ in range(720):  # an hour on AC
            v = min(100.0, v + 0.15)
            raw.append((t, v))
            plug.append((t, 1.0))
            t += 5
    out["battery_cycles"] = {"target": "battery", "raw": raw, "plugged": plug}
    return out


def run(name: str, target_id: str, raw: list[tuple[float, float]], plugged: list[tuple[float, float]] | None,
        data: str) -> dict[str, Any]:
    target = TARGETS_BY_ID[target_id]
    kw: dict[str, Any] = {}
    if plugged:
        kw["regime_starts"], kw["not_applicable_at"] = battery_regimes(plugged)
    t = time.perf_counter()
    r = backtest(raw, target, **kw)
    r["runtime_s"] = round(time.perf_counter() - t, 1)
    r["data"] = data
    m = r.get("models", {})
    cr = r.get("crossings", {})
    print(f"  {name:<18} origins={r.get('origins')} MAE " + " ".join(
        f"{k}={v['mae']}" for k, v in m.items() if v.get("mae") is not None)
        + f" | crossings actual={cr.get('actual_crossings')} forecasts={cr.get('forecasts_issued')} "
          f"false={cr.get('false_prediction_rate')} missed={cr.get('missed_crossing_rate')} "
          f"|timing|={cr.get('median_abs_timing_error_s')}s lead={cr.get('mean_lead_time_s')}s "
          f"censored={cr.get('censored_forecasts')} lifecycle={r.get('lifecycle')} exceedance={r.get('exceedance')}")
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device")
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--json")
    args = ap.parse_args()
    report: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(), "recorded": {}, "synthetic": {}}
    if args.device:
        print("Recorded telemetry (read-only):")
        series = asyncio.run(load_device(args.device, args.hours))
        for tid in ("memory", "temperature", "cpu", "battery", "disk"):
            if tid in series:
                report["recorded"][tid] = run(tid, tid, series[tid], series.get("plugged") if tid == "battery" else None,
                                              "recorded telemetry")
    print("Synthetic scenarios (evaluation only):")
    for name, sc in synthetic().items():
        report["synthetic"][name] = run(name, sc["target"], sc["raw"], sc.get("plugged"), "synthetic")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        print(f"report written to {args.json}")


if __name__ == "__main__":
    main()
