"""Anomaly-detection evaluation (Phase 4): replay telemetry through the live detection code.

    python scripts/anomaly_eval.py                      # synthetic scenarios A-G + a normal day
    python scripts/anomaly_eval.py --device <id>        # also replay this device's recorded history
    python scripts/anomaly_eval.py --json out.json      # machine-readable report

Synthetic data is labelled ``synthetic`` and only exists inside this evaluation. Device replay is
read-only: it reads recorded telemetry (DATABASE_URL), trains baselines on the history *before* the
replay window (no future leakage) and reports what would have been raised in the window. There is
no ground truth for real data, so it reports alert volume, levels and confidence - not precision.

Run from the repository root with the backend virtualenv:
    backend/.venv/Scripts/python scripts/anomaly_eval.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.domain.anomalies.policy import AnomalyPolicy  # noqa: E402
from app.domain.anomalies.replay import (  # noqa: E402
    DAY,
    _dt,
    baseline_stability,
    evaluate,
    generate,
    model_drift,
    replay,
    run_scenario,
    scenarios,
    train_baselines,
    train_replay_model,
)

#: signal -> metric names to look for in a device's recorded series (first match wins)
SIGNAL_METRICS = {
    "cpu": ("cpu.usage_percent",),
    "memory": ("memory.usage_percent",),
    "temperature": ("cpu.temperature_c", "thermal.zone_temperature_c"),
    "disk_active": ("disk.active_time_percent",),
    "net_latency": ("network.gateway_latency_ms",),
    "gpu": ("gpu.usage_percent",),
}


def synthetic(policy: AnomalyPolicy) -> dict[str, Any]:
    out: dict[str, Any] = {"scenarios": {}}
    for sc in scenarios():
        t = time.perf_counter()
        r = run_scenario(sc, policy)
        r["runtime_s"] = round(time.perf_counter() - t, 1)
        out["scenarios"][sc.name] = r
        m = r["metrics"]
        print(f"  {sc.name:<24} expected: {sc.expected:<40} reported={m['anomalies_reported']} "
              f"recall={m['recall']} fp={m['false_positives']} latency={m['detection_latency_s']}")
    # false-positive rate on normal behavior: one full synthetic day, no incidents
    t0 = 1_760_000_000.0 - (1_760_000_000.0 % DAY)
    hist = generate(7, t0 - 8 * DAY, 8 * DAY)
    baselines = train_baselines(hist, policy, _dt(t0))
    model = train_replay_model("normal-day", hist, baselines, policy)
    normal = generate(21, t0, DAY)
    res = replay(normal, baselines, policy, t0, t0 + DAY, model=model)
    ev = evaluate(res, [], DAY)
    out["normal_day"] = {"data": "synthetic", **ev}
    out["baseline_stability"] = baseline_stability(hist, policy, _dt(t0))
    out["model_drift"] = model_drift(model, normal) if model else None
    print(f"  normal day (24 h): alerts={ev['anomalies_reported']} fp/h={ev['false_positives_per_hour']} "
          f"drift={out['model_drift']}")
    return out


async def device_replay(device_id: str, hours: float, policy: AnomalyPolicy) -> dict[str, Any]:
    from app.core.config import Settings
    from app.infrastructure.database.engine import Database
    from app.repositories.sql import SqlTelemetryRepository

    settings = Settings()
    if not settings.database_url:
        return {"skipped": "DATABASE_URL not configured"}
    db = Database(settings.database_url)
    repo = SqlTelemetryRepository(db)
    try:
        metrics = {m.key for m in await repo.list_metrics(device_id)}
        keys: dict[str, str] = {}
        for sid, names in SIGNAL_METRICS.items():
            for name in names:
                match = sorted(k for k in metrics if k == name or k.startswith(name + "{"))
                if match:
                    keys[sid] = match[0]
                    break
        end = datetime.now(UTC)
        split = end - timedelta(hours=hours)
        start = split - timedelta(days=policy.baseline_history_days)
        hist = await repo.history(device_id, sorted(set(keys.values())), start, split, 60)
        history = {sid: [(p.time.timestamp(), p.avg) for p in hist.get(k, [])] for sid, k in keys.items()}
        live: dict[str, list[tuple[float, float]]] = {}
        for sid, k in keys.items():
            raw = await repo.raw_values(device_id, k, split, end, limit=50_000)
            live[sid] = [(t.timestamp(), v) for t, v in raw]
    finally:
        await db.dispose()
    live = {sid: pts for sid, pts in live.items() if pts}
    baselines = train_baselines({s: p for s, p in history.items() if s in live}, policy, split, "replay")
    model = train_replay_model(device_id, history, baselines, policy)
    res = replay(live, baselines, policy, split.timestamp(), end.timestamp(), model=model,
                 interval_s=5.0, device_id=device_id)
    ev = evaluate(res, [], end.timestamp() - split.timestamp())
    report = {
        "data": "recorded telemetry (read-only)",
        "device_id": device_id,
        "window": {"start": split.isoformat(), "end": end.isoformat()},
        "series": keys,
        "samples": {s: len(p) for s, p in live.items()},
        "baseline_status": {s: b.status.value for s, b in baselines.items()},
        "baseline_minutes": {s: b.sample_count for s, b in baselines.items()},
        "model": model.model_id if model else None,
        "alert_volume_per_day": ev["alert_volume_per_day"],
        "anomalies": [
            {"title": a.title, "type": a.anomaly_type.value, "level": a.effective_level.value,
             "confidence": a.confidence, "started_at": a.started_at.isoformat(), "lifecycle": a.lifecycle.value}
            for a in res.reported()
        ],
        "suppressed": sum(1 for a in res.anomalies.values() if a.lifecycle.value == "SUPPRESSED"),
    }
    print(f"  device {device_id}: {len(report['anomalies'])} anomalies in {hours} h "
          f"(baselines {report['baseline_status']}, model {report['model']})")
    for a in report["anomalies"]:
        print(f"    {a['started_at']} {a['level']:<8} {a['confidence']}  {a['title']}")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", help="replay this device's recorded telemetry (read-only)")
    ap.add_argument("--hours", type=float, default=6.0, help="replay window for --device (default 6)")
    ap.add_argument("--skip-synthetic", action="store_true")
    ap.add_argument("--json", help="write the full report to this file")
    args = ap.parse_args()
    policy = AnomalyPolicy()
    report: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(), "policy": policy.public()}
    if not args.skip_synthetic:
        print("Synthetic scenarios (evaluation data, never shown in the product):")
        report["synthetic"] = synthetic(policy)
    if args.device:
        print("Recorded telemetry replay:")
        report["device"] = asyncio.run(device_replay(args.device, args.hours, policy))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        print(f"report written to {args.json}")


if __name__ == "__main__":
    main()
