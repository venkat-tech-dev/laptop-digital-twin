# Scaling and availability

## 1. Deployment model (Phase 10)

**One active backend, optional hot standby.** A PostgreSQL session advisory lock (`LEADER_ELECTION`,
default on) elects the active instance. A second backend started against the same database waits as
standby: API and readiness answer 503 `STANDBY`, WebSockets close with 1013, and no background loop
runs. It takes over when the active instance's database session ends.

* Measured: standby takeover **5.25 s** after a hard kill of the active process; the old instance
  rejoins as standby (`scripts/failover_drill.py`, isolated database, 2026-10-09).
* A leader that loses its lock connection (database restart, network partition) shuts itself down
  rather than keep acting, so two actives cannot coexist. Docker's restart policy brings it back as a
  candidate.
* Not done: automatic placement on a second host. The standby protects against process crashes and
  enables near-zero-downtime upgrades on one host. Host failure still needs a second host with access
  to the same PostgreSQL.

Why not N active replicas: the audit (`docs/architecture/phase-10-current-state-audit.md`, finding
S1) found tenancy, policies, quotas, presence, alert engine, remediation dispatch and anomaly/forecast
state held in process memory. Active-active needs those shared (database or Redis) first. Measured
load (one process handles 100 agents at ≈ 60 % CPU) does not justify that work yet.

## 2. Components

| Component | Scaling trigger | Bottleneck | Shared state | Concurrency / idempotency | Failure behaviour | Scale-down |
|---|---|---|---|---|---|---|
| API (reads) | p95 of device-state reads > 250 ms (SLO) | CPU of the single process | in-memory twins (authoritative copy in PostgreSQL/Redis) | read-only | 503 + Retry-After when the DB is down | n/a (single active) |
| Telemetry ingestion | `ldt_ingest_inflight` at 32, shed ratio > 0 | CPU (validation, twin projection) | dedupe LRU + receipts table | at-least-once from the agent; batch-id dedupe (in-flight guard + LRU + durable receipts); samples `ON CONFLICT DO NOTHING` | 503 overloaded, 429 quota; agents keep the data and replay | — |
| Persistence | `ldt_persist_oldest_queued_age_seconds` > 60 | PostgreSQL insert rate | bounded queue (50k rows) | COPY + `ON CONFLICT`; sequence-numbered flush | backoff 2→60 s; drop-oldest when full (counted) | — |
| Digital twin projection | twin_projection latency | CPU | Redis twin documents (restore at start) | per-field timestamps reject stale overwrites | rebuilt from incoming telemetry | — |
| Anomaly detection | evaluation round > interval (10 s) | CPU (≈ 1 ms per device per round, Phase 4) | baselines/models in PostgreSQL | per device | supervisor restart; telemetry unaffected | — |
| Forecasting | tick time | CPU | predictions in PostgreSQL | per device | supervisor restart | — |
| Alerting + notifications | `ldt_notification_oldest_due_seconds` > 120 | provider latency, DB commits (~14 ms each on this disk) | alerts/notifications tables | idempotency keys; claim with `FOR UPDATE SKIP LOCKED`; stuck SENDING requeued | retries 4× with jitter, then FAILED | — |
| AI diagnosis | queue depth near max | local model latency, RAM | diagnoses table | concurrency 1, bounded queue | rules-only fallback; memory gate | — |
| Remediation coordination | in-flight count | approval workflow, agent polling | remediation table + audit chain (advisory lock) | **never retried blindly**; idempotency ledger on the agent; signed envelopes with nonce and expiry | circuit breaker per device/action; kill switches | — |
| WebSocket gateway | connections (500 per org quota) | per-socket send queues | Redis pub/sub between instances | fan-out only to visible devices | clients reconnect with backoff and resync | — |

## 3. Path to horizontal scale-out (when measurements require it)

1. Move tenancy, policies, identity providers and OIDC state to read-through caches with Redis
   invalidation (version counters per organization).
2. Move rate windows and quotas to Redis (atomic INCR with TTL); closes threat-model RR2.
3. Partition device-owned processing (twin, anomaly, forecast, presence) by device: consistent hashing
   over instances with ownership leases in PostgreSQL; ingest is routed to the owner (or any instance
   that forwards).
4. Keep singleton jobs (retention, snapshots, alert escalation, remediation dispatch) on the elected
   leader only.
5. Re-run `scripts/loadtest.py` at 500, 1,000 and 5,000 agents on the isolated stack before claiming
   any of these scales.

## 4. Capacity planning inputs (measured)

* ≈ 690k samples/device/day (95 MB uncompressed; 17.9× compression after 2 days). Steady state
  ≈ 340 MB per device for 30-day raw retention.
* Load test on the Phase 10 code (isolated, this laptop, `docs/loadtest-results-phase10.json`):
  100 devices without loss (end-to-end p95 106 ms, write queue peak 49 %); **500 devices overload
  the database insert path** (inserts 12.6 s avg, queue full). Agents ≥ 1.8 re-send evicted batches,
  so overload delays data rather than losing it, but this host is sized for about 100 devices.
* At 1,000 devices: ≈ 8,000 sample rows/s sustained insert; ≈ 340 GB storage. Needs a dedicated
  database host with fast storage, and a load test there, before onboarding such a fleet.
* Fleet Operations → Capacity projects device growth, alert volume and (platform) storage from
  observed daily history and shows `INSUFFICIENT_DATA` until 7 days exist.
