"""Durable execution ledger: idempotency (execution id -> result) and nonce replay protection.

Stored as JSON in the agent's data directory (written atomically) so a retry after a network failure or an
agent restart returns the earlier result instead of running the action twice.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

KEEP_DAYS = 14
MAX_ENTRIES = 2000


class ExecutionLedger:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"executions": {}, "nonces": {}}
        with contextlib.suppress(OSError, ValueError):
            self._data = json.loads(path.read_text(encoding="utf-8"))
        self._data.setdefault("executions", {})
        self._data.setdefault("nonces", {})

    def get(self, execution_id: str) -> dict[str, Any] | None:
        r = self._data["executions"].get(execution_id)
        return dict(r) if isinstance(r, dict) else None

    def nonce_used(self, nonce: str, execution_id: str) -> bool:
        """A nonce belongs to exactly one execution; reuse for another execution is a replay."""
        owner = self._data["nonces"].get(nonce)
        return owner is not None and owner != execution_id

    def begin(self, execution_id: str, nonce: str, action_id: str) -> None:
        with self._lock:
            now = datetime.now(UTC).isoformat()
            self._data["executions"][execution_id] = {"phase": "running", "action_id": action_id, "at": now}
            self._data["nonces"][nonce] = execution_id
            self._write()

    def finish(self, execution_id: str, phase: str, detail: str, data: dict[str, Any]) -> None:
        with self._lock:
            entry = self._data["executions"].setdefault(execution_id, {})
            entry.update(
                {"phase": phase, "detail": detail, "data": data, "at": datetime.now(UTC).isoformat()}
            )
            self._prune()
            self._write()

    def _prune(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=KEEP_DAYS)).isoformat()
        ex = self._data["executions"]
        for k in [k for k, v in ex.items() if str(v.get("at", "")) < cutoff][: max(0, len(ex) - 100)]:
            ex.pop(k, None)
        while len(ex) > MAX_ENTRIES:
            ex.pop(next(iter(ex)))
        live = set(ex)
        self._data["nonces"] = {n: e for n, e in self._data["nonces"].items() if e in live}

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        os.replace(tmp, self._path)
