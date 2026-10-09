"""Phase 10 failover drill: two backend processes, one database (active/standby election).

    python scripts/failover_drill.py --database-url postgresql+asyncpg://…/ldt_test [--redis-url redis://…/1]

Uses an ISOLATED database (refuses the default "ldt" name) and its own ports (8020 active candidate,
8021 standby candidate). Steps:
 1. start A; it must become active (readiness 200, role active);
 2. start B; it must be standby (readiness 503, API 503 STANDBY, no background loops);
 3. hard-kill A (simulated crash: TerminateProcess / SIGKILL);
 4. measure the time until B is active and serving the API;
 5. restart A; it must come back as standby.
Only processes started by this script are stopped. Results are printed as JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PY = BACKEND / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python"


def get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 - local drill endpoints
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except Exception:
        return 0, {}


def start(port: int, db: str, redis: str, log: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": db, "REDIS_URL": redis, "API_PORT": str(port), "API_HOST": "127.0.0.1",
        "AUTH_MODE": "none", "DIAGNOSIS_MODELS": "", "LOG_LEVEL": "WARNING", "SYNC_ENABLED": "false",
    })
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and module
        [str(PY), "-m", "app.main"], cwd=BACKEND, env=env, stdout=log.open("wb"), stderr=subprocess.STDOUT
    )


def wait_for(fn, timeout: float, step: float = 0.2) -> float | None:  # type: ignore[no-untyped-def]
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if fn():
            return round(time.monotonic() - t0, 2)
        time.sleep(step)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--redis-url", default="redis://127.0.0.1:16379/1")
    ap.add_argument("--logs", type=Path, default=Path("."))
    a = ap.parse_args()
    if a.database_url.rstrip("/").endswith("/ldt"):
        sys.exit("refusing to drill against the live database 'ldt'")
    subprocess.run([str(PY), "-m", "alembic", "upgrade", "head"], cwd=BACKEND, check=True,  # noqa: S603
                   env={**os.environ, "DATABASE_URL": a.database_url}, capture_output=True)
    A, B = "http://127.0.0.1:8020", "http://127.0.0.1:8021"
    procs: list[subprocess.Popen[bytes]] = []
    out: dict = {}
    try:
        pa = start(8020, a.database_url, a.redis_url, a.logs / "drill-a.log")
        procs.append(pa)
        out["a_active_after_s"] = wait_for(lambda: get(f"{A}/health/ready")[0] == 200, 60)
        pb = start(8021, a.database_url, a.redis_url, a.logs / "drill-b.log")
        procs.append(pb)
        wait_for(lambda: get(f"{B}/health/ready")[0] != 0, 60)
        code, body = get(f"{B}/health/ready")
        api_code, api_body = get(f"{B}/api/v1/device")
        out["b_standby"] = {"ready": code, "status": body.get("status"), "api": api_code, "api_code": api_body.get("code")}
        out["a_still_active"] = get(f"{A}/health/ready")[0] == 200
        t_kill = time.monotonic()
        pa.kill()  # crash, not a graceful stop
        pa.wait(10)
        took = wait_for(lambda: get(f"{B}/health/ready")[0] == 200, 60)
        out["failover_s"] = round(time.monotonic() - t_kill, 2) if took is not None else None
        out["b_after_failover"] = {"ready": get(f"{B}/health/ready")[0], "api": get(f"{B}/api/v1/system/info")[0]}
        pa2 = start(8020, a.database_url, a.redis_url, a.logs / "drill-a2.log")
        procs.append(pa2)
        wait_for(lambda: get(f"{A}/health/ready")[0] != 0, 60)
        out["a_rejoined_as"] = get(f"{A}/health/ready")[1].get("status")
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(10)
    ok = (
        out.get("a_active_after_s") is not None
        and out["b_standby"] == {"ready": 503, "status": "standby", "api": 503, "api_code": "STANDBY"}
        and out.get("failover_s") is not None
        and out["b_after_failover"]["ready"] == 200
        and out.get("a_rejoined_as") == "standby"
    )
    out["result"] = "PASS" if ok else "FAIL"
    print(json.dumps(out, indent=2))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
