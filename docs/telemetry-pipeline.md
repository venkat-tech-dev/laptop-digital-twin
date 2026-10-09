# Telemetry pipeline (Phase 2)

Near-real-time path from the endpoint agent to the dashboard:

```
collect once → validate once → persist once → project state → distribute to consumers
```

There is one authoritative ingestion path (`backend/app/services/ingest_pipeline.py`). Every batch
goes through the same sequence: authenticate, rate limit, size and gzip limits, validate, dedupe,
apply, track, receipt, acknowledge. No consumer reads agent data any other way.

```
 Windows endpoint                       Backend (FastAPI)                               Consumers
┌───────────────────────┐   HTTPS    ┌───────────────────────────────────────────┐
│ collectors (lanes)    │  gzip bulk │ IngestBodyMiddleware: size / gzip limits  │
│   ↓ normalise (v1.1)  │ ─────────▶ │ require_agent: device token ↔ device_id   │
│ BatchAccumulator      │            │ TokenBucket per device → 429 Retry-After  │
│   (latest per series, │            │ IngestPipeline                            │
│    static suppression,│ ◀───────── │   schema version → schema → timestamps    │
│    events, priority)  │  ack:      │   → dedupe (LRU, warmed from receipts)    │
│   ↓ every 5 s, or now │  accepted/ │   → DigitalTwinService.update (in memory) │   WebSocket /ws/twin
│     for HIGH/CRITICAL │  duplicates│   → SamplePersister (bounded queue) ──┐   │   device:<id>
│ SQLite outbox         │  /rejected/│   → EventRecorder (bounded queue) ──┐ │   │   workspace:<id>
│   priority, limits,   │  last_seq  │   → SequenceTracker / Presence      │ │   │ ─▶ fleet
│   thinning            │            │   → ReceiptWriter (bounded queue) ─┐│ │   │
│   ↓ SyncManager       │  heartbeat │ EventBus → Redis pub/sub → every   ││ │   │   REST: /devices,
│ newest first, backlog │ ─────────▶ │   replica → ConnectionManager      ││ │   │   /devices/{id}/state,
│ by priority, backoff  │  (30 s)    │   (topic routing, bounded queues)  ▼▼ ▼   │   /telemetry/history,
└───────────────────────┘            │ PostgreSQL + TimescaleDB (hypertable,     │   /pipeline/stats
                                     │   5-min continuous aggregate, retention)  │
                                     │ Redis: hot state (per device), pub/sub    │
                                     └───────────────────────────────────────────┘
```

## Wire contract (schema 1.1)

Every payload carries `schema_version`. The backend accepts `1.0`, which is a Phase 1 agent that
sends no version, and `1.1`. Anything else is rejected per batch as
`unsupported_schema_version: <v> (supported: 1.0,1.1)`. The agent then pauses uploads for an hour
and keeps its queue, so nothing is lost while the backend is upgraded.

| Field | Meaning |
|---|---|
| `schema_version` | `"1.1"` |
| `device_id` | stable device identity (hash of hardware identifiers, `ldt-…`) |
| `agent_version` | e.g. `1.3.0`; the agent instance id is in the agent logs and health |
| `batch_id` | random 128-bit id, the **idempotency key** |
| `sequence` | per-installation counter, persisted in the outbox, survives restarts |
| `collected_at` | oldest measurement in the batch, so latency figures include the batching wait |
| `sent_at` | batch creation (device clock, UTC) |
| `priority` | `critical` / `high` / `normal` / `low`, the highest priority of the content |
| `replay` | `true` for backlog uploaded after an outage. History only; never overwrites live state |
| `samples[]` | current metrics: `metric, component, value, unit, timestamp, source, quality, availability, kind, reason, labels, category` |
| `processes` | top-process snapshot (live batches only update the live view) |
| `events[]` | device events: `event_id` (idempotent), `type, severity, priority, category, timestamp, source, message, data` |
| `device_health` | compliance posture HEALTHY/WARNING/CRITICAL/UNKNOWN, checks and reasons |
| `agent_health` | run mode, uptime, CPU/RAM, queue, sync and collector state, plus pipeline counters |

The four kinds of data stay separate:
- **Device metadata** is the inventory envelope, sent on start, hourly, and after a `409`.
- **Current metrics** are `samples`.
- **Events** are `events`.
- **Agent health** is `agent_health` plus the heartbeat.

Every sample and event also carries a `category`: `performance`, `hardware`, `os`, `applications`,
`security` or `agent`.

All timestamps are UTC ISO 8601. Device timestamps are kept as reported; the backend adds
`server_received_at`.

Clock drift is handled as follows:
- **Estimate:** the backend tracks `server_received_at − sent_at` as an EWMA per device. This
  includes transit time, so it is an upper bound.
- **Exposure:** drift beyond `CLOCK_DRIFT_WARN_S` (120 s) is listed in `/pipeline/stats`.
- **Rejection:** batches dated more than `INGEST_MAX_FUTURE_SKEW_S` (1 day) ahead are rejected
  (`timestamp_in_future`).
- **Heartbeat:** the response returns `server_time` and `clock_offset_s` to the agent.

### Priorities

| Priority | Examples | Effect |
|---|---|---|
| CRITICAL | `critical_temperature` (≥ `CRITICAL_TEMPERATURE_C`, default 95 °C, 5 °C hysteresis); any event with severity `critical` | immediate flush + upload attempt; kept longest in the outbox |
| HIGH | `app_crash`, `security_posture_changed`, `device_health_changed`, `internet_lost`, `network_disconnected`, `collector_failed` (after `COLLECTOR_FAILED_AFTER`=3 consecutive failures), `agent_stopped` | immediate flush + upload attempt |
| NORMAL | periodic metrics, other events | flushed every `MAX_BATCH_WAIT_MS` (5 s) |
| LOW | batches containing only agent health | thinned/evicted first |

### Batching

| Setting | Default | Meaning |
|---|---|---|
| `MAX_BATCH_WAIT_MS` (alias `PUBLISH_INTERVAL_MS`) | 5000 | a batch is cut at least this often (latest value per series) |
| `MAX_BATCH_SIZE` (alias `BATCH_SIZE`) | 50 | batches per upload request |
| `MAX_BATCH_BYTES` | 1,000,000 | JSON bytes per upload request (before gzip) |
| `STATIC_RESEND_S` | 600 | static values (capacities, versions, config) only when changed, plus a keyframe every 10 min and after every reconnect |
| `HEARTBEAT_INTERVAL_S` | 30 | liveness ping, independent of the backlog |
| `OFFLINE_FLUSH_INTERVAL_S` | 30 | while the backend is unreachable, one batch per 30 s (bounds disk use) |

## Delivery: offline-first sync

1. Every batch is written to the SQLite outbox **before** any network attempt. The outbox uses WAL
   journalling and zlib-compressed payloads, and `batch_id` is UNIQUE.
2. `SyncManager` uploads the newest batch first, so the live view is current right after a
   reconnect. The backlog follows by **priority, then age**, flagged `replay`, in bulk requests
   gzip-compressed on the wire.
3. Retry classification:

| Outcome | Classified as | Agent behaviour |
|---|---|---|
| timeout, connection refused/reset, DNS failure | retryable | backoff `min(60 s, 1 s·2ⁿ)` × 50–100 % jitter; nothing dropped |
| HTTP 5xx, 408, 425 | retryable | same |
| HTTP 429 / 503 with `Retry-After` | retryable | waits at least `Retry-After` (capped at 1 h) |
| HTTP 401 | re-authenticate | re-register once with the enrollment key (token rotated), then retry |
| HTTP 403 | retryable (configuration) | data kept; the operator must fix the identity |
| HTTP 409 unknown device | retryable | inventory re-announced, then retried |
| per-batch `invalid_schema`, `malformed_payload`, `timestamp_in_future` | **non-retryable** | dead-lettered at once (deleted, counted, logged); the rest of the request is unaffected |
| per-batch `unsupported_schema_version` | pause | uploads paused for 1 h, queue kept |
| whole request 4xx (other) | non-retryable | attempt counter; dead-letter after `SYNC_MAX_ATTEMPTS` |

Connectivity signals cut the backoff short: the network collector's `internet_restored` event, a
successful heartbeat, or a HIGH/CRITICAL event. Graceful shutdown makes a final flush and a 5 s
upload attempt.

### Idempotency

- **Agent:** `batch_id` is unique in the outbox, and the same id is resent until it is acknowledged.
- **Backend, in memory:** an LRU of 200,000 accepted batch ids makes a retry after a lost
  acknowledgement come back as `duplicate`.
- **Backend, durable:** `ingest_receipts` (batch id, device, sequence, received_at, counts) is
  written asynchronously and reloaded into the LRU at startup. A batch accepted before a backend
  restart is still recognised as a duplicate.
- **Samples:** the primary key `(time, metric_id)` with `ON CONFLICT DO NOTHING` stores each sample
  once.
- **Events:** `system_events.event_uid` is UNIQUE and `ON CONFLICT DO NOTHING`, so an agent event
  replayed after a restart is stored once.

### Acknowledgement

`POST /api/v1/ingest/telemetry/bulk` returns `200` with:

```json
{
  "accepted": 48, "duplicates": 1, "rejected": 1, "last_sequence": 4711,
  "server_received_at": "2026-10-07T09:20:11.204Z",
  "results": [
    {"batch_id": "9f…", "status": "accepted"},
    {"batch_id": "a1…", "status": "duplicate"},
    {"batch_id": "c3…", "status": "rejected", "detail": "invalid_schema: samples.0.value: …"}
  ]
}
```

The agent deletes `accepted` and `duplicate` rows, dead-letters `rejected` ones, and keeps
everything else.

### Sequence tracking

The backend classifies each accepted batch per device:

| Kind | Rule |
|---|---|
| `in_order` | `seq == last + 1` |
| `gap` | `seq > last + 1`; the missing numbers are recorded, up to 4,096 per device |
| `out_of_order` | `seq ≤ last`, normal for a replayed backlog; fills a recorded gap |
| `duplicate` | the batch id was already seen |
| `reset` | `seq` far below `last`, i.e. a reinstalled agent |

`missing` therefore converges to the number of batches the backend genuinely never received.
These are batches dropped by the agent's backpressure, or lost.

## Backpressure

**Agent outbox** (limits: `QUEUE_MAX_BATCHES` 20,000, `QUEUE_MAX_MB` 200, `QUEUE_MAX_AGE_H` 72),
applied in this order:

1. **Age:** NORMAL/LOW rows expire after 72 h; HIGH/CRITICAL rows after 144 h.
2. **Thin:** while over the count or byte limit, every other *old* NORMAL/LOW batch is deleted.
   The newest quarter is untouched. Consecutive 5-second samples are largely redundant, so the
   trend survives at half resolution instead of whole hours disappearing.
3. **Evict:** if still over the limit, the oldest rows go, LOW/NORMAL before HIGH before CRITICAL.
   **The newest row (latest state) is never deleted.**

Everything removed is counted (`queue_thinned_total`, `queue_dropped_total`), reported in agent
health and logged.

**Backend:**
- **Overload protection:** at most `INGEST_MAX_INFLIGHT` (32) telemetry requests per process are
  processed at once. Beyond that the ingest middleware answers `503` + `Retry-After` (2–5 s,
  jittered) *before reading the body*. When the persistence queue is above
  `INGEST_SHED_REPLAY_ABOVE` (80 %), backlog (replay) uploads are deferred with `503`, so live data
  keeps flowing while history catches up. Agents keep deferred data in their outbox: nothing is
  lost, it arrives later.
- **Request path:** per-device token bucket (`INGEST_RATE_PER_DEVICE_PER_MIN` 240, burst 60),
  answered with `429` + `Retry-After`. Body limit 5 MB compressed / 40 MB inflated.
- **Behind ingest:** bounded queues. The persister keeps the newest 50,000 samples, the event
  recorder 5,000 jobs, the receipt writer 50,000 receipts. All of them retry with backoff when the
  database is down and count their drops.
- **WebSocket:** a bounded queue per client (256 messages). A client that falls behind is
  disconnected, then reconnects and receives a fresh snapshot instead of an ever-growing backlog.

## Current state vs history

| Need | Served from | Cost |
|---|---|---|
| current state of a device | in-memory twin projection (`GET /devices/{id}/state`, `/twin`, WebSocket snapshot) | O(1), no database |
| hot state for other replicas / restart | Redis `twin:<device>` (written at most every 2 s per device) | |
| short charts (≤ 10 min) | Redis/in-memory recent buffer (`/telemetry/recent`) | |
| history | TimescaleDB hypertable `telemetry_samples` (raw, ≤ 1 row per series per 5 s) | indexed `(metric_id, time)` |
| long ranges (bucket ≥ 5 min) | continuous aggregate `telemetry_samples_5m` (avg/min/max/count, real-time) | |
| events | `system_events` (device events with `event_uid`, priority, category), `health_events`, `anomalies` | |

### Storage decision: PostgreSQL + TimescaleDB

The existing PostgreSQL + TimescaleDB stack is kept. Alternatives considered:
- **InfluxDB** or a **ClickHouse** column store: either would add a second database for devices,
  users, credentials and events, which already need relational integrity.
- **Plain PostgreSQL:** tables grow without chunk-wise retention or compression.

TimescaleDB provides several things in the database we already run:
- the hypertable is chunked by day;
- native compression after 2 days;
- `add_retention_policy`, so raw retention drops whole chunks instead of a `DELETE` scan;
- continuous aggregates for long ranges;
- full SQL joins with device and metric metadata.

On plain PostgreSQL (no extension), the same code works: history is aggregated on the fly, and
retention falls back to a periodic `DELETE`.

### Retention

| Data | Setting | Default | Mechanism |
|---|---|---|---|
| raw samples | `RETENTION_DAYS` | 30 d | TimescaleDB retention policy, re-applied from settings at startup |
| 5-min aggregates | `AGGREGATE_RETENTION_DAYS` | 365 d | retention policy on `telemetry_samples_5m` |
| device/system events | `EVENT_RETENTION_DAYS` | 365 d | hourly purge |
| ingest receipts | `RECEIPT_RETENTION_HOURS` | 168 h | hourly purge; longer than the agent's 72 h queue age, so any replay is still recognised |

## Distribution: WebSocket gateway

The full message list is in [api.md](api.md#websocket-wstwin).

- **Authentication:** `?token=` (API key or JWT), with the origin checked against `CORS_ORIGINS`.
  A disabled account is refused at connect time.
- **Subscriptions:** `device:<id>`, `workspace:<id>` and `fleet`. The topic must exist, and each
  connection has a limit. Routing is by `device_id`, and each message is serialised once per
  publish.
- **Multiple replicas:** each event is serialised once and delivered to the local sockets
  immediately. It is also published to Redis tagged with this replica's id. Other replicas
  broadcast it; the origin replica skips its own echo instead of decoding it again. The channel is
  namespaced by Redis db (`ldt:events:db<n>`), because pub/sub ignores logical databases.
- **Lifecycle:** connect, `connection_status`, subscribe, snapshot per device, then live deltas.
  The server heartbeats every 5 s; the client pings every 10 s and gets a `pong` with
  `server_time`. Clients silent for more than 45 s are closed. Slow consumers are dropped. All
  clients are closed with `1001` on shutdown.
- **Browser reconnect:** exponential backoff with full jitter (0.5 to 15 s). The client
  re-authenticates and re-subscribes on every `connection_status`, so the server sends fresh
  snapshots.

The frontend makes no telemetry polling requests. It loads initial state over REST, then follows
the WebSocket subscription. Only the administrative panels (agent health, pipeline stats) refresh
every 15 s.

## Presence

Presence is computed from the agent's last contact: a heartbeat every 30 s, or any accepted batch.

| State | Rule (configurable) |
|---|---|
| ONLINE | last contact ≤ `PRESENCE_STALE_AFTER_S` (90 s) |
| STALE | ≤ `PRESENCE_OFFLINE_AFTER_S` (300 s) |
| OFFLINE | older |
| UNKNOWN | device known (registered/restored) but never contacted since |

Transitions are published as `device_presence_changed`. Presence differs from the twin's
*telemetry* status. An agent whose collectors fail still heartbeats, and shows as ONLINE with stale
data. An agent replaying a backlog is ONLINE even though its newest samples are old.

## Latency measurement (near real time)

| Stage | Measured where | How |
|---|---|---|
| `collection_to_server_ms` | backend | `server_received_at − collected_at` (oldest sample; device clock; live batches only) |
| `server_processing_ms` | backend | validate → dedupe → twin update → queue hand-off (wall time) |
| `ws_queue_ms` | backend | time a message waits in a client's send queue |
| `websocket_delivery_ms` | browser | `receive − published_at`, corrected by the ping/pong clock offset; reported back on the next ping |
| `end_to_end_latency_ms` | browser | `receive − collected_at` (offset-corrected) |

All stages are exported as the Prometheus histogram `ldt_pipeline_latency_ms{stage}`, and as rolling
p50/p95/p99/max in `GET /api/v1/pipeline/stats` and Settings → Telemetry pipeline. The batching
interval (5 s) dominates end-to-end latency by design. The UI therefore says **near real time**,
never "real time".

## Observability

| Layer | Signals |
|---|---|
| agent | `health.json` + `agent_health` in every batch + heartbeat. Per-collector success/failure/duration, queue depth/bytes/by priority, thinned/dropped/dead-lettered, sync failures, paused reason, API latency, batches created/uploaded, events generated, last sequence and server-acknowledged sequence, own CPU/RSS |
| backend | `/metrics`: `ldt_ingest_batches_total{result}`, `ldt_ingest_rejected_total{reason}`, `ldt_ingest_rate_limited_total`, `ldt_ingest_bytes_total{route,kind=wire\|decoded}`, `ldt_sequence_observed_total{kind}`, `ldt_devices_by_presence{presence}`, `ldt_pipeline_latency_ms{stage}`, `ldt_persist_queue_depth`, `ldt_ingest_receipts_pending`, `ldt_db_operation_seconds{operation}`, `ldt_websocket_connections`, `ldt_websocket_subscriptions`, `ldt_websocket_messages_total`, `ldt_websocket_dropped_total`, plus HTTP latency per route |
| end to end | browser-reported delivery/end-to-end latency, `/pipeline/stats` |

## Security and privacy

- **Transport:** HTTPS/TLS. Plain HTTP is accepted only for a loopback backend while
  `AGENT_ALLOW_INSECURE_LOCALHOST=true`. Certificates are always verified (`AGENT_CA_BUNDLE` for
  private PKI).
- **Identity:** each agent exchanges the enrollment key once for its own random token. The backend
  stores only the SHA-256 hash; the agent stores the token DPAPI-encrypted. A token authorises only
  its own `device_id` (`403`). Ingest with the shared enrollment key is off by default
  (`ALLOW_ENROLLMENT_KEY_INGEST=false`). Tokens can be revoked per device.
- **Untrusted input:**
  - every field is length- and pattern-bounded;
  - `extra="forbid"` on samples;
  - categories and priorities are enumerations;
  - lists have maximum sizes;
  - compressed and inflated body limits;
  - timestamps are sanity-checked;
  - browser latency reports are clamped and capped;
  - validation errors never echo submitted values.
- **Rate limits:** per device on agent endpoints, per IP elsewhere, per client on login.
- **Secrets:** none in code (keys from env/`.env`, the token in DPAPI). Logs redact
  token/secret/password/authorization/key fields; tokens are never logged.

**What is transmitted** is exactly the Phase 1 inventory ([endpoint-agent.md](endpoint-agent.md#what-is-collected-and-what-leaves-the-laptop)).
Phase 2 adds only:
- pipeline metadata: schema version, priority, category, counters;
- the heartbeat: version, run mode, last sequence, queue depth and age, sync state, API latency,
  number of failing collectors.

**Never transmitted:** passwords, keystrokes, screenshots, clipboard contents, personal file
contents, browser history, private messages.

## Compression (measured)

| Payload | JSON | gzip | ratio |
|---|---|---|---|
| one real batch from this laptop (169 samples, 32 processes) | 63,341 B | 6,276 B | 10.1× |
| same without processes | 50,574 B | 4,223 B | 12.0× |
| live: first 28 bulk requests after deploying v1.3.0 (includes backlog) | 921,618 B | 87,567 B | 10.5× |

gzip level 6 costs about 1 ms per batch on the agent. Bodies under 1 KB are sent uncompressed. A
backend that answers `415` gets plain bodies from then on.

## Measured performance

All figures were measured on this laptop:
- **Machine:** i5-1335U, 10 cores, 16 GB, Windows 11.
- **Backend:** one uvicorn process, no uvloop.
- **Infrastructure:** PostgreSQL/TimescaleDB and Redis in Docker Desktop.
- **Load generator:** on the same machine.
- **Payload:** a real captured batch, 169 samples and 32 processes; 62.7 KB JSON, 6.2 KB gzip.
- **Cadence:** each synthetic agent sends one batch every 5 s (the agent default), plus a heartbeat
  every 30 s.

The raw data is in `docs/loadtest-results.json`.

| Devices | Batches/s accepted | Samples/s | HTTP p50 / p95 | End-to-end p50 / p95 (collected → browser) | WS delivery p50 / p95 | Backend CPU avg (1 core = 100 %) | RSS max | Persist queue max | Samples dropped | Deferred (503) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 0.19 | 32 | 43 / 75 ms | 38 / 54 ms | 10 / 17 ms | 2 % | 103 MB | 116 | 0 | 0 |
| 10 | 1.85 | 313 | 68 / 143 ms | 57 / 91 ms | 14 / 22 ms | 12 % | 112 MB | 812 | 0 | 0 |
| 50 | 9.15 | 1,546 | 119 ms / 2.2 s | 105 / 780 ms | 18 / 608 ms | 52 % | 156 MB | 8,932 | 0 | 6 |
| 100 | 18.1 | 3,052 | 0.77 / 1.7 s | 0.78 / 1.2 s | 49 / 265 ms | 95 % | 222 MB | 42,340 | 0 | 27 |
| 500 | 9.3 (overloaded) | 1,578 | 11.5 / 40.8 s | 15.5 s | 2.4 s | 88 % | 350 MB | 50,000 | 39,784 | 457 |

**Capacity:** one backend process handles about **18 full-size batches/s**, roughly 90 agents
at the 5 s cadence, with sub-second median end-to-end latency. 500 agents exceed one process:
- most of the excess is deferred (503, kept in the agents' queues);
- the server still saturates, latency grows to seconds, and the bounded persistence queue sheds the
  oldest history samples.

Live state, acknowledgements and events are unaffected. Fleets of that size need several backend
replicas with device-partitioned routing (see Limitations).

Optimisations made during load testing, with the before → after figures at 100 devices:

| Change | Effect |
|---|---|
| sample persistence via binary `COPY` to a staging table + one `INSERT … ON CONFLICT` | samples dropped at 100 devices 46,396 → 0; insert CPU no longer on the request path |
| local-first WebSocket fan-out, Redis echo skipped by origin id | removes one JSON decode per event per replica (~16 events per batch) |
| Prometheus per-reading label lookups batched; per-metric gauges only for the primary device | ~8 % of ingest CPU |
| Redis hot-state snapshot per device every `HOT_STATE_INTERVAL_S` (10 s) instead of 2 s | full-twin serialisation 5× less often |
| concurrency gate in the ingest middleware (503 + Retry-After) | overload becomes deferral instead of unbounded queueing |
| end-to-end p50 at 100 devices | 2.48 s → 0.78 s |

**Large backlog:** 2,000 queued batches (12 MB in SQLite, 122.5 MB JSON, 11.5 MB on the wire)
drained in 46.7 s, 42.8 batches/s. Bulk replay is more than twice as efficient as live traffic.
There were no duplicates and no rejections.

## Configuration summary (new in Phase 2)

Backend:
- **Rate and size limits:** `INGEST_RATE_PER_DEVICE_PER_MIN`, `INGEST_RATE_BURST`,
  `INGEST_MAX_BODY_BYTES`, `INGEST_MAX_DECOMPRESSED_BYTES`.
- **Timestamps:** `INGEST_MAX_FUTURE_SKEW_S`, `CLOCK_DRIFT_WARN_S`.
- **Agent identity:** `ALLOW_ENROLLMENT_KEY_INGEST`.
- **Presence:** `PRESENCE_STALE_AFTER_S`, `PRESENCE_OFFLINE_AFTER_S`.
- **Routing:** `PRIMARY_DEVICE_ID`, `WS_MAX_SUBSCRIPTIONS`.
- **Capacity:** `INGEST_MAX_INFLIGHT`, `INGEST_SHED_REPLAY_ABOVE`, `HOT_STATE_INTERVAL_S`,
  `DEVICE_TOKEN_CACHE_S` (verified device tokens are cached, so ingest needs no database round trip
  and survives a database outage).
- **Retention:** `AGGREGATE_RETENTION_DAYS`, `EVENT_RETENTION_DAYS`, `RECEIPT_RETENTION_HOURS`.

Agent:
- **Batching:** `MAX_BATCH_WAIT_MS`, `MAX_BATCH_SIZE`, `MAX_BATCH_BYTES`, `STATIC_RESEND_S`.
- **Liveness:** `HEARTBEAT_INTERVAL_S`.
- **Event thresholds:** `CRITICAL_TEMPERATURE_C`, `COLLECTOR_FAILED_AFTER`.

## Testing strategy

| Level | Where | What |
|---|---|---|
| unit | `agent/tests/test_contract_v11.py`, `test_store_sync.py`, `test_client.py`; `backend/tests/unit/test_pipeline.py` | contract, priorities, static suppression, outbox priority/thinning/eviction/migration, retry classification (500, 429 + Retry-After, invalid, unsupported schema, 409), gzip + 415 fallback, sequence classification, presence thresholds, event idempotency |
| integration | `backend/tests/unit/test_pipeline.py` (TestClient), `backend/tests/integration/*` (real PostgreSQL/Redis), `agent/tests/test_runner_integration.py` (real Windows collectors) | token-only ingest, multi-device isolation, WebSocket topic routing and legacy clients, size limits, rate limits, restart recovery |
| failure | scripted against an isolated backend (see the Phase 2 report) | backend down/restart, database down, large backlog drain, duplicate after backend restart |
| load | `scripts/loadtest.py` | 1/10/50/100/500 synthetic agents, real-sized payloads |

### Load test

Run it against a **separate** backend and database. Synthetic devices must not appear in a real
fleet. For example:

```powershell
docker compose exec postgres psql -U ldt -d ldt -c "CREATE DATABASE ldt_load"
# migrate with DATABASE_URL pointing at ldt_load, then start a second backend on port 8020 with
# DATABASE_URL=…/ldt_load REDIS_URL=redis://127.0.0.1:16379/1 AUTH_MODE=none (loopback only)
python scripts/loadtest.py --base http://127.0.0.1:8020 --key <test enrollment key> `
  --levels 1,10,50,100,500 --duration 60 --backend-pid <pid> --template <captured batch.json>
```
