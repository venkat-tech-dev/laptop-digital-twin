# Phase 10 audit: the platform after Phases 1–9 (2026-10-09)

Scope: the whole repository (backend, agent, frontend, infrastructure, tests, docs) and the running
deployment on the development laptop. Evidence is cited as `file:line` or as a measurement taken on
2026-10-09. "Measured" means observed on this system; "Inferred" means read from code.

## 1. Architecture as built

```
Windows agent (Python service, 1 per laptop)
  └─ HTTPS: inventory, gzip bulk telemetry (every 5 s), heartbeat (30 s), action pull
Backend: ONE FastAPI process (uvicorn) — api + ingest + all background loops
  ├─ in-memory: twins, twin documents, presence, tenancy, policies, alert engine, remediation items,
  │             anomaly/forecast state, rate limits, dedupe LRU, WebSocket registry
  ├─ PostgreSQL 16 + TimescaleDB 2.17 (durable store; hypertable telemetry_samples)
  └─ Redis 7 (no persistence): WebSocket fan-out channel, hot twin copies, chart backfill stream
Frontend: React SPA behind nginx (one WebSocket per tab)
```

There is no message broker. "Pipeline" stages are in-process function calls plus bounded in-memory
queues that are flushed to PostgreSQL in the background.

## 2. Measurements (2026-10-09, live deployment, 1 enrolled laptop)

| Item | Value |
|---|---|
| Database size | 246 MB total; `telemetry_samples` 202 MB |
| Telemetry volume, one laptop | 620,071 samples (2026-10-07), 694,330 (2026-10-08): **≈ 690k samples/day ≈ 95 MB/day uncompressed** |
| Compression | chunk of 2026-10-06: 36 MB → 2 MB (**17.9×**); policy compresses after 2 days |
| Retention jobs | raw samples 30 days, 5-min aggregate 365 days, receipts 168 h |
| Ingest receipts | 68,962 rows in ~2 days (≈ 0.4 batches/s) |
| Backend process | 130–155 MiB RSS, < 2 % CPU (one device) |
| Postgres | 413 MiB, 6 connections (pool 5 + 5 overflow) |
| Single-process capacity (Phase 3 load test) | 100 simulated agents at ≈ 60 % CPU, p50 end-to-end 51 ms, no loss; 500 agents overload one process |
| Phase 9 tenancy overhead | not measurable at p50; flat from 1 to 200 organizations (docs/tenancy-bench-results.json) |

**Capacity model (inferred from the above):** per device ≈ 2 days × 95 MB hot + 28 days × 5.3 MB
compressed ≈ **340 MB steady state** for 30-day raw retention (+ aggregates, small). 1,000 devices ≈
340 GB and ≈ 8,000 sample-rows/s sustained insert (690k/day × 1,000 ÷ 86,400). The ingest side was
measured to 100 agents per process. Database insert capacity at 1,000+ devices has **not** been
measured.

## 3. Findings

Severity: **P0** data loss, security or silent failure in the current single-node deployment.
**P1** limits availability or scale, or prevents safe operation. **P2** operational maturity.
**P3** hygiene. "Fixed" entries were changed in Phase 10 and have tests (section 5).

### Reliability and data integrity

| # | Sev | Finding | Evidence | Status |
|---|---|---|---|---|
| F1 | P0 | Persister `flush()` popped `len(batch)` rows after an awaited write; if the bounded queue overflowed meanwhile, **newer unwritten rows were popped and lost** | services/persistence.py (flush) | **Fixed**: sequence-numbered rows, pop only what was written; regression test |
| F2 | P0 | No background loop was supervised: a loop that raised stayed dead **silently** until restart (incl. the WS heartbeat loop, where one bad client stopped heartbeats for all) | core/container.py start(); heartbeat loop | **Fixed**: `core/supervisor.py` restart with bounded backoff + jitter, crash-loop detection, metrics, `/health/ready`; heartbeat iterations isolated |
| F3 | P0 | Parent services waited only for stop; a dead **child** loop (notification delivery, alert tick, diagnosis worker, remediation events, anomaly trainer) went unnoticed | services/alerting.py, diagnosis.py, remediation.py, intelligence.py run() | **Fixed**: `watch()` / done-checks raise, supervisor restarts the service, children cancelled in `finally` |
| F4 | P0 | A crash mid-delivery left notifications in `SENDING` forever (claim sets SENDING; nothing reclaims) | repositories/alerting.py claim_due | **Fixed**: requeue after 5 min (at-least-once), backlog + oldest-due SLIs |
| F5 | P1 | Concurrent copies of one batch (agent retry racing the first request) were both applied to twin, anomaly state and event bus | services/ingest_pipeline.py apply() | **Fixed**: in-flight batch-id guard |
| F6 | P1 | Event-record jobs dropped on first failure, no metric | services/persistence.py EventRecorder | **Fixed**: 3 bounded attempts (idempotent writes), drop counted by reason |
| F7 | P1 | Unbounded growth: audit buffer for WARNING+ events while DB down; diagnosis cooldown map; remediation execution index (and an hourly prune that a tick could skip); deletion-job map; OIDC pending state (unauthenticated); session cache | see services/* | **Fixed**: hard caps / pruning (OIDC: 10k pending → 503) |
| F8 | P1 | No DB statement timeout; pool size not configurable | infrastructure/database/engine.py | **Fixed**: `DB_STATEMENT_TIMEOUT_MS` (60 s; maintenance deletes lift it locally), pool settings |
| F9 | P1 | Redis "connected" flag stayed false after an outage until a readiness probe pinged; one bad pub/sub message tore down the subscription | infrastructure/redis/client.py | **Fixed** |
| F10 | P1 | No durable ingest queue: up to 50k accepted rows (persister), 5k event jobs, receipts and ≤ 1 s of audit live only in memory and are lost on a crash **after** the agent got 202 | services/persistence.py, ingest_pipeline.py | **Fixed for agents ≥ 1.8**: durable confirmation (receipt only after the rows are written; heartbeat classifies durable / pending / unknown; the agent keeps and re-sends). Chaos drill: 10 batches lost at a crash, all recovered (61/61). Event records and ≤ 1 s of audit remain in-memory windows |
| F11 | P2 | Out-of-order batches are applied as received (sequence tracker is diagnostic only); twin uses per-field timestamps (Phase 3) | services/sequences.py | Documented in `consistency-model.md` |
| F12 | P0 | **Found by the chaos drill:** the persister's downsampler dropped any sample *older* than the newest kept one (negative gap < interval), silently losing replayed history | services/persistence.py | **Fixed** + regression test |
| F13 | P1 | **Found by the chaos drill:** readiness hung while the database was paused (no timeout on the ping) | api/probes.py | **Fixed**: bounded ping (2 s), at most one in flight |
| F14 | P1 | Two leaks of aggregate statistics across organizations (`/prediction-accuracy` without device_id; `/diagnosis-config/status`) | api/v1/predictions.py, diagnosis.py | **Fixed** + isolation regression test |
| F15 | P2 | HTTP latency metric labelled almost every request `unmatched` (included routers) | core/middleware.py | **Fixed**: full route templates |

### Availability and scaling

| # | Sev | Finding | Evidence | Status |
|---|---|---|---|---|
| S1 | P1 | **Single-replica design.** Tenancy, policies, identity providers, OIDC state, presence, alert engine, remediation items, anomaly/forecast state, rate limits and quotas live in process memory, loaded once; a second replica would diverge (e.g. a device revoked on A still ingests on B) and every background loop would run twice (duplicate escalations, conflicting remediation dispatch) | agent audit (services/tenancy.py:98-137, remediation.py:102-133, alerting.py:345-363, …) | **Decision (section 4)**: one active instance + standby, enforced with a PostgreSQL leader lock |
| S2 | P2 | `/health/live` checks nothing; readiness ignored background state | api/probes.py | **Fixed** (readiness); liveness stays a pure process check by design |
| S3 | P2 | `GET /devices` builds every row then filters/sorts in Python (O(N) per page); `/org/devices` unpaginated | api/v1/devices.py:95-133, org.py:382 | Open (fine at measured scale; P2 for 10k devices) |

### Delivery, security, operations

| # | Sev | Finding | Status |
|---|---|---|---|
| D1 | P1 | No CI pipeline; git repository has **zero commits** | CI workflow added (section 5); committing is the owner's decision |
| D2 | P1 | Python dependencies unpinned (`>=`); images not reproducible | lock files generated from the tested environments |
| D3 | P2 | No staging/prod configuration, no rollback runbook; migrations run automatically at container start | runbooks + release procedure |
| D4 | P2 | Agent version hard-coded (1.7.0) vs `pyproject` 1.0.0; no packaging; no update channel (correct: none allowed) | version aligned; staged-rollout design |
| D5 | P2 | `/metrics` unauthenticated (loopback only) | documented; bind/scrape restriction in checklist |
| D6 | P2 | Missing SLIs: notification backlog, background loop health, persister lag, recorder drops, inflight, Redis state | **Fixed** (metrics added) |
| D7 | P2 | No runbooks; no fleet-level analytics beyond counts; no capacity forecasting; anomaly false-positive rate only as a counter | Phase 10 work items |
| D8 | P3 | Frontend: all pages eager-loaded; polling continues in hidden tabs; `fleetStore` never pruned | Phase 10 work items |
| D9 | P3 | Only 5 tests use a real database | integration/chaos tests on an isolated database |

## 4. Architectural decisions

1. **No Kafka, Kubernetes, second database or microservices.** Measured load (one laptop; 100-agent
   capacity per process) does not justify them. TimescaleDB with compression covers the storage model
   to at least hundreds of devices on one host. Re-evaluate at > 500 devices, using the load test.
2. **One active backend + hot standby (active/passive).** Making every in-memory structure shared
   (Redis/DB-backed tenancy, alert engine, remediation dispatch, quotas) is a large change with
   correctness risk. The platform's measured scale does not need it. Instead, a PostgreSQL session
   advisory lock elects one active instance. A second instance waits as standby (readiness 503,
   API 503) and takes over when the active one's database session ends. This prevents the
   split-brain failures in S1, adds failover, and keeps behaviour identical. Horizontal scale-out of
   ingest is the documented next step (section 7 of `docs/operations/scaling.md`).
3. **At-least-once where duplicates are harmless, never for remediation.** Notifications and event
   records are retried (idempotency keys / unique ids). Remediation dispatch keeps the Phase 8
   idempotency ledger and signed envelopes and is never blindly retried.

## 5. Implementation plan and status

| Step | Content | Status |
|---|---|---|
| 1 | Audit + baseline measurements | done (this document) |
| 2 | P0/P1 reliability fixes F1–F9 + tests (`tests/unit/test_reliability_phase10.py`, persister regression test) | done |
| 3 | Leader lock / standby (S1) | done: failover drill 5.25 s / 5.98 s |
| 4 | SLOs, SLI endpoint, alert rules (`docs/operations/slo.md`) | done (targets proposed; rules not checked with promtool) |
| 5 | Fleet intelligence: health score, cross-device correlation, recurring issues, capacity forecast | done |
| 6 | Model governance: anomaly FP rate, prediction accuracy, diagnosis ops status | done (by-cohort accuracy needs a fleet) |
| 7 | Fleet operations dashboard (executive / IT views) | done (not verified visually in a browser) |
| 8 | CI workflow, lock files, release + rollback procedure, agent rollout design | done (CI never executed) |
| 9 | Runbooks, chaos tests (isolated DB), load test re-run, readiness assessment | done |
| 10 | Durable confirmation (F10) | done, agent 1.8 |
