"""Phase 8 load test: remediation engine at fleet scale (in-process, memory repository, real signing).

    python scripts/remediation_bench.py --devices 100 1000 10000 [--sql]

Per fleet size: a proposal storm (one per device + 50 % duplicates), an approval burst, then worker ticks
with simulated agents that pull, accept and complete; reports throughput, tick latency, the maximum number
of actions in flight (must never exceed the fleet limit) and memory. ``--sql`` also times proposal +
approval persistence against the isolated ``ldt_load`` database (never the real one).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.domain.remediation.envelope import Signer  # noqa: E402
from app.domain.remediation.models import Status  # noqa: E402
from app.repositories.remediation import MemoryRemediationRepository  # noqa: E402
from app.services.remediation import RemediationService  # noqa: E402


class Window:
    def keys(self) -> list[str]:
        return ["cpu.usage_percent"]

    def values_since(self, key: str, since: float) -> list[tuple[float, float]]:
        return [(time.time(), 40.0)]


class Twins:
    def __init__(self, n: int) -> None:
        self.t = {f"dev-{i:05d}": SimpleNamespace(
            device=SimpleNamespace(agent_version="1.6.0", os_name="Microsoft Windows 11 Pro"),
            agent_health={}, processes=None, window=Window(), remediation={}) for i in range(n)}

    def get(self, d: str) -> Any:
        return self.t.get(d)


class Presence:
    def __init__(self, devices: list[str]) -> None:
        now = datetime.now(UTC)
        hb = {"agent_version": "1.6.0", "run_mode": "console",
              "remediation_actions": ["REFRESH_TELEMETRY", "REQUEST_SYSTEM_RESCAN", "RECONNECT_AGENT"]}
        self.p = {d: SimpleNamespace(heartbeat=hb, last_batch_at=now, last_contact_at=now, last_heartbeat_at=now)
                  for d in devices}

    def presence_of(self, d: str) -> str:
        return "ONLINE"

    def get(self, d: str) -> Any:
        return self.p.get(d)


class Store:
    async def get_setting(self, k: str) -> None:
        return None

    async def set_setting(self, k: str, v: Any, by: str | None) -> None:
        return None


async def noop(*a: Any) -> None:
    return None


async def bench(n: int) -> dict[str, Any]:
    twins = Twins(n)
    presence = Presence(list(twins.t))
    events: list[Any] = []

    async def publish(evs: list[Any]) -> None:
        events.extend(evs)

    settings = SimpleNamespace(remediation_kill_switch=False, twin_show_process_names=True)
    svc = RemediationService(settings, twins, presence, MemoryRemediationRepository(), Store(), publish,
                             lambda r: None, Signer.from_b64(Signer.generate_b64()))
    tracemalloc.start() if os.environ.get("BENCH_MEMORY") else None
    devices = list(twins.t)
    t0 = time.perf_counter()
    for d in devices:
        await svc.propose(d, "REFRESH_TELEMETRY", {}, "system")
    for d in devices[: n // 2]:  # duplicate event storm: must not create new proposals
        await svc.propose(d, "REFRESH_TELEMETRY", {}, "system")
    propose_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    for r in list(svc.items.values()):
        await svc.approve(r, "admin", "admin")
    approve_s = time.perf_counter() - t0
    tick_ms, max_in_flight, rounds = [], 0, 0
    t_drain = time.perf_counter()
    while any(r.status != Status.SUCCEEDED for r in svc.items.values()) and rounds < 5000:
        rounds += 1
        a = time.perf_counter()
        await svc.tick(datetime.now(UTC))
        tick_ms.append((time.perf_counter() - a) * 1000)
        in_flight = [r for r in svc.items.values() if r.status in (Status.VALIDATING, Status.EXECUTING, Status.VERIFYING)]
        max_in_flight = max(max_in_flight, len(in_flight))
        for r in in_flight:  # simulated agents: pull, accept, complete; then fresh telemetry arrives
            if r.status == Status.VALIDATING:
                svc.agent_pull(r.device_id, datetime.now(UTC))
                await svc.agent_report(r.device_id, r.execution_id, "completed", {"detail": "ok"})
                presence.p[r.device_id].last_batch_at = datetime.now(UTC) + timedelta(seconds=1)
    drain_s = time.perf_counter() - t_drain
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    done = sum(1 for r in svc.items.values() if r.status == Status.SUCCEEDED)
    return {
        "devices": n, "proposals": len(svc.items), "duplicates_suppressed": svc.stats["deduplicated"],
        "propose_per_s": round(1.5 * n / propose_s), "approve_per_s": round(n / approve_s),
        "succeeded": done, "ticks": rounds, "drain_s": round(drain_s, 1),
        "tick_ms_p50": round(statistics.median(tick_ms), 1), "tick_ms_max": round(max(tick_ms), 1),
        "max_in_flight": max_in_flight, "fleet_limit": svc.policy.fleet_max_concurrent,
        "events_published": len(events), "peak_python_mb": round(peak / 2**20, 1),
    }


async def bench_sql(n: int) -> dict[str, Any]:
    from app.infrastructure.database.engine import Database
    from app.repositories.remediation import SqlRemediationRepository

    env = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    db = Database(f"postgresql+asyncpg://ldt:{env['POSTGRES_PASSWORD'].strip()}@127.0.0.1:15432/ldt_load")
    try:
        twins = Twins(n)
        svc = RemediationService(SimpleNamespace(remediation_kill_switch=False, twin_show_process_names=True), twins,
                                 Presence(list(twins.t)), SqlRemediationRepository(db), Store(), noop, lambda r: None,
                                 Signer.from_b64(Signer.generate_b64()))
        t0 = time.perf_counter()
        for d in twins.t:
            await svc.propose(d, "REQUEST_SYSTEM_RESCAN", {}, "system")
        p = time.perf_counter() - t0
        t0 = time.perf_counter()
        for r in list(svc.items.values()):
            await svc.approve(r, "admin", "admin")
        a = time.perf_counter() - t0
        chain = await svc.repo.verify_audit()
        return {"sql_devices": n, "sql_propose_per_s": round(n / p), "sql_approve_per_s": round(n / a),
                "audit_rows": chain["rows"], "audit_ok": chain["ok"]}
    finally:
        await db.dispose()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", type=int, nargs="+", default=[100, 1000, 10000])
    ap.add_argument("--sql", action="store_true")
    args = ap.parse_args()
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    import structlog
    import logging

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    out = [asyncio.run(bench(n)) for n in args.devices]
    if args.sql:
        out.append(asyncio.run(bench_sql(500)))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
