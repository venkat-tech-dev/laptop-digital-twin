"""Alerting load benchmark (Phase 6): event ingest, alert storms and notification delivery.

Drives the real AlertService (adapters -> engine -> routing -> notifications -> worker -> in-app
provider). Synthetic events, isolated storage:

    backend/.venv/Scripts/python scripts/alert_bench.py memory 1 10 100 1000 10000
    BENCH_DATABASE_URL=postgresql+asyncpg://.../ldt_load backend/.venv/Scripts/python scripts/alert_bench.py sql 1 10 100 1000

Never point BENCH_DATABASE_URL at the production database.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import Settings  # noqa: E402
from app.domain.events.events import AnomalyChanged  # noqa: E402
from app.repositories.alerting import MemoryAlertRepository, SqlAlertRepository  # noqa: E402
from app.services.alerting import AlertService  # noqa: E402
from app.services.notify_providers import InAppProvider  # noqa: E402


class _Store:
    async def get_setting(self, _k: str) -> None:
        return None

    async def list_users(self) -> list[Any]:
        return []


class _WS:
    def sessions_of(self, _s: str) -> int:
        return 0


def event(device: str, i: int, kind: str = "detected", level: str = "HIGH") -> AnomalyChanged:
    return AnomalyChanged(device_id=device, kind=kind, anomaly={
        "anomaly_id": f"{device}-{i}", "anomaly_type": "behavioral_anomaly", "level": level, "confidence": 0.9,
        "title": "Unusually high cpu usage", "message": "bench", "rule_id": f"behavior.r{i % 5}",
        "metric_key": "performance.cpu.usage_percent", "value": 88.0, "lifecycle": "ONGOING",
        "started_at": datetime.now(UTC).isoformat()})


async def run(mode: str, n_devices: int) -> dict[str, Any]:
    settings = Settings(_env_file=None, DATABASE_URL="", REDIS_URL="")  # type: ignore[call-arg]
    db = None
    if mode == "sql":
        from app.infrastructure.database.engine import Database

        db = Database(os.environ["BENCH_DATABASE_URL"])
        repo: Any = SqlAlertRepository(db)
        async with db.sessions.begin() as s:  # devices must exist (foreign key)
            from sqlalchemy import text

            await s.execute(text("DELETE FROM alerts"))
            await s.execute(text("DELETE FROM devices WHERE id LIKE 'bench-%'"))
            for d in range(n_devices):
                await s.execute(text("INSERT INTO devices(id, agent_version, inventory, first_seen, last_inventory_at) "
                                     "VALUES (:i, 'b', '{}', now(), now())"), {"i": f"bench-{d:05d}"})
    else:
        repo = MemoryAlertRepository(max_items=10**7)

    async def publish(_events: list[Any]) -> None:
        return None

    svc = AlertService(settings, repo, _Store(), _Store(), None, _WS(), publish, lambda _r: None,
                       {"in_app": InAppProvider(lambda _n: None)}, "bench-user")
    devices = [f"bench-{d:05d}" for d in range(n_devices)]
    # 1) new conditions: 5 per device
    t = time.perf_counter()
    for d in devices:
        for i in range(5):
            await svc.on_event(event(d, i))
    created_s = time.perf_counter() - t
    created = 5 * n_devices
    # 2) storm: every condition repeats 4 more times (should only update)
    t = time.perf_counter()
    for _ in range(4):
        for d in devices:
            for i in range(5):
                await svc.on_event(event(d, i, "updated"))
    storm_s = time.perf_counter() - t
    storm = 20 * n_devices
    # 3) delivery: drain the notification queue through the worker
    t = time.perf_counter()
    delivered = 0
    while True:
        k = await svc.deliver_due(datetime.now(UTC) + timedelta(hours=1), limit=200)
        if not k:
            break
        delivered += k
    deliver_s = time.perf_counter() - t
    open_alerts = len(svc.engine.open_alerts())
    if db is not None:
        await db.dispose()
    return {
        "mode": mode, "devices": n_devices,
        "new_events": created, "new_events_per_s": round(created / created_s),
        "storm_events": storm, "storm_events_per_s": round(storm / storm_s),
        "open_alerts": open_alerts, "duplicates_created": open_alerts - created,
        "notifications_delivered": delivered, "deliveries_per_s": round(delivered / deliver_s) if deliver_s else None,
        "engine": svc.engine.stats,
    }


def main() -> None:
    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    mode = sys.argv[1]
    for n in [int(a) for a in sys.argv[2:]]:
        print(json.dumps(asyncio.run(run(mode, n))), flush=True)


if __name__ == "__main__":
    main()
