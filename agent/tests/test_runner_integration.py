"""End-to-end agent lifecycle on real Windows collectors (no backend reachable).

Covers: collection without a backend/browser, local caching, graceful stop requested from another
thread (as the Windows service control handler does), and state recovery after restart.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest

from app.config.settings import AgentSettings, RunMode
from app.runner import AgentRunner

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real Windows collectors")


def _settings(data_dir: Path) -> AgentSettings:
    return AgentSettings(
        _env_file=None,  # type: ignore[call-arg]
        AGENT_BACKEND_URL="http://127.0.0.1:9",  # nothing listens here: backend unavailable
        AGENT_INGEST_KEY="integration-test-key",
        AGENT_DATA_DIR=str(data_dir),
        TELEMETRY_INTERVAL_MS=1000,
        PUBLISH_INTERVAL_MS=1000,
        OFFLINE_FLUSH_INTERVAL_S=5,
        ENABLE_UPDATE_COLLECTION=False,
        CONFIG_POLL_INTERVAL_S=0,
        AGENT_HEALTH_INTERVAL_S=10,
        AGENT_REQUEST_TIMEOUT_S=1,
    )


def _run(runner: AgentRunner, seconds: float) -> float:
    def stopper() -> None:
        time.sleep(seconds)
        runner.request_stop()  # thread-safe, like SvcStop / SvcShutdown

    threading.Thread(target=stopper, daemon=True).start()
    started = time.monotonic()
    asyncio.run(runner.run())
    return time.monotonic() - started


def _outbox(data_dir: Path) -> tuple[list[dict[str, object]], dict[str, str]]:
    db = sqlite3.connect(data_dir / "telemetry.db")
    rows = [json.loads(zlib.decompress(r[0])) for r in db.execute("SELECT payload FROM outbox ORDER BY id")]
    state = dict(db.execute("SELECT key, value FROM state").fetchall())
    db.close()
    return rows, state


def test_offline_collection_graceful_stop_and_restart_recovery(tmp_path: Path) -> None:
    elapsed = _run(AgentRunner(_settings(tmp_path), RunMode.SERVICE), 15)
    assert elapsed < 45, "graceful stop must complete promptly"
    batches, state = _outbox(tmp_path)
    assert batches, "telemetry must be cached locally while the backend is unreachable"
    events = [e["type"] for b in batches for e in b["events"]]  # type: ignore[index, union-attr]
    assert "agent_started" in events and "agent_stopped" in events  # final flush on stop
    metrics = {s["metric"] for b in batches for s in b["samples"]}  # type: ignore[index, union-attr]
    assert {"cpu.usage_percent", "memory.usage_percent", "network.internet_connected"} <= metrics
    assert any(b["agent_health"] for b in batches)
    health = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert health["run_mode"] == "service" and health["sync_online"] is False
    first_sequence = int(state["sequence"])

    _run(AgentRunner(_settings(tmp_path), RunMode.SERVICE), 8)  # restart: queue + sequence survive
    batches2, state2 = _outbox(tmp_path)
    assert len(batches2) > len(batches) and int(state2["sequence"]) > first_sequence
    seqs = [int(b["sequence"]) for b in batches2]  # type: ignore[call-overload]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # no reuse after restart
