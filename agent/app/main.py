"""Laptop Digital Twin endpoint agent - command line entry point.

Usage::

    python -m app.main              # run in the foreground (console mode)
    python -m app.main --discover   # print the hardware inventory and exit
    python -m app.main --once       # collect every collector twice, print a table and exit
    python -m app.service install   # install as a Windows service (administrator) - see app/service
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal

import structlog

from app.config.settings import AgentSettings, RunMode
from app.contracts import DeviceEvent, MetricSample, ProcessSnapshot
from app.observability.logging import configure_logging
from app.runner import AGENT_VERSION, AgentRunner
from app.runtime import AgentRuntime
from app.scheduler import CollectionScheduler

log = structlog.get_logger("agent.main")


async def run_once(settings: AgentSettings) -> None:
    runtime = AgentRuntime(settings)
    await asyncio.to_thread(runtime.discover)
    state: dict[str, str] = {}
    providers = runtime.build_providers((state.get, state.__setitem__), lambda: None)
    collected: dict[str, MetricSample] = {}
    snapshot: list[ProcessSnapshot] = []
    events: list[DeviceEvent] = []

    def sink(samples: list[MetricSample], procs: ProcessSnapshot | None) -> None:
        for s in samples:
            key = s.metric + (json.dumps(s.labels, sort_keys=True) if s.labels else "")
            collected[key] = s
        if procs:
            snapshot.append(procs)

    scheduler = CollectionScheduler(
        providers, runtime.worker, sink, settings.cpu_budget_percent, event_sink=events.extend
    )
    for _ in range(2):  # rates need two samples
        await asyncio.gather(*(scheduler.collect_once(p) for p in providers))
        await asyncio.sleep(1.0)
    await asyncio.to_thread(runtime.close)
    print(f"device_id={runtime.device_id} agent={AGENT_VERSION}")
    for s in sorted(collected.values(), key=lambda x: x.metric):
        labels = ",".join(f"{k}={v}" for k, v in s.labels.items())
        value = s.value if s.value is not None else f"Unavailable ({s.reason})"
        print(f"{s.quality.value:<11} {s.metric:<38} {labels[:40]:<40} {value} {s.unit}  [{s.source}]")
    if snapshot:
        top = snapshot[-1].processes[:5]
        print("top processes:", [(p.name, p.cpu_percent, p.memory_rss_bytes) for p in top])
    print("events:", [(e.type, e.message[:80]) for e in events[:10]], f"(total {len(events)})")
    print(
        "collector health:",
        {n: (h.consecutive_failures, h.last_error) for n, h in scheduler.health.items() if h.last_error},
    )


async def run_console(settings: AgentSettings) -> None:
    runner = AgentRunner(settings, RunMode.CONSOLE)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows: Ctrl+C arrives as KeyboardInterrupt
            loop.add_signal_handler(sig, stop.set)
    await runner.run(stop)


def run() -> None:
    parser = argparse.ArgumentParser(description="Laptop Digital Twin endpoint agent")
    parser.add_argument("--discover", action="store_true", help="print hardware inventory and exit")
    parser.add_argument("--once", action="store_true", help="collect once, print all metrics and exit")
    args = parser.parse_args()
    settings = AgentSettings()
    log_file = (
        None
        if (args.once or args.discover)
        else settings.resolved_data_dir(RunMode.CONSOLE) / "logs" / "agent.log"
    )
    configure_logging(settings.log_level, log_file, max_mb=settings.log_max_mb, backups=settings.log_backups)
    if args.discover:
        runtime = AgentRuntime(settings)
        inventory = runtime.discover()
        print(json.dumps({"device_id": runtime.device_id, "inventory": inventory}, indent=2, default=str))
        runtime.worker.shutdown()
        return
    if not args.once and not settings.agent_ingest_key and not settings.enrollment_token:
        log.error(
            "missing_agent_key",
            hint="Set AGENT_ENROLLMENT_TOKEN (from the organization console) or AGENT_INGEST_KEY",
        )
        raise SystemExit(2)
    try:
        asyncio.run(run_once(settings) if args.once else run_console(settings))
    except KeyboardInterrupt:
        log.info("agent_interrupted")


if __name__ == "__main__":
    run()
