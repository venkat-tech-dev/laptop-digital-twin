# Backup and restore

Baseline procedure for the Docker deployment (PostgreSQL 16 + TimescaleDB). The tool is
`scripts/backup_restore.py`; it runs `pg_dump` and `pg_restore` inside the postgres container using
the container's own credentials.

## 1. What to back up

| Item | How | Notes |
|---|---|---|
| Database `ldt` | `backup_restore.py backup` | everything: organizations, members, devices, policies, audit, telemetry, alerts, remediations |
| Secrets | your secret manager / `SECRETS_DIR` | `JWT_SECRET`, `DATA_ENCRYPTION_KEY` (without it MFA seeds cannot be decrypted), remediation signing key, `POSTGRES_PASSWORD`. **Never store them next to the dumps** |
| `models/` volume | file copy | uploaded 3D model / photo (optional) |
| Agent | nothing | laptops keep their DPAPI-protected credential; they reconnect after a restore |

Redis holds only caches and live twin documents; it is rebuilt from the database and live telemetry.

## 2. Backup

```
python scripts/backup_restore.py backup --db ldt --out D:\ldt-backups
```

This writes `ldt-<UTC time>.dump` (custom format: compressed, supports selective restore) and a
`.sha256` file next to it. Schedule it (for example daily with Windows Task Scheduler) and copy the files
off the machine. Keep at least 7 daily and 4 weekly copies. Dumps contain telemetry and audit data: store
them encrypted (BitLocker volume, encrypted bucket) and restrict who can read them.

## 3. Restore (into a new database, then switch)

```
python scripts/backup_restore.py restore --file D:\ldt-backups\ldt-20261009T0200Z.dump --db ldt_restore
python scripts/backup_restore.py verify  --source ldt --db ldt_restore    # when the source still exists
```

* The restore refuses an existing database name. You cannot restore over the live database by
  accident.
* The checksum is verified before restoring.
* TimescaleDB's `timescaledb_pre_restore()` / `timescaledb_post_restore()` wrap `pg_restore`.
* `verify` compares row counts of the governance tables and recomputes the **audit hash chain** of the
  restored copy with the application's algorithm.

To go live on the restored copy: stop the backend, point `POSTGRES_DB` / `DATABASE_URL` to
`ldt_restore` (or rename the databases while the backend is stopped), start the backend (migrations run
and are no-ops), then check `/health/ready` and Organization → Audit → Verify integrity.

## 4. Tested

On 2026-10-09 against throwaway databases in the project's Postgres container (never the live `ldt`):

| Check | Result |
|---|---|
| Fresh install: migrations 0001 → 0012 on an empty database | OK |
| Upgrade 0011 → 0012 with legacy users, devices, credentials, alerts, notifications | OK: default organization; admin → owner, operator → IT operator, viewer → read only, employee → employee; only the earliest admin became platform administrator; devices ACTIVE / legacy; credentials expire in 90 days |
| Audit table append-only | UPDATE and DELETE refused by the trigger |
| Per-organization retention SQL | old closed alerts and delivered notifications deleted; open alerts and pending notifications kept |
| Downgrade 0012 → 0011, then upgrade again | OK |
| Backup → restore → verify (15 hash-chained audit events) | tables equal, audit chain valid, append-only trigger still active after restore |

Targets for this single-host deployment (not yet measured at production size): restore point objective
= backup interval (24 h with daily dumps); restore time objective ≈ minutes for gigabytes of data.
Measure restore time on a copy of the real database before relying on it.

## 5. Not covered

Point-in-time recovery (WAL archiving), streaming replicas and cross-region copies are not configured.
Add them with standard PostgreSQL tooling (pgBackRest, WAL-G) if the recovery point must be shorter than
the dump interval.

## 6. Disaster recovery plan (Phase 10)

**Objectives (proposed, single host):**

| Objective | Target | Basis |
|---|---|---|
| RPO, governance data (organizations, members, policies, audit, enrollment, alerts, remediation, diagnoses) | 24 h with daily dumps; **≤ 15 min** after enabling WAL archiving (recommended) | dump interval |
| RPO, raw telemetry | 24 h; partly re-sent by agents (each agent queues while the backend is unreachable, bounded by its queue limits) | agent queue |
| RTO | 1 h for a restore onto the existing host | restore of the 246 MB test database took seconds; **not measured at production size** |

**Backups:** daily `pg_dump -Fc` (+ SHA-256), kept 7 daily + 4 weekly, copied off-host. Store them
encrypted at rest (BitLocker volume or an encrypted bucket), readable only by the operator role. Every
read of a backup should be logged by the storage system.

**Secrets are never in the dumps** and must be backed up separately, encrypted (password manager or
KMS): `JWT_SECRET`, `DATA_ENCRYPTION_KEY` (MFA seeds are unreadable without it), the remediation signing
key, `POSTGRES_PASSWORD`. Losing `DATA_ENCRYPTION_KEY` means every user re-enrolls MFA. Losing the
signing key means every agent must be re-pinned (`docs/remediation.md`).

**What can and cannot be reconstructed:**

| Data | After loss |
|---|---|
| Live twin state, presence, in-memory anomaly/forecast state | rebuilt automatically from incoming telemetry within minutes |
| Device inventory | re-sent by each agent at its next start |
| Raw telemetry since the last backup | only the part still queued on agents; the rest is lost |
| Baselines and anomaly models | retrained from new telemetry (cold start: about 12 h until behavioral baselines are ready) |
| Organizations, members, roles, policies, identity providers, SCIM tokens | **only from backup** |
| Enrollment state and device credentials | from backup; devices enrolled after the backup must re-enroll with a new token |
| Audit trail, alerts, predictions, diagnoses, remediation history | **only from backup** (governance records) |

**Recovery order:** 1. PostgreSQL (restore into a new database, verify), 2. secrets, 3. Redis (empty
is fine), 4. backend (migrations run, no-op), 5. frontend, 6. confirm agents reconnect, 7. readiness,
audit-chain verification, SLO page.

**Drills performed (2026-10-09, isolated databases):** backup → restore → verify (row counts, audit
chain, trigger); migration upgrade, downgrade and re-upgrade; failover of the active backend
(`scripts/failover_drill.py`); database outage during ingestion (`scripts/chaos_drill.py`). **Not
done:** a restore of the production-size database on a second host, and multi-region recovery
(not deployed, not claimed).
