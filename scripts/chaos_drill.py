"""Phase 10 chaos drill: database outage during ingestion, duplicates, out-of-order data, crash window.

    python scripts/chaos_drill.py [--keep]

Fully isolated: starts its OWN PostgreSQL/TimescaleDB container (``ldt-chaos-pg`` on 127.0.0.1:15499,
from the locally available image) and its OWN backend on 127.0.0.1:8022 (no Redis, AUTH_MODE=none).
Never touches the project's live containers or ports. Removes everything it created at the end.

Phases:
 A  normal ingestion: N batches accepted and persisted
 B  database paused (docker pause): ingestion keeps being accepted (202), readiness reports the outage,
    the leader does NOT step down; duplicates and out-of-order batches are absorbed
 C  database resumed: everything accepted during B is persisted exactly once (no loss, no duplicates)
 D  crash window: database paused, more batches accepted, backend hard-killed, database resumed;
    measures how many accepted samples were lost (documented loss window: the in-memory write queue)
Prints JSON; exit code 1 if an expectation fails.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PY = BACKEND / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python"
IMAGE = "timescale/timescaledb:2.17.2-pg16"
NAME = "ldt-chaos-pg"
PORT = 15499
API = "http://127.0.0.1:8022"
KEY = "chaos-drill-key-0123456789abcdef"
DEVICE = "chaos-device-0001"
sys.path.insert(0, str(BACKEND))
from tests.conftest import INVENTORY  # noqa: E402  (realistic inventory shape)


def sh(*args: str, check: bool = True) -> str:
    r = subprocess.run(list(args), capture_output=True, text=True)  # noqa: S603 - fixed docker/psql argv
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip()[-400:]}")
    return r.stdout.strip()


def http(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,  # noqa: S310 - local drill endpoint
                                 headers={"Content-Type": "application/json", "X-Agent-Key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except Exception:
        return 0, {}


def psql(sql: str) -> str:
    return sh("docker", "exec", NAME, "psql", "-U", "ldt", "-d", "chaos", "-tAc", sql)


def persisted() -> int:
    return int(psql(
        "SELECT count(*) FROM telemetry_samples s JOIN telemetry_metrics m ON m.id = s.metric_id "
        f"WHERE m.device_id = '{DEVICE}'"
    ) or 0)


def wait_for(fn, timeout: float, step: float = 0.5):  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


class Agent:
    """Minimal synthetic agent: one series, one sample per batch, 6 s apart (persister keeps 1 per 5 s)."""

    def __init__(self) -> None:
        self.seq = 0
        self.t0 = datetime.now(UTC) - timedelta(hours=2)
        self.sent: list[dict] = []

    def batch(self, ts: datetime | None = None) -> dict:
        self.seq += 1
        ts = ts or self.t0 + timedelta(seconds=6 * self.seq)
        return {
            "device_id": DEVICE, "agent_version": "1.7.0", "sequence": self.seq, "batch_id": f"chaos-{self.seq}",
            "sent_at": datetime.now(UTC).isoformat(), "collected_at": ts.isoformat(), "replay": False,
            "samples": [{"metric": "cpu.usage_percent", "component": "cpu", "value": float(self.seq % 100),
                         "unit": "percent", "timestamp": ts.isoformat(), "quality": "GOOD",
                         "availability": "available", "source": "drill", "kind": "measured", "labels": {}}],
        }

    def send(self, b: dict) -> int:
        code, _ = http("POST", "/api/v1/ingest/telemetry", b)
        if code == 202:
            self.sent.append(b)
        return code


def start_backend(url: str, log: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": url, "REDIS_URL": "", "API_PORT": "8022", "API_HOST": "127.0.0.1", "AUTH_MODE": "none",
        "AGENT_INGEST_KEY": KEY, "ALLOW_ENROLLMENT_KEY_INGEST": "true", "DIAGNOSIS_MODELS": "",
        "LOG_LEVEL": "WARNING", "INGEST_RATE_PER_DEVICE_PER_MIN": "100000", "DB_STATEMENT_TIMEOUT_MS": "5000",
    })
    return subprocess.Popen([str(PY), "-m", "app.main"], cwd=BACKEND, env=env,  # noqa: S603
                            stdout=log.open("wb"), stderr=subprocess.STDOUT)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", action="store_true", help="keep the container (debugging)")
    ap.add_argument("--logs", type=Path, default=Path("."))
    a = ap.parse_args()
    if sh("docker", "ps", "-a", "--filter", f"name=^{NAME}$", "--format", "{{.Names}}"):
        sys.exit(f"container {NAME} already exists; remove it first")
    pw = secrets.token_urlsafe(16)
    out: dict = {}
    proc: subprocess.Popen[bytes] | None = None
    try:
        sh("docker", "run", "-d", "--name", NAME, "-e", "POSTGRES_USER=ldt", "-e", f"POSTGRES_PASSWORD={pw}",
           "-e", "POSTGRES_DB=chaos", "-p", f"127.0.0.1:{PORT}:5432", IMAGE)
        if not wait_for(lambda: sh("docker", "exec", NAME, "pg_isready", "-U", "ldt", "-d", "chaos", check=False).endswith("accepting connections"), 90):
            raise RuntimeError("database did not start")
        time.sleep(3)  # the image restarts postgres once after init
        wait_for(lambda: sh("docker", "exec", NAME, "pg_isready", "-U", "ldt", "-d", "chaos", check=False).endswith("accepting connections"), 60)
        url = f"postgresql+asyncpg://ldt:{pw}@127.0.0.1:{PORT}/chaos"
        subprocess.run([str(PY), "-m", "alembic", "upgrade", "head"], cwd=BACKEND, check=True,  # noqa: S603
                       env={**os.environ, "DATABASE_URL": url}, capture_output=True)
        proc = start_backend(url, a.logs / "chaos-backend.log")
        if not wait_for(lambda: http("GET", "/health/ready")[0] == 200, 90):
            raise RuntimeError("backend not ready")
        inv = {"device_id": DEVICE, "agent_version": "1.7.0", "discovered_at": datetime.now(UTC).isoformat(),
               "inventory": json.loads((BACKEND / "tests" / "fixtures" / "inventory.json").read_text())
               if (BACKEND / "tests" / "fixtures" / "inventory.json").exists() else INVENTORY}
        out["inventory"] = http("POST", "/api/v1/ingest/inventory", inv)[0]
        ag = Agent()

        # A: normal
        out["A_accepted"] = sum(ag.send(ag.batch()) == 202 for _ in range(20))
        out["A_persisted"] = wait_for(lambda: (n := persisted()) >= 20 and n, 30) or persisted()

        # B: database paused
        sh("docker", "pause", NAME)
        t_pause = time.monotonic()
        codes = [ag.send(ag.batch()) for _ in range(30)]
        dup_codes = [http("POST", "/api/v1/ingest/telemetry", ag.sent[-1 - i])[1].get("duplicate") for i in range(5)]
        old_ts = ag.t0 + timedelta(seconds=1)  # out-of-order: older than everything sent
        codes.append(ag.send(ag.batch(ts=old_ts)))
        time.sleep(8)  # longer than one leader probe (5 s): a slow DB must not make the leader step down
        ready_code, ready = http("GET", "/health/ready")
        out["B"] = {
            "accepted_while_db_paused": codes.count(202), "other_codes": sorted({c for c in codes if c != 202}),
            "duplicates_reported": dup_codes, "readiness_during_outage": ready_code,
            "database_check": (ready.get("checks") or {}).get("database", {}).get("status"),
            "backend_alive_after_8s": proc.poll() is None,
        }
        sh("docker", "unpause", NAME)
        out["B"]["outage_s"] = round(time.monotonic() - t_pause, 1)

        # C: recovery
        expected = len({b["batch_id"] for b in ag.sent})
        got = wait_for(lambda: (n := persisted()) >= expected and n, 90) or persisted()
        dupes = int(psql(
            "SELECT count(*) - count(DISTINCT (s.time, s.metric_id)) FROM telemetry_samples s "
            f"JOIN telemetry_metrics m ON m.id = s.metric_id WHERE m.device_id = '{DEVICE}'") or 0)
        out["C"] = {"expected_samples": expected, "persisted_samples": got, "duplicate_rows": dupes,
                    "readiness_after": http("GET", "/health/ready")[0]}

        # D: crash with a queued backlog, then recovery the way agents >= 1.8 do it
        sh("docker", "pause", NAME)
        before = len(ag.sent)
        d_codes = [ag.send(ag.batch()) for _ in range(10)]
        proc.kill()  # crash while 10 accepted batches wait in memory
        proc.wait(10)
        sh("docker", "unpause", NAME)
        time.sleep(3)
        lost_before = len(ag.sent) - before - (persisted() - got)
        proc = start_backend(url, a.logs / "chaos-backend-restart.log")
        if not wait_for(lambda: http("GET", "/health/ready")[0] == 200, 90):
            raise RuntimeError("backend did not restart")
        hb = http("POST", "/api/v1/agent/heartbeat", {
            "device_id": DEVICE, "agent_version": "1.8.0", "sent_at": datetime.now(UTC).isoformat(),
            "unconfirmed_batch_ids": [b["batch_id"] for b in ag.sent],
        })[1]
        unknown = set(hb.get("unknown_batch_ids") or [])
        resent = [http("POST", "/api/v1/ingest/telemetry", b)[0] for b in ag.sent if b["batch_id"] in unknown]
        total = len({b["batch_id"] for b in ag.sent})
        final = wait_for(lambda: (n := persisted()) >= total and n, 60) or persisted()
        out["D"] = {"accepted_then_crashed": d_codes.count(202), "lost_at_crash": lost_before,
                    "reported_unknown_after_restart": len(unknown), "resent_accepted": resent.count(202),
                    "durable_confirmed": len(hb.get("durable_batch_ids") or []),
                    "expected_samples": total, "persisted_samples": final, "lost_after_recovery": total - final}
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(10)
        if not a.keep:
            sh("docker", "rm", "-f", NAME, check=False)
    ok = (
        out.get("A_persisted") == 20
        and out["B"]["accepted_while_db_paused"] == 31
        and out["B"]["duplicates_reported"] == [True] * 5
        and out["B"]["readiness_during_outage"] == 503
        and out["B"]["backend_alive_after_8s"]
        and out["C"]["persisted_samples"] == out["C"]["expected_samples"]
        and out["C"]["duplicate_rows"] == 0
        and out["C"]["readiness_after"] == 200
        and out["D"]["lost_after_recovery"] == 0
    )
    out["result"] = "PASS" if ok else "FAIL"
    print(json.dumps(out, indent=2))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
