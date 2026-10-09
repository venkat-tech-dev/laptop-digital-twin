# Event pipeline guarantees and Digital Twin consistency model

## Delivery guarantees (end to end)

| Hop | Guarantee | Mechanism |
|---|---|---|
| Agent → backend | **at-least-once** | the agent keeps every batch in a local SQLite queue until it gets 2xx; retries with backoff and jitter; honours 429/503 `Retry-After` |
| Duplicate batches | **deduplicated** | in-flight batch-id guard (concurrent copies, Phase 10), 200k-entry LRU, durable `ingest_receipts` (168 h) that warm the LRU after a restart |
| Samples → PostgreSQL | **idempotent writes; crash-safe with agents ≥ 1.8** | COPY + `ON CONFLICT DO NOTHING`. Accepted samples wait in a bounded in-memory queue (≤ 50k rows, flushed every 2 s). **Durable confirmation (Phase 10):** a batch's receipt is written only after its rows are durable; agents ≥ 1.8 keep accepted batches until the heartbeat confirms them, and re-send those the backend reports *unknown* (crash, or rows evicted by an overflowing queue, which are never confirmed and are forgotten by the deduplicator). Agents < 1.8 delete on 202 and keep the old loss window (≤ the in-memory queue). Measured: chaos drill phase D, 10 batches lost at a crash, all 10 re-sent and persisted (61/61) |
| Event records (health/system events) | at-least-once, idempotent | unique `event_uid`; 3 bounded attempts, then counted drop |
| Alerts → notifications | **at-least-once** | idempotency keys; claim with `SKIP LOCKED`; a delivery interrupted by a crash is requeued after 5 min (possible duplicate, never silent loss) |
| Remediation | **at-most-once execution per approved action** | signed envelope with nonce and expiry, agent-side idempotency ledger; no automatic re-send of an executed action |

**Exactly-once end to end is not claimed.** Duplicates are absorbed by idempotent writes and
deduplication. With agents ≥ 1.8 the remaining loss cases are the agent's own bounded queue
(age/size limits, counted on the agent) and data a device never sends; with older agents, the
in-memory write queue at a backend crash or overflow (counted in `ldt_persist_dropped_total`).

**Poison messages:** invalid batches are rejected at validation (422, counted by reason). The agent
dead-letters a batch that can never succeed: it is deleted from its queue, counted
(`queue_dropped_total` in the agent health file) and logged, so one bad batch never blocks the rest.
The batch content is not kept, by design (no telemetry is kept beyond its purpose). A malformed message on the Redis channel is skipped
and logged without stopping the subscription. There is no server-side dead-letter queue: every
accepted batch has been validated, and the remaining failures are infrastructure failures, which are
retried.

**Traceability:** every batch carries `batch_id`, `device_id`, `sequence`, `collected_at`, `sent_at`.
The backend stores `received_at` in the receipt. Organization comes from the device registry. API
requests carry `X-Request-ID` (in logs, error bodies and audit events). Remediation carries
`correlation_id` and `execution_id` through the signed envelope to the agent and back.

## Ordering and sequence gaps

* Each device numbers its batches. The backend classifies each one as first, in-order, gap,
  out-of-order or reset (`ldt_sequence_observed_total{kind}`). Gaps are diagnostic: the agent replays
  its own queue, so a gap usually closes when the replay arrives.
* Out-of-order data is **persisted** (history is complete) but **never overwrites live state**: the
  twin keeps, per series, the reading with the newest collection timestamp. An identical reading
  delivered twice changes nothing (`services/digital_twin.py`).

## Digital Twin consistency

* **Model:** per-field last-writer-wins by *collection time*, per device. The twin is a derived view;
  PostgreSQL (samples, events) is authoritative.
* **Versioning:** each device's twin document has a monotonic `twin_version` within an `epoch` (backend
  start). Every change is a patch `{path: field}` with `base_version` → `version`.
* **Clients:** a browser that sees a version gap, a new epoch or a reconnect requests a snapshot
  (`twin.sync`) before applying patches again. If the snapshot does not arrive within 3 s, it falls back
  to REST. Live data is never merged across epochs.
* **Restart:** twin documents are saved to Redis (TTL 7 days) and restored at start, then corrected by
  the next telemetry. Without Redis, the twin is rebuilt from the device inventory and new telemetry;
  fields show "no data" until a reading arrives. Nothing is interpolated or invented.
* **Freshness and uncertainty:** each field carries its timestamp and a freshness state (fresh / stale /
  expired by field policy). Values older than their policy are shown as stale, with their age.
* **Anomaly, prediction and alert state** are kept per device and recomputed from twin input. They
  carry their own lifecycle and timestamps, so a delayed batch updates them as late evidence, not as
  "now".

## Failover

Only the elected active backend projects twins and runs background processing. A standby has no state;
on takeover it restores twin documents from Redis and resumes from PostgreSQL. In-flight batches on the
crashed instance were not acknowledged (the agent retries them) or were acknowledged and sit in its
persister queue (the documented loss window).
