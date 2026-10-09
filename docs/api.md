# API reference

Base URL: `http://127.0.0.1:8000`. Interactive OpenAPI docs live at `/docs` (disabled when
`APP_ENV=production`). All timestamps are UTC ISO-8601.

## Authentication

| Mode (`AUTH_MODE`) | Read endpoints | WebSocket |
|---|---|---|
| `none` (local dev only; refused in production) | open | open |
| `api_key` | `X-API-Key: <key>` or `Authorization: Bearer <jwt>` | `/ws/twin?token=<key or jwt>` |
| `jwt` | `Authorization: Bearer <jwt>` (from `POST /api/v1/auth/token`) | `/ws/twin?token=<jwt>` |

Roles: `admin`, `operator`, `viewer` see every device; `employee` sees only devices assigned to the
account (enforced on every device-scoped endpoint and WebSocket topic; other devices answer 404).

Agent endpoints (Phase 2):

- `POST /agent/register` takes the enrollment key `X-Agent-Key: <AGENT_INGEST_KEY>` and returns a
  per-device token.
- Every other agent call sends `Authorization: Bearer <device token>` plus `X-Device-Id: <device id>`.
  A token only authorises data for its own device (`403` otherwise).
- Ingest with the shared enrollment key is refused (`401`) unless `ALLOW_ENROLLMENT_KEY_INGEST=true`
  (tests and legacy agents only).

See [telemetry-pipeline.md](telemetry-pipeline.md) for the wire contract, acknowledgements and
retry semantics.

## REST endpoints (`/api/v1`)

| Method | Path | Description |
|---|---|---|
| GET | `/auth/config` | Public. The auth mode the UI must use |
| POST | `/auth/token` | `{"api_key": "..."}` → `{access_token, expires_at}` (HS256) |
| GET | `/device` | Identity, live status, data source, and **geometry** (`generic`, `matched` or `exact`, plus label) |
| GET | `/device/list` | All known devices |
| GET | `/hardware` | Discovered hardware inventory (serials hidden unless `EXPOSE_SERIAL_NUMBERS=true`) |
| GET | `/twin` | Full twin: components with telemetry, health, thermal summary, active anomalies, processes |
| GET | `/twin/components?include_telemetry=` | Component list |
| GET | `/twin/components/{component_id}` | One component (e.g. `cpu`, `gpu:0x…`, `disk:PhysicalDrive0`, `nic:Wi-Fi`, `battery`) |
| GET | `/telemetry/latest?component_id=` | Latest reading per series, aged to `DEGRADED`/`STALE` |
| GET | `/telemetry/history?keys=…&minutes=30&bucket_seconds=` | Persisted history in time buckets (avg/min/max/n), max 20 keys |
| GET | `/telemetry/recent?seconds=300` | Short-term buffer for chart backfill |
| GET | `/telemetry/catalog` | Persisted series and their sources |
| GET | `/health` | Overall and per-component explainable health |
| GET | `/health/events?limit=` | Health status transitions |
| GET | `/anomalies?status=active\|resolved&severity=&since=&limit=` | Anomalies |
| GET | `/anomalies/rules` | Configured V1 rules and V2 statistical detectors |
| GET | `/analytics/thermal?minutes=30` | Per-sensor stats, trend, time above 80/90 °C, throttled seconds |
| GET | `/analytics/performance?minutes=30` | mean/min/max/p95/stddev for key metrics |
| GET | `/analytics/predictions` | Trend predictions with confidence, method, assumptions |
| GET | `/simulation/scenarios` | Built-in what-if workloads |
| POST | `/simulation/run` | `{"scenario": "ai_ml", "duration_minutes": 10, "cpu_load"?, "gpu_load"?, "ram_gb"?, "on_battery"?}` → labelled SIMULATION result |
| GET | `/system/processes?sort_by=cpu\|memory\|gpu\|disk&limit=15` | Top processes (read-only) |
| GET | `/system/events?limit=` | System events (online/offline, sensor availability, battery/thermal state) |
| GET | `/system/info` | Backend config summary (no secrets) |
| GET | `/devices?q=&connectivity=&health=&department=&sort=&order=&page=&page_size=` | Device list with server-side search, filters, sorting, pagination; rows = twin summary (connectivity, health, CPU/RAM/disk/battery/temperature, owner, department) |
| GET | `/fleet/summary` | Organization overview: devices by health and connectivity, per department |
| GET | `/devices/{device_id}` | Identity, ownership (assignment) and current summary |
| GET | `/devices/{device_id}/twin?format=nested\|flat` | Digital twin snapshot (identity, state, health, connectivity, freshness, version, epoch) |
| GET | `/devices/{device_id}/twin/explain?path=` | Provenance and rules behind one twin field |
| GET | `/devices/{device_id}/timeline?limit=&severity=` | Twin + agent events, newest first |
| GET | `/devices/{device_id}/history?fields=&range=15m\|1h\|6h\|24h` | History of twin fields (resolved to metric keys) |
| PUT | `/devices/{device_id}/assignment` | Admin: assign to an employee account `{username, employee_name}` |
| GET | `/devices/{device_id}/state` | Current state of one device from the in-memory projection (no database query): twin snapshot + presence + device/agent health + recent events + sequence stats |
| GET | `/endpoint?device_id=` | Device posture, agent health, recent device events, presence, sequence stats, credential |
| GET | `/pipeline/stats` | Pipeline observability: ingest counters and rejections, latency per stage (p50/p95/p99/max), presence summary, per-device sequences, clock-drift warnings, persistence, retention, WebSocket fan-out |
| POST | `/agent/register` | Agent: enrollment key → `{device_id, device_token}` (re-registering rotates the token) |
| POST | `/agent/heartbeat` | Agent: liveness (drives presence) → `{server_time, presence, last_sequence_received, clock_offset_s}` |
| POST | `/ingest/inventory` | Agent: hardware inventory envelope |
| POST | `/ingest/telemetry` | Agent: one telemetry batch. `409` = unknown device (agent re-sends inventory) |
| POST | `/ingest/telemetry/bulk` | Agent: up to 500 batches, gzip accepted; summary + per-batch acknowledgement |

Errors use standard status codes:

- `401`: authentication failed.
- `404`: no device or component.
- `403`: a device token used for another device.
- `409`: conflict.
- `413`: agent request body above `INGEST_MAX_BODY_BYTES` (compressed) or
  `INGEST_MAX_DECOMPRESSED_BYTES` (inflated).
- `415`: unsupported `Content-Encoding` (only `gzip`/`deflate`/identity are accepted).
- `422`: validation error. Submitted values are not echoed back.
- `429`: rate limit hit, with a `Retry-After` header. Agents are limited per device
  (`INGEST_RATE_PER_DEVICE_PER_MIN`, burst `INGEST_RATE_BURST`); browsers per IP.
- `500`: generic error. Details are logged, not returned.

## Observability endpoints

| Path | Description |
|---|---|
| `/health/live` | Process liveness |
| `/health/ready` | Database and Redis reachability with latency, persistence queue, agent status (`503` if the database is down) |
| `/metrics` | Prometheus metrics: API latency, ingest counts, event processing latency, WebSocket connections, DB/Redis latency and errors, persistence queue, sensor availability, agent provider failures, active anomalies |

Every response carries an `X-Request-ID` header. Send your own to correlate with the logs.

## WebSocket `/ws/twin`

Server → client (JSON text frames, `event` field):

| Event | Payload |
|---|---|
| `connection_status` | `status`, `client_id`, `heartbeat_interval_s`, `protocol`, `primary_device_id`, `topics_supported` |
| `subscribed` / `unsubscribed` | `topics` (now active), `accepted`, `rejected: [{topic, reason}]`, `devices` |
| `twin_snapshot` | `device_id`, `twin` (same shape as `GET /twin`, plus `presence`); sent on connect (primary device), once per newly subscribed device, and on `resync` |
| `fleet_snapshot` | `devices: [{device_id, model, status, presence, last_contact_at}]` after subscribing to `fleet` |
| `telemetry_update` | `sequence`, `device_status`, `components: {id: {current_state, availability, health, last_updated, telemetry: {key: reading}}}`, optional `processes`, `timing: {collected_at, sent_at, server_received_at, published_at, replay}` |
| `device_presence_changed` | `presence`, `previous_presence` (`ONLINE`/`STALE`/`OFFLINE`/`UNKNOWN`), `last_contact_at` |
| `twin.snapshot` / `twin.state.patch` | Phase 3 digital twin: flat snapshot, then versioned patches `{twin_version, base_version, epoch, changes}` (see [digital-twin.md](digital-twin.md)) |
| `twin.status.changed` / `twin.event.created` / `twin.summary` / `twin.sync.required` | connectivity transitions, timeline events, fleet rows, resync requests |
| `component_state_changed` | `component_id`, `previous_state`, `current_state`, `domain_event` (`ComponentUpdated`, `BatteryStateChanged`, `ThermalStateChanged`) |
| `health_changed` | `component_id`, `score`, `status`, `previous_*`, `reasons` |
| `anomaly_detected` / `anomaly_resolved` | `anomaly` |
| `device_status_changed` | `status`, `previous_status`, `last_seen_at` (`DeviceOnline`/`DeviceOffline`) |
| `system_event` | `event_type` (e.g. `sensor_unavailable`, `sensor_recovered`), `message`, `data` |
| `heartbeat` | every 5 s to every client: `server_time`, `primary_device_id`, `device_status`, `last_seen` |
| `pong` | reply to a client ping, with `server_time` (used for clock-offset estimation) |

Client → server:

```json
{"type": "subscribe",   "topics": ["device:ldt-70732d1beda54383", "workspace:<id>", "fleet"]}
{"type": "unsubscribe", "topics": ["fleet"]}
{"type": "resync", "device_id": "ldt-70732d1beda54383"}
{"type": "ping", "latency": {"websocket_delivery_ms": [3.1, 2.8], "end_to_end_latency_ms": [212, 198]}}
```

The browser pings every 10 s. Topics route messages by `device_id`:
- `device:<id>` gets everything about that device.
- `workspace:<id>` is expanded to its devices at subscribe time.
- `fleet` gets the low-volume events (presence, status, anomalies, health) of every device, but not
  `telemetry_update`.
- A client that never subscribes gets the primary device only, which keeps older clients working.

A topic must name a known device or workspace (`rejected` with `unknown_device` /
`unknown_workspace` otherwise). The subscription limit is `WS_MAX_SUBSCRIPTIONS`.

After a reconnect the browser re-authenticates (token in the URL) and re-subscribes. The server
then sends fresh snapshots, so no state is missed while the client was away.
The server closes connections idle for more than 45 s, and disconnects slow consumers whose
bounded send queue overflows. The browser client reconnects with exponential backoff and jitter,
and treats 15 s without any frame as a disconnect.

Example `telemetry_update` (abridged):

```json
{
  "event": "telemetry_update", "mode": "live", "device_id": "ldt-70732d1beda54383", "sequence": 812,
  "device_status": "LIVE", "timestamp": "2026-10-06T11:58:25.401Z",
  "components": {
    "cpu": {"current_state": "busy", "availability": "partial",
            "health": {"score": 100, "status": "healthy", "reasons": [...]},
            "telemetry": {"cpu.usage_percent": {"value": 83.1, "unit": "percent", "quality": "GOOD",
                                                "source": "psutil (Win32 GetSystemTimes)", "kind": "measured", ...}}}
  }
}
```
