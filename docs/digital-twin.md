# Digital twin state engine (Phase 3)

The physical laptop is the source of truth. Telemetry describes it. The digital twin represents it.
The UI visualizes the twin.

```
REAL LAPTOP -> endpoint agent -> telemetry pipeline (Phase 2: validate, dedupe, persist)
            -> DigitalTwinService   latest reading per series (older or duplicate readings never win)
            -> TwinEngine.project   normalized fields + severity + freshness + connectivity + health
                                     + timeline events                      [deterministic rules]
            -> TwinDocument v<n>    current state, versioned per device, Redis-backed
                 |- REST  GET /devices/{id}/twin        (snapshot)
                 |- WS    twin.snapshot -> twin.state.patch (only changed fields)
                 |- WS    twin.status.changed, twin.event.created, twin.summary (fleet)
                 |- history stays in TimescaleDB (GET /devices/{id}/history)
                 \- future intelligence reads the same document + timeline
```

Code:
- **Rules:** `backend/app/domain/twin/rules.py`.
- **Projection:** `backend/app/domain/twin/projection.py`, a pure function from readings to fields.
- **Engine:** `backend/app/services/twin_engine.py`.
- **Distribution, persistence and timeline recording:** `backend/app/services/twin_state.py`.
- **APIs:** `backend/app/api/v1/devices.py`.

## Identity

| Field | Meaning |
|---|---|
| `twin_id` | `uuid5(namespace, device_id)`: stable, never derived from the volatile hostname |
| `device_id` | hardware-derived id from the agent (`ldt-…`) |
| `agent_id` | per-installation id: `AGENT_ID` label, else a random id persisted in the agent's outbox |
| `hostname`, `manufacturer`, `model`, OS | descriptive only, from the inventory |
| `owner`, `department` | assignment (employee) and workspace |
| `twin_version`, `epoch` | the version increments on every state change; the epoch changes when the backend restarts. `(epoch, version)` orders states, and versions are monotonic within an epoch. |

## State document

The document is flat. Its keys are dotted paths, and patches use the same paths. Sections:
- `identity`, `hardware`, `operating_system`, `performance` (cpu, memory, disk, gpu), `storage`,
  `network`, `battery`, `thermal`, `security`, `applications`, `agent`;
- `connectivity`, `health`, `alerts`, `sections`, `twin`.

Every measured field is an object:

```json
"performance.cpu.usage_percent": {
  "value": 72.4, "unit": "%", "label": null, "reason": null,
  "timestamp": "2026-10-07T11:39:02.118+00:00",      // collected on the device
  "interval_s": 1.0,                                 // the collector's interval (agent schema 1.2)
  "status": "elevated",                              // severity (rules below)
  "freshness": "LIVE",
  "source": {"metric_key": "cpu.usage_percent", "origin": "psutil (Win32 GetSystemTimes)",
             "batch_id": "9f3c…", "sequence": 18921, "received_at": "2026-10-07T11:39:03.006+00:00"}
}
```

`value: null` means unknown. The UI shows "No data" or "Unsupported" with the agent's reason, never
0. The document contains only metrics the agent actually reports: no field is invented, and
unsupported sensors are `UNSUPPORTED`.

**Partial telemetry:** a batch that lacks a metric does not reset it. The twin keeps the latest
reading per series. That field's freshness decays from its own timestamp until a new reading
arrives.

**Ordering and duplicates:**
- A reading older than the current one never replaces it (replayed backlog only goes to history).
- An identical reading (same timestamp, value and availability) is a no-op.
- Duplicate batches are already removed by Phase 2's batch-id dedupe.

**Determinism:** the projection is a pure function of the current readings. The engine's rules use
the evaluation time that is passed in, never the wall clock, so the same telemetry sequence evaluated
at the same times gives the same documents, versions and events. A test covers this.

**Auditability:** `GET /devices/{id}/twin/explain?path=…` answers "why does the twin say this?".
It returns the source reading (value, collection time, interval, source API, quality), the batch and
sequence that delivered it, when it was received, the severity rule with its thresholds, and the
freshness limits.

## Rules (deterministic, documented in code)

### Freshness, per metric

```
wait     = TWIN_PUBLISH_WAIT_S (5 s agent batching) + TWIN_FRESHNESS_GRACE_S (5 s)
LIVE     age <= interval + wait
RECENT   age <= max(4 x interval, 60 s) + wait
STALE    older
OFFLINE  device offline            UNKNOWN  never reported      UNSUPPORTED  sensor unavailable
```

A 1-second CPU sample is LIVE for 11 s. A 5-minute security reading stays LIVE for 310 s.
Phase 1's fixed 3/10/30 s thresholds marked slow metrics stale seconds after an on-time sample.
Static values (capacities, models) do not age; they follow the device's connectivity.

### Connectivity (agent heartbeat + telemetry)

| State | Rule |
|---|---|
| ONLINE | heartbeat ok and telemetry on time |
| DEGRADED | heartbeat ok but telemetry late, or ≥ 3 collectors failing |
| STALE | heartbeat missed (`PRESENCE_STALE_AFTER_S`, 90 s) |
| OFFLINE | no contact for `PRESENCE_OFFLINE_AFTER_S` (300 s) |
| UNKNOWN | never contacted since the backend started, and no telemetry |

The browser does not count as a signal: an open page never makes a device look live.

### Severity (visual state) per metric, with hysteresis

| Field | elevated | warning | critical | note |
|---|---|---|---|---|
| CPU usage % | 60 | 90 | 95 | health only when sustained (mean ≥ 90 % over 120 s) |
| CPU-area temperature °C | 80 | 90 | 98 | the same thresholds as Phase 1's thermal bands |
| Memory in use % | 70 | 90 | 95 | |
| System volume used % | 80 | 90 | 95 | |
| Drive activity % | 50 | 90 | 98 | visual only |
| GPU usage % | 60 | 90 | 95 | visual only |
| Battery charge % (only while on battery) | ≤ 30 | ≤ 15 | ≤ 5 | on AC or charging: normal |
| Battery health % | < 80 | < 60 | < 40 | |
| Gateway latency ms / packet loss % | 50 / 1 | 150 / 5 | 500 / 20 | visual only |
| Drive wear % | 70 | 90 | 100 | |
| Signature age (days) | 3 | 7 | 30 | |
| Booleans | | firewall off, Secure Boot off, no internet, no network | real-time protection off, antivirus off, SMART critical warning, drive not healthy | throttling and restart-required are elevated |

A value drops to a lower level only once it is past the threshold minus the hysteresis (3 points
for most), so values hovering at a boundary don't flap.

### Health (device level)

The device's health is the worst of the following rules:
- thermal, memory, disk space, drive, battery, network and security, each from the severity table
  above;
- sustained CPU (mean ≥ 90 % over 120 s);
- the endpoint's own security posture, added only when it is worse than the twin's security rule;
- agent problems (≥ 3 failing collectors or ≥ 1000 queued batches): warning;
- active anomalies from the existing rule-based and statistical detectors.

When the device is OFFLINE or UNKNOWN, health is **UNKNOWN**, and the previous verdict is kept as
`last_known`. Every reason lists its rule, field, message and rule description.

### Visual states

Each section (cpu, memory, storage, gpu, network, battery, thermal, security) and the device as a
whole take the worst severity among their fields: `normal`, `elevated`, `warning` or `critical`.
`offline` applies when the device is offline, and `unknown` when there is no data. The 3D twin shows
severity as halos on the physical parts, with critical pulsing and normal showing no halo. Offline,
stale or unknown devices are desaturated with an explicit overlay.

## Timeline

Events come only from telemetry, the twin and the agent; nothing is generated for demonstration:
- `twin.threshold`: a metric enters or leaves warning/critical, or a boolean alert flips. When memory
  enters warning, the event also names the top memory process (if process names are allowed by
  `TWIN_SHOW_PROCESS_NAMES`).
- `twin.connectivity`: online, offline, delayed.
- `twin.health`: health transitions with the triggering rule.
- `device.*`: agent events (crashes, network lost or restored, service changes, updates, agent
  start/stop, collector failures).

Events are persisted in `system_events`, with idempotent `event_uid`, priority and category. They
are served by `GET /devices/{id}/timeline` and streamed as `twin.event.created`.

## APIs

| Method | Path | Notes |
|---|---|---|
| GET | `/devices?q=&connectivity=&health=&department=&sort=&order=&page=&page_size=` | server-side search, filter, sort, pagination (≤ 500 per page) + counts |
| GET | `/fleet/summary` | organization → departments: totals by health and connectivity |
| GET | `/devices/{id}` | identity, ownership, current summary |
| GET | `/devices/{id}/twin?format=nested\|flat` | snapshot from memory (no database query) |
| GET | `/devices/{id}/twin/explain?path=` | provenance + rules for one field |
| GET | `/devices/{id}/timeline?limit=&severity=` | newest first |
| GET | `/devices/{id}/history?fields=…&range=15m\|1h\|6h\|24h` | twin field paths resolved to metric keys; 5-minute aggregates for long ranges |
| PUT | `/devices/{id}/assignment` | admin: `{username, employee_name}` |

The twin is never written by clients. Operational state comes only from telemetry. Administrators
change only metadata: assignments and workspaces.

## WebSocket

| Event | Payload |
|---|---|
| `twin.snapshot` | `{twin: {twin_id, device_id, twin_version, epoch, state (flat)}}`, sent on subscribe and on `{"type": "twin.sync", "device_id": …}` |
| `twin.state.patch` | `{twin_version, base_version, epoch, changes: {path: field or null}}` |
| `twin.status.changed` | `{previous, status, twin_version}` (connectivity) |
| `twin.event.created` | `{event: {event_id, type, severity, timestamp, message, data}}` |
| `twin.summary` | fleet topic: compact row per device (≤ 1 per 10 s per device, immediately on connectivity/health/visual changes) |
| `twin.sync.required` | the hardware inventory rebuilt the component tree; fetch a snapshot |

The client applies a patch only when `epoch` matches and `base_version` equals its own version.
Anything else (a missed message, or a backend restart) triggers `twin.sync`, falling back to the
REST snapshot after 3 s. Missed state is never reconstructed from partial messages. After a
reconnect the client re-authenticates, re-subscribes, and receives fresh snapshots.

## Authorization

| Role | Devices | Fleet, pipeline, workspaces, settings |
|---|---|---|
| admin, operator, viewer | all | yes (writes per role) |
| employee | only devices assigned to the account | no (403) |

The authorization is enforced server-side:
- **REST:** every device-scoped endpoint and every endpoint with `?device_id=` goes through one
  dependency (`app/api/access.py`). An employee without a parameter gets their own device.
- **WebSocket:** the connection carries the allowed set. `device:`, `workspace:` and `fleet` topics
  are filtered by it, so an employee's fleet channel only contains their own devices.
- **Unauthorized ids:** a device the caller may not see is answered exactly like an unknown one
  (404 / `unknown_device`), so ids cannot be probed.

## Frontend

- **Data flow:** a REST snapshot first, then a `device:<id>` subscription, then patches. The store
  (`stores/twinDocStore.ts`) holds the flat document, and components subscribe to single fields
  (`useTwinValue(path)`). A CPU patch re-renders only the CPU card; a test covers this. The ticking
  "updated 4 s ago" text is its own component.
- **Device scope:** `services/deviceScope.ts` holds one selected device per tab. Every
  device-scoped REST call carries it, the WebSocket follows it, and pages remount on switch.
- **Fleet:** `pages/FleetPage.tsx` shows the organization overview and departments, plus the device
  list with server-side search, filter, sort and pagination. Rows are updated by `twin.summary` on
  the `fleet` topic (no per-row connections, no polling).
- **Twin page:** a header (identity, owner, live connectivity, health, version) and state cards
  (value, severity, freshness, age, real 5-minute delta, mini-trend from history plus live points,
  "why this value?"). Then the health reasons and the timeline. The 3D twin shows severity halos
  and an offline state.
- **Themes:** dark (default) or light (Settings → Appearance).

## Measured performance

These were measured on this laptop:
- **Machine:** i5-1335U, 16 GB, Windows 11.
- **Backend:** one uvicorn process. PostgreSQL/TimescaleDB and Redis run in Docker Desktop.
- **Load generator:** `scripts/loadtest.py`, on the same machine, with real-sized batches (169
  samples, 32 processes) every 5 s per device.
- **Viewers:** one WebSocket observer subscribed to `fleet` and one device.

The raw data is in `docs/loadtest-results-phase3.json`.

| Devices | Twin projection p50 / p95 | `GET /devices/{id}/twin` p50 / p95 | Device list page p50 | Search p50 | Fleet summary p50 | Timeline p50 | End-to-end p50 / p95 | Backend CPU | History samples dropped |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 0.85 / 1.2 ms | 2.9 / 3.7 ms | 2.6 ms | 3.1 ms | 2.5 ms | 12.8 ms | 16 / 20 ms | 1 % | 0 |
| 10 | 0.89 / 2.0 ms | 2.5 / 3.5 ms | 4.0 ms | 3.5 ms | 3.2 ms | 16.0 ms | 14 / 41 ms | 6 % | 0 |
| 100 | 1.08 / 2.4 ms | 4.1 / 7.1 ms | 6.4 ms | 6.0 ms | 6.7 ms | 18.1 ms | 51 / 191 ms | 60 % | 0 |
| 500 | 1.13 / 2.4 ms | 4.6 / 14 ms | 12.6 ms | 13.7 ms | 12.3 ms | 19.3 ms | overloaded (s) | 86 % | 104,744 |

- **WebSocket patch size:** partial field updates (`merge`) average about 9.7 KB per batch with the
  synthetic load, where every metric changes every batch. Before field-level merging it was
  18.5–20.5 KB; the full snapshot is about 22 KB.
- **Fleet traffic:** 1 `twin.summary` per device per 10 s at most, immediately on state changes.
- **Interest-based fan-out:** per-batch deltas for devices that nobody watches are neither
  serialized nor published. At 100 devices this took the backend from 90 % to 60 % CPU, end-to-end
  p50 from 439 ms to 51 ms, and history drops from 19,832 to 0.

A single process is comfortable at **100 agents**, and about 160 by CPU extrapolation. 500 agents
need several backend replicas with device-partitioned routing; see the limitations in the Phase 3
report.

Browser, real telemetry from this laptop, Chromium via Playwright:

| Metric | Value |
|---|---|
| patch received → DOM updated (CPU card) | p50 21.7 ms, p95 45.8 ms (16/16 patches) |
| page load → first live value (Vite dev server) | 4.6–7.2 s |
| JS heap with the twin page open | 86–98 MB |
| device stops → STALE overlay / OFFLINE overlay | 19–20 s / 40 s (test thresholds 20 / 40 s) |
| agent back → twin LIVE again | 7–9 s |
| backend restart → patches resume, no page reload | resync on the new epoch at reconnect, ≈ 1 s |

## Scope

There is no machine learning, prediction, LLM, remediation or notification workflow. The twin
document, timeline and history are the inputs those later phases will use.
