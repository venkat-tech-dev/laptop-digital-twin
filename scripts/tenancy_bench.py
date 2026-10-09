"""Phase 9 performance: authorization overhead, tenant scale and noisy-neighbour isolation (in-process).

    python scripts/tenancy_bench.py --tenants 1 10 50 100 200 --devices 5
        [--out docs/tenancy-bench-results.json]

Everything goes through the real HTTP API of an in-process app (memory repositories, no database):
organizations are created by a platform administrator, devices enroll with single-use tokens and send
telemetry with their own credentials. Brute-force limiters on login/enrollment are lifted for the setup
only (they are per client IP and the benchmark is one client).

Measured per tenant count:
* GET /api/v1/org/devices (fleet with compliance) as an organization owner
* GET /api/v1/devices/{id}/twin (own device) and a cross-tenant probe of another organization's device
* POST /api/v1/ingest/telemetry from one device
Authorization overhead: the same twin read with AUTH_MODE=none (no identity, no tenancy checks).
Noisy neighbour: tenant A floods telemetry far above its quota from 6 threads while tenant B keeps
working; B's latency and errors are compared with a quiet baseline. The in-process test client serialises
all requests through one event loop (about 50-100 requests/s here), so A's quota is set low (60/min) to be
exceeded, and B's latency rise during the flood is queueing behind A's requests in this single process,
not tenant leakage. The isolation result is that B is never throttled while A is throttled at its quota.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.security import SlidingWindowRateLimiter  # noqa: E402
from app.main import create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from tests.conftest import sample, settings  # noqa: E402
from tests.unit.tenancy_helpers import PASSWORD, Device, bearer, enroll  # noqa: E402

OPEN = SlidingWindowRateLimiter(limit=10**9, window_s=60.0)


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 2) if xs else 0.0


def timed(fn: Any, n: int, warmup: int = 20) -> dict[str, float]:
    for _ in range(warmup):  # first calls pay import / cache costs
        fn()
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
    return {"p50_ms": pct(out, 0.5), "p95_ms": pct(out, 0.95), "mean_ms": round(statistics.fmean(out), 2)}


def post_telemetry(c: TestClient, d: Device, lock: threading.Lock | None = None) -> int:
    if lock:
        with lock:
            d.seq += 1
            seq = d.seq
    else:
        d.seq += 1
        seq = d.seq
    ts = datetime.now(UTC)
    body = {
        "device_id": d.device_id,
        "agent_version": "1.6.0",
        "sequence": seq,
        "sent_at": ts.isoformat(),
        "samples": [sample("cpu.usage_percent", 30.0, ts=ts)],
    }
    return c.post("/api/v1/ingest/telemetry", json=body, headers=d.headers).status_code


def accounts_app() -> TestClient:
    return TestClient(
        create_app(settings(AUTH_MODE="accounts", JWT_SECRET="b" * 40, RATE_LIMIT_PER_MINUTE=10**7))
    )


def build(c: TestClient, tenants: int, devices: int) -> dict[str, Any]:
    ct = c.app.state.container  # type: ignore[attr-defined]
    ct.login_limiter = ct.enroll_limiter = OPEN
    root = bearer(
        c.post("/api/v1/auth/setup", json={"username": "root", "password": PASSWORD}).json()["access_token"]
    )
    owners: dict[str, dict[str, str]] = {}
    devs: dict[str, list[Device]] = {}
    t0 = time.perf_counter()
    for i in range(tenants):
        org = f"org{i:04d}"
        assert (
            c.post(
                "/api/v1/platform/organizations", json={"org_id": org, "name": org}, headers=root
            ).status_code
            == 201
        )
        hdr = {**root, "X-Organization-Id": org}
        r = c.post(
            "/api/v1/org/members",
            json={"username": f"owner{i}", "password": PASSWORD, "role": "org_owner"},
            headers=hdr,
        )
        assert r.status_code == 201, r.text
        devs[org] = [enroll(c, hdr, f"dev-{org}-{j:03d}") for j in range(devices)]
        owners[org] = bearer(
            c.post("/api/v1/auth/login", json={"username": f"owner{i}", "password": PASSWORD}).json()[
                "access_token"
            ]
        )
    return {"root": root, "owners": owners, "devices": devs, "setup_s": round(time.perf_counter() - t0, 2)}


def scale(tenants: int, devices: int, n: int) -> dict[str, Any]:
    with accounts_app() as c:
        w = build(c, tenants, devices)
        org0 = "org0000"
        owner, mine = w["owners"][org0], w["devices"][org0][0]
        other = w["devices"][f"org{tenants - 1:04d}"][0] if tenants > 1 else None

        def probe() -> None:
            assert c.get(f"/api/v1/devices/{other.device_id}/twin", headers=owner).status_code == 404  # type: ignore[union-attr]

        return {
            "tenants": tenants,
            "devices_total": tenants * devices,
            "setup_s": w["setup_s"],
            "org_fleet_list": timed(lambda: c.get("/api/v1/org/devices", headers=owner), n),
            "twin_read_own": timed(lambda: c.get(f"/api/v1/devices/{mine.device_id}/twin", headers=owner), n),
            "cross_tenant_probe_404": timed(probe, n) if other else None,
            "telemetry_ingest": timed(lambda: post_telemetry(c, mine), n),
        }


def authz_overhead(n: int) -> dict[str, Any]:
    with TestClient(create_app(settings())) as c:  # AUTH_MODE=none: single local operator, no tenancy checks
        ct = c.app.state.container  # type: ignore[attr-defined]
        ct.enroll_limiter = OPEN
        d = enroll_legacy(c, "dev-none-0001")
        none = timed(lambda: c.get(f"/api/v1/devices/{d}/twin"), n)
    with accounts_app() as c:
        w = build(c, 1, 1)
        owner, mine = w["owners"]["org0000"], w["devices"]["org0000"][0]
        acc = timed(lambda: c.get(f"/api/v1/devices/{mine.device_id}/twin", headers=owner), n)
    return {
        "auth_none": none,
        "accounts_with_tenancy": acc,
        "overhead_p50_ms": round(acc["p50_ms"] - none["p50_ms"], 2),
    }


def enroll_legacy(c: TestClient, device_id: str) -> str:
    from tests.conftest import AGENT_KEY, INVENTORY

    env = {
        "device_id": device_id,
        "agent_version": "1.6.0",
        "discovered_at": datetime.now(UTC).isoformat(),
        "inventory": INVENTORY,
    }
    assert c.post("/api/v1/ingest/inventory", json=env, headers={"X-Agent-Key": AGENT_KEY}).status_code == 202
    return device_id


def noisy_neighbour(seconds: float, quota: int) -> dict[str, Any]:
    with accounts_app() as c:
        w = build(c, 2, 2)
        a, b = "org0000", "org0001"
        assert (
            c.patch(
                f"/api/v1/platform/organizations/{a}",
                headers=w["root"],
                json={"quotas": {"telemetry_batches_per_min": {"limit": quota, "mode": "THROTTLE"}}},
            ).status_code
            == 200
        )
        dev_b, owner_b = w["devices"][b][0], w["owners"][b]
        lock = threading.Lock()

        def b_round() -> tuple[list[float], list[int]]:
            lat, codes = [], []
            end = time.perf_counter() + seconds
            while time.perf_counter() < end:
                t0 = time.perf_counter()
                codes.append(post_telemetry(c, dev_b, lock))
                codes.append(c.get("/api/v1/org/devices", headers=owner_b).status_code)
                lat.append((time.perf_counter() - t0) * 1000)
            return lat, codes

        base_lat, base_codes = b_round()
        stop = threading.Event()
        a_codes: list[int] = []

        def flood(d: Device) -> None:
            while not stop.is_set():
                a_codes.append(post_telemetry(c, d, lock))

        with ThreadPoolExecutor(6) as pool:
            for i in range(6):
                pool.submit(flood, w["devices"][a][i % 2])
            flood_lat, flood_codes = b_round()
            stop.set()
        return {
            "tenant_a_quota_per_min": quota,
            "tenant_a_requests": len(a_codes),
            "tenant_a_accepted": a_codes.count(202),
            "tenant_a_throttled_429": a_codes.count(429),
            "tenant_b_baseline": {
                "rounds": len(base_lat),
                "p50_ms": pct(base_lat, 0.5),
                "p95_ms": pct(base_lat, 0.95),
                "non_2xx": sum(1 for x in base_codes if x >= 300),
            },
            "tenant_b_during_flood": {
                "rounds": len(flood_lat),
                "p50_ms": pct(flood_lat, 0.5),
                "p95_ms": pct(flood_lat, 0.95),
                "non_2xx": sum(1 for x in flood_codes if x >= 300),
            },
        }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenants", type=int, nargs="+", default=[1, 10, 50, 100])
    ap.add_argument("--devices", type=int, default=5)
    ap.add_argument("-n", type=int, default=200, help="samples per measurement")
    ap.add_argument("--flood-seconds", type=float, default=5.0)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    import logging

    logging.disable(logging.WARNING)
    result: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "in-process, memory repositories",
    }
    result["authorization_overhead"] = authz_overhead(a.n)
    print(json.dumps(result["authorization_overhead"]), flush=True)
    result["scale"] = []
    for t in a.tenants:
        r = scale(t, a.devices, a.n)
        result["scale"].append(r)
        print(json.dumps(r), flush=True)
    result["noisy_neighbour"] = noisy_neighbour(a.flood_seconds, quota=60)
    print(json.dumps(result["noisy_neighbour"]), flush=True)
    if a.out:
        a.out.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
