# Architecture

## Layers

```
PHYSICAL LAPTOP (Windows)
   │  PDH performance counters · WMI/CIM · DXGI · NtQuerySystemInformation · powercfg · psutil · (LibreHardwareMonitor)
   ▼
SYSTEM TELEMETRY AGENT            agent/app/providers/*          one provider per hardware domain
   ▼
TELEMETRY NORMALIZATION           agent/app/normalization/       units → canonical, validation, quality
   ▼
EVENT / MESSAGE PIPELINE          agent/app/publisher/           batching, bounded store-and-forward, HTTP
   ▼                                                              (MQTT/IoT adapters implement the same protocol)
FASTAPI INGEST                    backend/app/api/v1/ingest.py   X-Agent-Key, schema validation
   ▼
DIGITAL TWIN STATE ENGINE         backend/app/services/digital_twin.py
   │   routes samples → components, derives states, health engine, anomaly engine, liveness
   ├──▶ domain events → EventBus → (Redis pub/sub) → WebSocket /ws/twin → React
   ├──▶ Redis: hot twin snapshot, short-term chart buffer (stream)
   └──▶ PostgreSQL + TimescaleDB: samples (hypertable), anomalies, health/system events, inventory
              ▲
ANALYTICS / PREDICTIONS / SIMULATION   backend/app/services/{analytics,simulation}.py
```

## Separation of concerns

| Concern | Location |
|---|---|
| Hardware collection | `agent/app/providers/`, platform seams in `agent/app/platform/` (WMI, PDH, DXGI, NT, powercfg, LHM) |
| Normalization | `agent/app/normalization/normalizer.py` |
| Wire contract | `agent/app/contracts.py` ⇄ `backend/app/schemas/ingest.py` (a test keeps them in sync) |
| Backend API | `backend/app/api/` (thin routes, no business logic) |
| Twin state | `backend/app/services/digital_twin.py` + `backend/app/domain/components/` |
| Persistence | `backend/app/repositories/` (protocols, SQL and in-memory implementations), `services/persistence.py` |
| Analytics | `backend/app/services/analytics.py`, `backend/app/domain/analytics/stats.py` |
| Anomaly detection | `backend/app/domain/anomalies/` (rules V1, statistical V2) |
| Health | `backend/app/domain/health/engine.py` |
| Frontend visualization | `frontend/src/three/` (3D), `frontend/src/features/` (panels) |
| Infrastructure | `docker-compose.yml`, `infrastructure/docker/` |
| Security | `backend/app/core/security.py`, `middleware.py`, `api/deps.py` |
| Observability | `backend/app/core/{logging,metrics,tracing}.py`, `/health/*`, `/metrics` |

## Agent internals

* **Providers** (`TelemetryProvider`): CPU, Memory, GPU, Disk, StorageCapacity, Network, Battery,
  Temperature, Fan, Process, System and Display. Each declares its metrics, returns `Reading`s in
  the raw units of its source, and never invents values. Every failure is local to its metric.
* **Platform worker**: one MTA-initialised thread for all COM/WMI/PDH calls, keeping blocking I/O
  off the asyncio loop. Slow work (the `powercfg` battery report) runs on its own background thread.
* **Scheduler**: runs each provider at its own interval. A provider exception or timeout becomes
  `ERROR` samples for that provider's metrics; the other providers keep running. A self-imposed
  CPU budget (`AGENT_CPU_BUDGET_PERCENT`) stretches intervals up to 4× if the agent gets heavy.
  The agent reports its own CPU, RSS, collection durations and failures as `agent.*` metrics.
* **Process enumeration** uses one `NtQuerySystemInformation(SystemProcessInformation)` call
  (about 7 ms for 400 processes). The psutil approach measured 4.5 s on this machine.
* **Delivery**: samples are accumulated (latest per series) and flushed each second. When the
  backend is down, batches go into a bounded buffer (oldest dropped and counted) and are replayed
  in order with exponential backoff. The hardware inventory is re-announced after every reconnect.
  On HTTP 409 (backend lost the device), the agent re-sends the inventory, then the batch.

## Digital twin domain model

```
Laptop
 ├─ Chassis                     (structural, no sensors)
 ├─ Display                     brightness (WMI)
 ├─ Motherboard
 │   ├─ CPU                     usage, per-core, frequency, package temp/power (LHM)
 │   ├─ GPU (per DXGI adapter)  engine usage, memory, temp/clock/power (LHM)
 │   ├─ Memory                  usage, page file, modules
 │   └─ VRM                     (no sensors exposed)
 ├─ Storage
 │   └─ Disk (per physical disk)  throughput, IOPS, latency, queue, Windows health, temp (LHM)
 ├─ Battery                     charge, state, capacity, wear, cycles, voltage, rates
 ├─ Cooling
 │   ├─ Fan                     RPM (LHM / Win32_Fan), else unobservable
 │   └─ Thermal sensors         ACPI thermal zones, passive limit, throttle reasons
 ├─ Network
 │   └─ Network adapter (per NIC)  link, speed, throughput, errors
 ├─ Power                       source, system power (measured on battery)
 └─ Operating system
     └─ Telemetry agent         self-monitoring
```

Each component has `component_id`, `component_type`, `name`, `manufacturer`, `model`,
`current_state`, `health` (score, status, reasons), `telemetry` (readings keyed by
`metric{labels}`), `last_updated` and `availability`
(`available | partial | unavailable | no_telemetry`).

## Event model

Domain events (`backend/app/domain/events/events.py`):

`TelemetryReceived`, `ComponentUpdated`, `HealthChanged`, `AnomalyDetected`, `AnomalyResolved`,
`SensorUnavailable`, `DeviceOnline`, `DeviceOffline`, `DeviceStatusChanged`,
`BatteryStateChanged`, `ThermalStateChanged`, `SystemEvent`.

They are mapped to WebSocket events in `infrastructure/websocket/protocol.py`:
`telemetry_update`, `component_state_changed`, `health_changed`, `anomaly_detected`,
`anomaly_resolved`, `device_status_changed`, `system_event`, plus the transport events
`connection_status`, `twin_snapshot`, `heartbeat` and `pong`. The agent knows nothing about the UI.

## Real-time path and fan-out

1. The agent POSTs a batch.
2. `TelemetryService.ingest`:
   - `DigitalTwinService.update` produces events.
   - Samples go into the persistence queue (non-blocking).
   - Recent values go into the chart buffer.
   - Events are published.
3. The event bus publishes to Redis `ldt:events` (when Redis is up). Every backend replica's
   listener broadcasts to its WebSocket clients. Without Redis, events are broadcast locally.
4. Each WebSocket client has a bounded send queue. A slow consumer is disconnected rather than
   blocking others, and it resynchronises from a fresh `twin_snapshot` on reconnect.

## Persistence

* `telemetry_metrics`: catalogue of series (one row per `metric{labels}` per device).
* `telemetry_samples (time, metric_id, value, quality)`: a TimescaleDB hypertable with 1-day
  chunks, compression after 2 days and retention of 30 days. With plain PostgreSQL it is a
  normal table with an index plus a periodic purge task.
* Samples are down-sampled per series (`PERSIST_SAMPLE_INTERVAL_S`, default 5 s) and batch-inserted.
* `devices`, `hardware_components`, `health_events`, `anomalies` and `system_events` are
  written by a sequential background recorder.
* Schema changes go through Alembic only (`backend/alembic/versions`).

## Key decisions

* **HTTP ingest instead of agent→Redis.** It decouples the agent from infrastructure, gives one
  authenticated entry point, and maps cleanly to MQTT/IoT Hub later.
* **ACPI thermal zone as the labelled fallback.** It is the only temperature Windows gives a
  non-admin user. The UI and API always say which sensor a temperature came from
  (`is_cpu_package_sensor`).
* **Procedural 3D model.** No exact mesh of the detected laptop ships with the project, so a
  parametric generic laptop is scaled to the detected panel size and labelled `GENERIC MODEL`.
  Drop a GLB into `models/` (see `models/README.md`) to use a matched model.
* **No ML (V3) yet.** Rules plus EWMA z-scores are explainable and sufficient for this data. ML
  would need labelled history to provide measurable value.
