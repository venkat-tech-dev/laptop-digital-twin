"""Telemetry pipeline load test: N synthetic agents against a backend, measured end to end.

Each synthetic device registers (enrollment key -> device token), announces an inventory, then sends
one gzip bulk upload every ``--interval`` seconds (the agent default is 5 s) plus a heartbeat every
30 s - the same wire protocol as the real agent. A WebSocket observer subscribes to ``fleet`` and to
the first device and measures delivery and end-to-end latency. The backend process is sampled for
CPU / RSS, and ``/api/v1/pipeline/stats`` + ``/metrics`` provide server-side figures.

SAFETY: run it against a separate backend and database (synthetic devices would otherwise appear in
your real fleet). Example (see docs/telemetry-pipeline.md, "Load test"):

    python scripts/loadtest.py --base http://127.0.0.1:8020 --key <enrollment key> \
        --levels 1,10,50,100,500 --duration 60 --backend-pid <uvicorn pid> --out loadtest.json

Payload: ``--template`` takes a real captured batch (JSON) so request sizes are realistic;
without it a generic batch of the same shape (169 samples, 32 processes) is generated.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import random
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psutil
import websockets

TS, DEV, BID, SEQ = "__TS__", "__DEV__", "__BID__", 987654321


def generic_template() -> dict[str, Any]:
    comps = ["cpu", "memory", "storage", "network", "battery", "thermal", "gpu", "os"]
    samples = []
    for i in range(169):
        comp = comps[i % len(comps)]
        samples.append(
            {
                "metric": f"{comp if comp != 'os' else 'system'}.load_metric_{i}",
                "component": comp,
                "value": round(random.uniform(0, 100), 2),
                "unit": "percent",
                "timestamp": TS,
                "source": "loadtest synthetic",
                "quality": "GOOD",
                "availability": "available",
                "kind": "measured",
                "labels": {},
            }
        )
    procs = [
        {
            "pid": 1000 + i,
            "name": f"process-{i}.exe",
            "status": "running",
            "cpu_percent": 1.0,
            "memory_rss_bytes": 10_000_000,
            "memory_percent": 0.5,
            "num_threads": 8,
            "io_read_bytes_per_sec": 0.0,
            "io_write_bytes_per_sec": 0.0,
        }
        for i in range(32)
    ]
    return {
        "samples": samples,
        "processes": {"timestamp": TS, "source": "loadtest", "total_processes": 250, "processes": procs},
    }


def build_template(path: str | None) -> str:
    data = json.loads(Path(path).read_text(encoding="utf-8")) if path else generic_template()
    data.update(
        {
            "schema_version": "1.1",
            "device_id": DEV,
            "batch_id": BID,
            "sequence": SEQ,
            "agent_version": "loadtest",
            "sent_at": TS,
            "collected_at": TS,
            "replay": False,
            "events": [],
            "agent_health": None,
        }
    )
    for s in data["samples"]:
        s["timestamp"] = TS
    if data.get("processes"):
        data["processes"]["timestamp"] = TS
    return json.dumps(data, separators=(",", ":"))


@dataclass
class Level:
    devices: int
    sent: int = 0
    accepted: int = 0
    errors: int = 0
    rate_limited: int = 0
    deferred: int = 0  # 503 overload: an agent keeps the batch queued and retries after Retry-After
    status: dict[str, int] = field(default_factory=dict)
    http_ms: list[float] = field(default_factory=list)
    wire_bytes: int = 0
    raw_bytes: int = 0
    ws_delivery_ms: list[float] = field(default_factory=list)
    ws_e2e_ms: list[float] = field(default_factory=list)
    ws_messages: int = 0
    ws_reconnects: int = 0
    twin_patches: int = 0
    twin_patch_bytes: list[int] = field(default_factory=list)
    twin_summaries: int = 0
    cpu: list[float] = field(default_factory=list)
    rss: list[int] = field(default_factory=list)
    persist_queue: list[int] = field(default_factory=list)
    ws_queue: list[int] = field(default_factory=list)


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(len(v) * q))], 1)


class Device:
    def __init__(self, idx: int) -> None:
        self.id = f"load-{idx:04d}"
        self.seq = 0
        self.headers: dict[str, str] = {}

    async def enroll(self, http: httpx.AsyncClient, key: str, inventory: dict[str, Any]) -> None:
        r = await http.post(
            "/api/v1/agent/register",
            json={"device_id": self.id, "agent_version": "loadtest"},
            headers={"X-Agent-Key": key},
        )
        r.raise_for_status()
        self.headers = {"Authorization": f"Bearer {r.json()['device_token']}", "X-Device-Id": self.id}
        env = {
            "schema_version": "1.1",
            "device_id": self.id,
            "agent_version": "loadtest",
            "discovered_at": datetime.now(UTC).isoformat(),
            "inventory": inventory,
        }
        (await http.post("/api/v1/ingest/inventory", json=env, headers=self.headers)).raise_for_status()

    async def run(
        self, http: httpx.AsyncClient, template: str, lvl: Level, until: float, interval: float
    ) -> None:
        await asyncio.sleep(random.uniform(0, interval))  # agents are not synchronised
        last_hb = 0.0
        while time.monotonic() < until:
            started = time.monotonic()
            self.seq += 1
            now = datetime.now(UTC).isoformat()
            body = (
                template.replace(TS, now)
                .replace(DEV, self.id)
                .replace(BID, uuid.uuid4().hex)
                .replace(str(SEQ), str(self.seq))
            )
            raw = ('{"batches":[' + body + "]}").encode()
            packed = gzip.compress(raw, 6)
            t0 = time.perf_counter()
            try:
                r = await http.post(
                    "/api/v1/ingest/telemetry/bulk",
                    content=packed,
                    headers={**self.headers, "Content-Encoding": "gzip", "Content-Type": "application/json"},
                )
                lvl.http_ms.append((time.perf_counter() - t0) * 1000)
                lvl.status[str(r.status_code)] = lvl.status.get(str(r.status_code), 0) + 1
                if r.status_code == 200:
                    lvl.accepted += int(r.json().get("accepted", 0))
                elif r.status_code == 429:
                    lvl.rate_limited += 1
                elif r.status_code == 503:
                    lvl.deferred += 1
                    await asyncio.sleep(float(r.headers.get("Retry-After", "5")))
                else:
                    lvl.errors += 1
            except httpx.HTTPError as exc:
                lvl.errors += 1
                lvl.status[type(exc).__name__] = lvl.status.get(type(exc).__name__, 0) + 1
            lvl.sent += 1
            lvl.wire_bytes += len(packed)
            lvl.raw_bytes += len(raw)
            if started - last_hb > 30:
                last_hb = started
                hb = {"device_id": self.id, "agent_version": "loadtest", "sent_at": now, "queue_depth": 0}
                try:
                    await http.post("/api/v1/agent/heartbeat", json=hb, headers=self.headers)
                except httpx.HTTPError:
                    lvl.errors += 1
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))


async def observe_ws(base: str, lvl: Level, until: float, first_device: str) -> None:
    """A dashboard-like client: subscribes, pings every 10 s (idle clients are closed after 45 s)."""
    url = base.replace("http", "ws", 1) + "/ws/twin"
    while time.monotonic() < until:
        try:
            async with websockets.connect(url, max_size=2**24) as ws:
                await ws.send(json.dumps({"type": "subscribe", "topics": ["fleet", f"device:{first_device}"]}))
                last_ping = time.monotonic()
                while time.monotonic() < until:
                    if time.monotonic() - last_ping > 10:
                        last_ping = time.monotonic()
                        await ws.send(json.dumps({"type": "ping"}))
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        continue
                    msg = json.loads(raw)
                    lvl.ws_messages += 1
                    if msg.get("event") == "twin.state.patch" and msg.get("device_id") == first_device:
                        lvl.twin_patches += 1
                        lvl.twin_patch_bytes.append(len(raw))
                    elif msg.get("event") == "twin.summary":
                        lvl.twin_summaries += 1
                    timing = msg.get("timing") if msg.get("event") == "telemetry_update" else None
                    if timing:
                        now = datetime.now(UTC)  # same host as the backend: no clock offset
                        published = datetime.fromisoformat(timing["published_at"])
                        lvl.ws_delivery_ms.append((now - published).total_seconds() * 1000)
                        collected = datetime.fromisoformat(timing["collected_at"])
                        lvl.ws_e2e_ms.append((now - collected).total_seconds() * 1000)
        except websockets.ConnectionClosed:
            lvl.ws_reconnects += 1


async def sample_backend(http: httpx.AsyncClient, pid: int | None, lvl: Level, until: float) -> None:
    proc = psutil.Process(pid) if pid else None
    if proc:
        proc.cpu_percent(None)
    while time.monotonic() < until:
        await asyncio.sleep(2.0)
        if proc:
            lvl.cpu.append(proc.cpu_percent(None))
            lvl.rss.append(proc.memory_info().rss)
        try:
            stats = (await http.get("/api/v1/pipeline/stats")).json()
            lvl.persist_queue.append(int(stats["persistence"]["queue_depth"]))
            lvl.ws_queue.append(int(stats["websocket"]["queued_messages"]))
        except Exception:
            pass


async def measure_twin_apis(http: httpx.AsyncClient, device: str) -> dict[str, Any]:
    """Response times of the Phase-3 read APIs while the load level is still fresh in memory."""
    calls = {
        "api_twin_snapshot": (f"/api/v1/devices/{device}/twin?format=flat", 20),
        "api_device_list_page": ("/api/v1/devices?page_size=50&sort=health", 10),
        "api_device_search": ("/api/v1/devices?q=load-00&page_size=50&sort=cpu&order=desc", 10),
        "api_fleet_summary": ("/api/v1/fleet/summary", 10),
        "api_timeline": (f"/api/v1/devices/{device}/timeline?limit=100", 5),
    }
    out: dict[str, Any] = {}
    for name, (path, n) in calls.items():
        times = []
        size = 0
        for _ in range(n):
            t0 = time.perf_counter()
            r = await http.get(path)
            times.append((time.perf_counter() - t0) * 1000)
            size = len(r.content)
        out[f"{name}_p50_ms"] = pct(times, 0.5)
        out[f"{name}_p95_ms"] = pct(times, 0.95)
        out[f"{name}_kb"] = round(size / 1024, 1)
    return out


def metric_sum(text: str, name: str, label: str = "") -> float:
    total = 0.0
    for line in text.splitlines():
        if line.startswith(name) and label in line:
            total += float(line.rsplit(" ", 1)[1])
    return total


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8020")
    ap.add_argument("--key", required=True, help="enrollment key of the TEST backend")
    ap.add_argument("--levels", default="1,10,50,100,500")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--template")
    ap.add_argument("--inventory")
    ap.add_argument("--backend-pid", type=int)
    ap.add_argument("--out", default="loadtest.json")
    args = ap.parse_args()

    template = build_template(args.template)
    inventory = (
        json.loads(Path(args.inventory).read_text(encoding="utf-8")).get("inventory", {})
        if args.inventory
        else {"manufacturer": "LOADTEST", "model": "Synthetic"}
    )
    levels = [int(x) for x in args.levels.split(",")]
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    results = []
    async with httpx.AsyncClient(base_url=args.base, timeout=30.0, limits=limits) as http:
        pool: list[Device] = []
        for n in levels:
            while len(pool) < n:
                d = Device(len(pool))
                await d.enroll(http, args.key, inventory)
                pool.append(d)
            m0 = (await http.get("/metrics")).text
            lvl = Level(n)
            until = time.monotonic() + args.duration
            t0 = time.monotonic()
            await asyncio.gather(
                *(d.run(http, template, lvl, until, args.interval) for d in pool[:n]),
                observe_ws(args.base, lvl, until + 2, pool[0].id),
                sample_backend(http, args.backend_pid, lvl, until),
            )
            elapsed = time.monotonic() - t0
            await asyncio.sleep(3)  # let the persister drain
            m1 = (await http.get("/metrics")).text
            db_n = metric_sum(m1, "ldt_db_operation_seconds_count", 'operation="samples_insert"') - metric_sum(
                m0, "ldt_db_operation_seconds_count", 'operation="samples_insert"'
            )
            db_s = metric_sum(m1, "ldt_db_operation_seconds_sum", 'operation="samples_insert"') - metric_sum(
                m0, "ldt_db_operation_seconds_sum", 'operation="samples_insert"'
            )
            proc_n = metric_sum(m1, "ldt_pipeline_latency_ms_count", 'stage="server_processing"') - metric_sum(
                m0, "ldt_pipeline_latency_ms_count", 'stage="server_processing"'
            )
            proc_s = metric_sum(m1, "ldt_pipeline_latency_ms_sum", 'stage="server_processing"') - metric_sum(
                m0, "ldt_pipeline_latency_ms_sum", 'stage="server_processing"'
            )
            dropped = metric_sum(m1, "ldt_persist_dropped_total") - metric_sum(m0, "ldt_persist_dropped_total")
            ws_dropped = metric_sum(m1, "ldt_websocket_dropped_total") - metric_sum(m0, "ldt_websocket_dropped_total")
            samples_per_batch = template.count('"metric":')
            row = {
                "devices": n,
                "duration_s": round(elapsed, 1),
                "batches_sent": lvl.sent,
                "batches_accepted": lvl.accepted,
                "errors": lvl.errors,
                "rate_limited": lvl.rate_limited,
                "deferred_503": lvl.deferred,
                "status": lvl.status,
                "throughput_batches_s": round(lvl.accepted / elapsed, 2),
                "throughput_samples_s": round(lvl.accepted * samples_per_batch / elapsed, 1),
                "http_p50_ms": pct(lvl.http_ms, 0.5),
                "http_p95_ms": pct(lvl.http_ms, 0.95),
                "http_p99_ms": pct(lvl.http_ms, 0.99),
                "server_processing_avg_ms": round(proc_s / proc_n, 2) if proc_n else None,
                "db_samples_insert_avg_ms": round(db_s / db_n * 1000, 1) if db_n else None,
                "db_inserts": int(db_n),
                "persist_queue_max": max(lvl.persist_queue, default=None),
                "persist_dropped": int(dropped),
                "ws_queue_max": max(lvl.ws_queue, default=None),
                "ws_messages": lvl.ws_messages,
                "ws_reconnects": lvl.ws_reconnects,
                "ws_delivery_p50_ms": pct(lvl.ws_delivery_ms, 0.5),
                "ws_delivery_p95_ms": pct(lvl.ws_delivery_ms, 0.95),
                "end_to_end_p50_ms": pct(lvl.ws_e2e_ms, 0.5),
                "end_to_end_p95_ms": pct(lvl.ws_e2e_ms, 0.95),
                "ws_slow_consumers_dropped": int(ws_dropped),
                "backend_cpu_avg_pct": round(statistics.fmean(lvl.cpu), 1) if lvl.cpu else None,
                "backend_cpu_max_pct": round(max(lvl.cpu), 1) if lvl.cpu else None,
                "backend_rss_max_mb": round(max(lvl.rss) / 2**20, 1) if lvl.rss else None,
                "wire_kb_per_batch": round(lvl.wire_bytes / max(1, lvl.sent) / 1024, 1),
                "raw_kb_per_batch": round(lvl.raw_bytes / max(1, lvl.sent) / 1024, 1),
            }
            row.update(await measure_twin_apis(http, pool[0].id))
            row["ws_twin_patches_for_one_device"] = lvl.twin_patches
            row["ws_twin_patch_avg_bytes"] = round(statistics.fmean(lvl.twin_patch_bytes)) if lvl.twin_patch_bytes else None
            row["ws_twin_summaries_fleet"] = lvl.twin_summaries
            stats = (await http.get("/api/v1/pipeline/stats")).json()
            proj = stats["ingest"]["latency"].get("twin_projection_ms", {})
            row["twin_projection_p50_ms"] = proj.get("p50")
            row["twin_projection_p95_ms"] = proj.get("p95")
            row["twin_documents"] = stats.get("twin", {}).get("documents")
            row["twin_avg_patch_fields"] = stats.get("twin", {}).get("avg_patch_fields")
            results.append(row)
            print(json.dumps(row), flush=True)
    Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
