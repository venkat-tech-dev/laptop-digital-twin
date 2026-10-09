"""Local-first telemetry store: a durable SQLite outbox plus a small key/value state table.

* Every telemetry batch is written here before any upload attempt, so collection never depends on
  the network and nothing is lost on agent/Windows restart (WAL journal, ``synchronous=NORMAL``).
* Payloads are zlib-compressed JSON. ``batch_id`` is UNIQUE, so re-inserting is a no-op.
* Every row carries a priority (0 LOW, 1 NORMAL, 2 HIGH, 3 CRITICAL = the highest priority of the
  batch content). Uploads go newest-first for the live view, then the backlog by priority and age.
* Bounded, with an explicit backpressure policy (the database can never grow indefinitely):
    1. age:     NORMAL/LOW rows older than ``max_age_s`` expire; HIGH/CRITICAL get twice as long
    2. thin:    over the count/byte limit, every other *old* NORMAL/LOW batch is removed first
                (consecutive samples are largely redundant; the trend survives at half resolution);
                the newest quarter of the queue is never thinned
    3. evict:   still over the limit, the oldest rows are deleted, LOW/NORMAL before HIGH before
                CRITICAL; the newest row (latest state) is never deleted
  Everything removed is counted (``thinned_total``, ``dropped_total``) and logged.
* A corrupt database file is moved aside (``*.corrupt-<timestamp>``) and a fresh one is created;
  the agent keeps running.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger("agent.store")

SCHEMA_VERSION = 3  # 3: sent_at (Phase 10 durable confirmation)
PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "critical": 3}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    size INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    priority INTEGER NOT NULL DEFAULT 1,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox (next_attempt_at, id);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass(frozen=True, slots=True)
class QueuedBatch:
    row_id: int
    batch_id: str
    created_at: float
    attempts: int
    payload: bytes  # decompressed JSON


@dataclass(frozen=True, slots=True)
class StoreStats:
    depth: int
    bytes: int
    oldest_age_s: float | None
    dropped_total: int
    dead_lettered_total: int
    recovered_from_corruption: int
    thinned_total: int = 0
    by_priority: dict[str, int] | None = None


class TelemetryStore:
    def __init__(
        self,
        path: Path,
        max_batches: int,
        max_bytes: int,
        max_age_s: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._max_batches = max_batches
        self._max_bytes = max_bytes
        self._max_age_s = max_age_s
        self._clock = clock
        self._lock = threading.Lock()
        self._db = self._open()

    # ------------------------------------------------------------------ lifecycle
    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self._path, timeout=5.0, check_same_thread=False, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            row = db.execute("PRAGMA quick_check").fetchone()
            if row is None or row[0] != "ok":
                raise sqlite3.DatabaseError(f"quick_check failed: {row}")
            db.executescript(_SCHEMA)
            columns = {r[1] for r in db.execute("PRAGMA table_info(outbox)")}
            if "priority" not in columns:  # v1 -> v2 in place: existing rows become NORMAL
                db.execute("ALTER TABLE outbox ADD COLUMN priority INTEGER NOT NULL DEFAULT 1")
            if (
                "sent_at" not in columns
            ):  # v2 -> v3: accepted by the backend, waiting for durable confirmation
                db.execute("ALTER TABLE outbox ADD COLUMN sent_at REAL")
            db.execute("CREATE INDEX IF NOT EXISTS ix_outbox_priority ON outbox (priority, id)")
            db.execute("UPDATE state SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))
            db.execute(
                "INSERT OR IGNORE INTO state(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
        except sqlite3.DatabaseError:
            db.close()  # release the file handle so the corrupt file can be moved aside (Windows)
            raise
        return db

    def _open(self) -> sqlite3.Connection:
        try:
            return self._connect()
        except sqlite3.DatabaseError as exc:
            stamp = time.strftime("%Y%m%dT%H%M%S")
            log.error("store_corrupt_recreating", path=str(self._path), error=str(exc)[:200])
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self._path) + suffix)
                if p.exists():
                    p.replace(p.with_name(p.name + f".corrupt-{stamp}"))
            db = self._connect()
            self._set(
                db, "recovered_from_corruption", str(int(self._get(db, "recovered_from_corruption") or 0) + 1)
            )
            return db

    def close(self) -> None:
        with self._lock:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._db.close()

    # ------------------------------------------------------------------ state (bookmarks, counters)
    @staticmethod
    def _get(db: sqlite3.Connection, key: str) -> str | None:
        row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    @staticmethod
    def _set(db: sqlite3.Connection, key: str, value: str) -> None:
        db.execute(
            "INSERT INTO state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_state(self, key: str) -> str | None:
        with self._lock:
            return self._get(self._db, key)

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._set(self._db, key, value)

    def _bump(self, key: str, n: int) -> None:
        if n:
            self._set(self._db, key, str(int(self._get(self._db, key) or 0) + n))

    # ------------------------------------------------------------------ outbox
    def put(self, batch_id: str, payload: bytes, priority: int = 1) -> bool:
        """Persist one batch (idempotent). Returns False if the batch_id was already stored."""
        blob = zlib.compress(payload, 6)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO outbox(batch_id, created_at, size, priority, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (batch_id, self._clock(), len(blob), max(0, min(3, priority)), blob),
            )
            inserted = cur.rowcount == 1
            self._enforce_limits()
        return inserted

    def due(self, limit: int, newest_first: bool = False) -> list[QueuedBatch]:
        order = "id DESC" if newest_first else "priority DESC, id ASC"
        with self._lock:
            rows = self._db.execute(
                f"SELECT id, batch_id, created_at, attempts, payload FROM outbox "  # noqa: S608 (order is fixed)
                f"WHERE next_attempt_at <= ? ORDER BY {order} LIMIT ?",
                (self._clock(), limit),
            ).fetchall()
        out = []
        for row_id, batch_id, created, attempts, blob in rows:
            try:
                payload = zlib.decompress(blob)
            except zlib.error:
                log.error("store_row_undecodable_dropped", batch_id=batch_id)
                self.dead_letter([row_id], "undecodable payload")
                continue
            out.append(QueuedBatch(row_id, batch_id, created, attempts, payload))
        return out

    def ack(self, row_ids: list[int]) -> None:
        if not row_ids:
            return
        with self._lock:
            self._db.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in row_ids])

    # ---- Phase 10: keep accepted batches until the backend confirms they are durable
    def mark_sent(self, row_ids: list[int], resend_after_s: float) -> None:
        """Accepted (202) but maybe not yet written: keep the batch, do not send it again unless the backend
        later reports it unknown (it crashed before writing) or no confirmation arrives in time."""
        if not row_ids:
            return
        now = self._clock()
        with self._lock:
            self._db.executemany(
                "UPDATE outbox SET sent_at = ?, next_attempt_at = ? WHERE id = ?",
                [(now, now + resend_after_s, i) for i in row_ids],
            )

    def unconfirmed(self, limit: int) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT batch_id FROM outbox WHERE sent_at IS NOT NULL ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [r[0] for r in rows]

    def confirm(self, batch_ids: list[str]) -> int:
        """The backend has these batches durably: delete them."""
        if not batch_ids:
            return 0
        with self._lock:
            n = sum(
                self._db.execute(
                    "DELETE FROM outbox WHERE batch_id = ? AND sent_at IS NOT NULL", (b,)
                ).rowcount
                for b in batch_ids
            )
        return n

    def resend(self, batch_ids: list[str]) -> int:
        """The backend lost these accepted batches (e.g. it crashed before writing): send them again."""
        if not batch_ids:
            return 0
        with self._lock:
            n = sum(
                self._db.execute(
                    "UPDATE outbox SET sent_at = NULL, next_attempt_at = 0 "
                    "WHERE batch_id = ? AND sent_at IS NOT NULL",
                    (b,),
                ).rowcount
                for b in batch_ids
            )
        return n

    def retry_later(self, row_ids: list[int], error: str, at: float, count_attempt: bool) -> None:
        if not row_ids:
            return
        with self._lock:
            self._db.executemany(
                "UPDATE outbox SET attempts = attempts + ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
                [(1 if count_attempt else 0, at, error[:300], i) for i in row_ids],
            )

    def dead_letter(self, row_ids: list[int], reason: str) -> None:
        """Permanently drop batches the backend rejected as invalid (counted, logged)."""
        if not row_ids:
            return
        with self._lock:
            self._db.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in row_ids])
            self._bump("dead_lettered_total", len(row_ids))
        log.error("batches_dead_lettered", count=len(row_ids), reason=reason[:200])

    def _over(self) -> tuple[int, int, bool]:
        depth, size = self._db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM outbox").fetchone()
        return int(depth), int(size), depth > self._max_batches or size > self._max_bytes

    def _delete(self, ids: list[int]) -> None:
        self._db.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])

    def _enforce_limits(self) -> None:
        now = self._clock()
        dropped = self._db.execute(
            "DELETE FROM outbox WHERE (priority <= 1 AND created_at < ?) OR created_at < ?",
            (now - self._max_age_s, now - 2 * self._max_age_s),
        ).rowcount
        depth, size, over = self._over()
        thinned = 0
        if over:  # first give up batches the backend already accepted (most likely durable)
            sent = [
                r[0] for r in self._db.execute("SELECT id FROM outbox WHERE sent_at IS NOT NULL ORDER BY id")
            ]
            excess = max(0, depth - self._max_batches) or len(sent)
            self._delete(sent[:excess])
            depth, size, over = self._over()
        if over:
            thinned = self._thin(depth, size)
            depth, size, over = self._over()
        if over:
            newest = self._db.execute("SELECT MAX(id) FROM outbox").fetchone()[0]
            excess_rows = max(0, depth - self._max_batches)
            rows = self._db.execute(
                "SELECT id, size FROM outbox WHERE id != ? ORDER BY priority ASC, id ASC", (newest,)
            ).fetchall()
            ids: list[int] = []
            for row_id, row_size in rows:
                if excess_rows <= 0 and size <= self._max_bytes:
                    break
                ids.append(row_id)
                excess_rows -= 1
                size -= row_size
            self._delete(ids)
            dropped += len(ids)
        if dropped:
            self._bump("dropped_total", dropped)
        if thinned:
            self._bump("thinned_total", thinned)
        if dropped or thinned:
            log.warning("queue_backpressure", dropped=dropped, thinned=thinned, depth=depth)

    def _thin(self, depth: int, size: int) -> int:
        """Remove every other old NORMAL/LOW batch until the limits hold (newest quarter untouched)."""
        protected = max(1, depth // 4)
        rows = self._db.execute(
            "SELECT id, size FROM outbox WHERE priority <= 1 AND id < "
            "(SELECT MIN(id) FROM (SELECT id FROM outbox ORDER BY id DESC LIMIT ?)) ORDER BY id ASC",
            (protected,),
        ).fetchall()
        ids: list[int] = []
        for i, (row_id, row_size) in enumerate(rows):
            if depth - len(ids) <= self._max_batches and size <= self._max_bytes:
                break
            if i % 2 == 1:
                ids.append(row_id)
                size -= row_size
        self._delete(ids)
        return len(ids)

    def stats(self) -> StoreStats:
        with self._lock:
            depth, size, oldest = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0), MIN(created_at) FROM outbox"
            ).fetchone()
            dropped = int(self._get(self._db, "dropped_total") or 0)
            thinned = int(self._get(self._db, "thinned_total") or 0)
            by_priority = dict(
                self._db.execute("SELECT priority, COUNT(*) FROM outbox GROUP BY priority").fetchall()
            )
            dead = int(self._get(self._db, "dead_lettered_total") or 0)
            recovered = int(self._get(self._db, "recovered_from_corruption") or 0)
        return StoreStats(
            depth=int(depth),
            bytes=int(size),
            oldest_age_s=round(self._clock() - oldest, 1) if oldest is not None else None,
            dropped_total=dropped,
            dead_lettered_total=dead,
            recovered_from_corruption=recovered,
            thinned_total=thinned,
            by_priority={name: int(by_priority.get(rank, 0)) for name, rank in PRIORITY_RANK.items()},
        )
