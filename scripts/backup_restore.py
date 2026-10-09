# ruff: noqa: S603, S607, S608  (operator script: fixed docker/psql argv; database names pass the _db() allowlist, table names are constants, SQL is passed to psql as $0, never through the shell)
"""Backup / restore baseline for the Docker PostgreSQL + TimescaleDB database (Phase 9).

    python scripts/backup_restore.py backup  --db ldt --out backups/
    python scripts/backup_restore.py restore --file backups/ldt-20261009T1200Z.dump --db ldt_restore
    python scripts/backup_restore.py verify  --source ldt --db ldt_restore

* backup: ``pg_dump -Fc`` inside the postgres container (custom format: compressed, selective restore),
  streamed to a local file; a SHA-256 is written next to it.
* restore: into a NEW database only (refuses an existing one), with TimescaleDB's
  ``timescaledb_pre_restore()`` / ``timescaledb_post_restore()`` around ``pg_restore``.
* verify: row counts of every governance table equal the source, and the audit hash chain of the restored
  copy verifies (the restored audit trail is the same, untampered trail).

Credentials come from the container's own environment (POSTGRES_USER); nothing secret is passed on the
command line or written to the dump's file name. Restoring over the live database is deliberately not
supported: restore next to it, verify, then switch DATABASE_URL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

CONTAINER = "laptop-digital-twin-postgres-1"
NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TABLES = (
    "organizations",
    "organization_members",
    "org_units",
    "device_groups",
    "device_group_members",
    "device_registry",
    "enrollment_tokens",
    "policies",
    "audit_events",
    "users",
    "devices",
    "device_credentials",
    "alerts",
    "notifications",
    "remediations",
    "remediation_audit",
    "diagnoses",
)


def _db(name: str) -> str:
    if not NAME.match(name):
        raise SystemExit(f"invalid database name: {name!r}")
    return name


def psql(db: str, sql: str) -> str:
    r = subprocess.run(
        [
            "docker",
            "exec",
            CONTAINER,
            "sh",
            "-c",
            f'psql -U "$POSTGRES_USER" -d {db} -v ON_ERROR_STOP=1 -tAc "$0"',
            sql,
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode:
        raise SystemExit(r.stderr.strip()[-1500:])
    return r.stdout.strip()


def backup(db: str, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{db}-{datetime.now(UTC):%Y%m%dT%H%MZ}.dump"
    with path.open("wb") as f:
        r = subprocess.run(
            [
                "docker",
                "exec",
                CONTAINER,
                "sh",
                "-c",
                f'pg_dump -U "$POSTGRES_USER" -Fc {db}',
            ],
            stdout=f,
            stderr=subprocess.PIPE,
        )
    if r.returncode:
        path.unlink(missing_ok=True)
        raise SystemExit(r.stderr.decode(errors="replace")[-1500:])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(".sha256").write_text(f"{digest}  {path.name}\n")
    print(
        json.dumps(
            {"backup": str(path), "bytes": path.stat().st_size, "sha256": digest}
        )
    )
    return path


def restore(file: Path, db: str) -> None:
    expected = file.with_suffix(".sha256")
    if (
        expected.exists()
        and expected.read_text().split()[0]
        != hashlib.sha256(file.read_bytes()).hexdigest()
    ):
        raise SystemExit("checksum mismatch: the dump file changed since the backup")
    if psql("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{db}'"):
        raise SystemExit(f"database {db} exists; restore only into a new database")
    psql("postgres", f"CREATE DATABASE {db}")
    psql(db, "CREATE EXTENSION IF NOT EXISTS timescaledb")
    psql(db, "SELECT timescaledb_pre_restore()")
    with file.open("rb") as f:
        r = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                CONTAINER,
                "sh",
                "-c",
                f'pg_restore -U "$POSTGRES_USER" -d {db} --no-owner',
            ],
            stdin=f,
            stderr=subprocess.PIPE,
        )
    psql(db, "SELECT timescaledb_post_restore()")
    errors = r.stderr.decode(errors="replace")
    # pg_restore reports harmless notices about the timescaledb extension objects; anything else fails
    real = [
        ln
        for ln in errors.splitlines()
        if "error" in ln.lower()
        and "timescaledb" not in ln.lower()
        and "already exists" not in ln
    ]
    if real:
        raise SystemExit("\n".join(real[-20:]))
    print(json.dumps({"restored": str(file), "database": db}))


def counts(db: str) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for t in TABLES:
        exists = psql(db, f"SELECT to_regclass('public.{t}') IS NOT NULL")
        out[t] = int(psql(db, f"SELECT count(*) FROM {t}")) if exists == "t" else None
    return out


def verify(source: str, db: str) -> bool:
    a, b = counts(source), counts(db)
    diff = {t: (a[t], b[t]) for t in TABLES if a[t] != b[t]}
    chain = verify_chain(db)
    ok = not diff and chain["ok"]
    print(
        json.dumps(
            {
                "tables_equal": not diff,
                "differences": diff,
                "audit_chain": chain,
                "ok": ok,
            }
        )
    )
    return ok


def verify_chain(db: str) -> dict[str, object]:
    """Recompute the audit hash chain of the restored copy with the application's own algorithm."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app.domain.governance.audit import GENESIS, AuditEvent, chain

    rows = psql(
        db, "SELECT coalesce(json_agg(a ORDER BY a.id), '[]') FROM audit_events a"
    )
    prev, n = GENESIS, 0
    for r in json.loads(rows or "[]"):
        e = AuditEvent(
            r["event_id"],
            datetime.fromisoformat(r["at"]),
            r["org_id"],
            r["actor_id"],
            r["actor_type"],
            r["action"],
            r["category"],
            r["resource_type"],
            r["resource_id"],
            r["result"],
            r["reason"],
            r["severity"],
            r["source"],
            r["request_id"],
            r["ip"],
            r["metadata"] or {},
        )
        n += 1
        if r["prev_hash"] != prev or chain(prev, e) != r["hash"]:
            return {"ok": False, "rows": n, "first_bad": n}
        prev = r["hash"]
    return {"ok": True, "rows": n}


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--db", default="ldt")
    b.add_argument("--out", type=Path, default=Path("backups"))
    r = sub.add_parser("restore")
    r.add_argument("--file", type=Path, required=True)
    r.add_argument("--db", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--source", required=True)
    v.add_argument("--db", required=True)
    a = ap.parse_args()
    if a.cmd == "backup":
        backup(_db(a.db), a.out)
    elif a.cmd == "restore":
        restore(a.file, _db(a.db))
    elif not verify(_db(a.source), _db(a.db)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
