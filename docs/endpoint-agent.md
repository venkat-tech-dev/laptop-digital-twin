# Endpoint Agent (Phase 1)

The endpoint agent is a Windows background service that collects device health, performance and
security-posture telemetry, caches it locally, and synchronises it with the backend. It does not need
the browser, the dashboard or an interactive user.

## Architecture

```
Windows Service (LaptopDigitalTwinAgent, LocalSystem, delayed auto-start, restart on failure)
└── AgentRunner  (app/runner.py)  START -> INITIALIZE -> COLLECT -> CACHE -> SYNC -> HEALTH CHECK -> STOP
    ├── Collectors (app/providers/*)  run concurrently, each on an isolated worker lane
    │     fast    cpu, memory, gpu, disk, network, temperature, system
    │     wmi     battery, fan, storage capacity, display, services
    │     proc    process snapshot (+ optional details)
    │     slow    security posture, SMART/WHEA reliability, event log
    │     net     network health (connectivity, gateway latency/loss)
    │     updates Windows Update status
    ├── Local telemetry store (app/storage/queue.py)  SQLite outbox + state, bounded
    ├── Sync manager (app/publisher/sync.py)          newest-first, bulk backfill, backoff, dead-letter
    ├── Secure API client (app/transport/client.py)   HTTPS, device registration, token refresh
    ├── Credential store (app/security/credentials.py) DPAPI-encrypted device token
    ├── Posture evaluation (app/health/compliance.py)  HEALTHY / WARNING / CRITICAL / UNKNOWN
    ├── Health monitor / watchdog (app/health/monitor.py, app/platform/worker.py)
    └── Structured logging (app/observability/logging.py) JSON, rotation, secret redaction
```

* **Isolation.** Each worker lane is a separate daemon thread with COM initialised. A hung Windows API
  call (WMI provider, Update searcher, ICMP) only delays collectors on the same lane. A collector that
  exceeds its timeout reports `ERROR` for its own metrics; a lane stuck longer than
  `AGENT_LANE_HUNG_AFTER_S` is replaced with a fresh thread. Collectors still running when they become
  due are skipped, never queued twice.
* **Local first.** Every flush writes a batch to the SQLite outbox *before* any network attempt. While
  the backend is unreachable, batches are coalesced to one per `OFFLINE_FLUSH_INTERVAL_S` (default
  30 s, ~5 KB compressed each). Limits: `QUEUE_MAX_BATCHES`, `QUEUE_MAX_MB`, `QUEUE_MAX_AGE_H`;
  the oldest rows are dropped and counted. A corrupt database is moved aside and recreated.
* **Sync.** The newest batch is uploaded first (the live twin is current immediately), then the
  backlog oldest-first in bulk requests (`BATCH_SIZE`, marked `replay`). Transient failures back off
  exponentially with jitter (`SYNC_BACKOFF_BASE_S`..`SYNC_BACKOFF_MAX_S`) and never drop data; batches
  rejected as invalid are dead-lettered after `SYNC_MAX_ATTEMPTS`. Every batch has a `batch_id`, so a
  retry after a lost response is acknowledged as `duplicate`. When Windows reports the internet is back,
  the remaining backoff is skipped.
* **Backend replay safety.** Replayed (older) batches are persisted as history but never overwrite
  newer live readings, the live process list, or a newer device-health evaluation, and are not fed to
  the anomaly engine.

## Telemetry schema

| Section | Model | Sent |
|---|---|---|
| DEVICE_STATIC_METADATA | `InventoryEnvelope` | on registration, reconnect and every `STATIC_REFRESH_INTERVAL_S` |
| DEVICE_DYNAMIC_METRICS | `MetricSample` (latest value per metric+labels) | every flush |
| DEVICE_EVENTS | `DeviceEvent` (`event_id`, type, severity, timestamp, message, data) | when they occur |
| DEVICE_HEALTH | `DeviceHealth` (state, reasons, per-check states) | every flush |
| AGENT_HEALTH | `AgentHealth` (run mode, uptime, queue, sync, collectors, footprint) | every `AGENT_HEALTH_INTERVAL_S` |

Event types: `app_crash`, `app_hang`, `boot_performance`, `network_connected`, `network_disconnected`,
`internet_lost`, `internet_restored`, `service_state_changed`, `update_installed`,
`security_posture_changed`, `device_health_changed`, `agent_started`, `agent_stopped`.

## What is collected (and what leaves the laptop)

Everything below is sent to the configured backend only. Nothing else leaves the machine.

| Area | Collected | Source (no admin unless stated) |
|---|---|---|
| CPU | model, cores/threads, utilisation (total, per logical CPU), effective/nominal clock, processor queue length, P/E-core topology | psutil, PDH, WMI, GetLogicalProcessorInformationEx |
| GPU | name, vendor, VRAM, engine utilisation, per-process GPU use | DXGI, PDH |
| Memory | total/used/available, %, page file | psutil |
| Disk | volumes (capacity, used, free, %), read/write throughput, IOPS, active time, latency, health status, NVMe SMART (wear, temperature, spare, power-on hours, media errors) | psutil, PDH, Storage Management, NVMe health log |
| Battery | %, charging state, AC/battery, time remaining, design/full capacity, health %, cycles, voltage, rates | psutil, WMI, powercfg battery report |
| Temperature / fans | ACPI thermal zone, passive limit, throttling; fan RPM if exposed | PDH ACPI; LibreHardwareMonitor (admin) for package/GPU/SSD temperature |
| Network | adapters, link state/speed, throughput, packets, errors; device-network vs internet connectivity; active adapter and type; default-gateway latency and packet loss; backend round-trip time | psutil, Network List Manager, IP Helper, ICMP echo |
| OS | edition, version, build/UBR, display version, architecture, hostname (`INCLUDE_HOSTNAME`), install date, boot time, uptime, boot duration (service only) | WMI, registry, event log |
| Updates | reboot required, pending updates (count, security), last installed update, recent failures | Windows Update Agent (offline search, no traffic to Microsoft) |
| Services | status and start type of `SERVICE_ALLOWLIST` only | Service Control Manager |
| Applications | top processes by CPU/memory/GPU/disk: name, PID, CPU %, memory, threads, handles, start time, I/O, socket counts; crashes and hangs (application name, version, faulting module, exception code) | NtQuerySystemInformation, IP Helper, event log |
| Security | Defender (service, AV, real-time, tamper protection, signature age/date, last scans), registered antivirus products, firewall per profile, Secure Boot, TPM presence/version, WHEA hardware errors, posture state | Defender WMI, Security Center, FwPolicy2, registry, TBS, event log |
| Agent | version, run mode, uptime, CPU/RAM, queue, sync state, per-collector status | the agent itself |

**Never collected:** keystrokes, screenshots, clipboard, passwords, file contents, browser history,
message contents, command lines, window titles, network addresses of connections (only counts).

**Opt-in only:** process image path (account folder redacted), process owner and publisher
(`COLLECT_PROCESS_DETAILS` or Settings → Privacy), IP addresses (`INCLUDE_IP_ADDRESSES`), serial numbers
(`INCLUDE_SERIAL_NUMBERS`), MAC addresses (`INCLUDE_MAC_ADDRESSES`). An ICMP probe beyond the gateway
is sent only if `LATENCY_PROBE_HOST` is set.

**Stored locally** (data directory: `%ProgramData%\LaptopDigitalTwin\agent` for the service,
`%LOCALAPPDATA%\LaptopDigitalTwin\agent` in console mode): `telemetry.db` (queue), `credentials.bin`
(DPAPI-encrypted device token), `logs/agent.log*` (rotated JSON logs, secrets redacted), `health.json`.

## Security

* HTTPS is required; plain HTTP is accepted only for a loopback backend
  (`AGENT_ALLOW_INSECURE_LOCALHOST`). Certificates are validated (system store or `AGENT_CA_BUNDLE`).
* `AGENT_INGEST_KEY` is an enrollment secret: it is exchanged once at `POST /api/v1/agent/register` for
  a per-device token. The backend stores only the token's SHA-256 hash; the agent stores the token
  encrypted with DPAPI. A rejected token triggers one re-registration (rotation). Administrators can
  revoke a device (`DELETE /api/v1/endpoint/credentials/{device_id}`, or Settings → Agent health).
* A device token can only submit data for its own device.
* No credentials are hard-coded; all configuration comes from the environment / `.env`.

## Configuration

All settings live in `agent/app/config/settings.py` and can be set in `.env`. Key ones:

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_BACKEND_URL` | `http://127.0.0.1:8000` | backend base URL (https:// for remote) |
| `AGENT_INGEST_KEY` | — | enrollment key (required) |
| `AGENT_ID` | — | optional fleet label |
| `TELEMETRY_INTERVAL_MS` | 5000 | base interval (CPU, memory, disk, network, temperature) |
| `CPU_/MEMORY_/DISK_/NETWORK_/TEMPERATURE_INTERVAL_MS` | base | per-collector overrides |
| `BATTERY_INTERVAL_MS` | 30000 | battery |
| `NETWORK_HEALTH_INTERVAL_MS` | 30000 | connectivity + latency |
| `SECURITY_INTERVAL_MS` / `SERVICES_INTERVAL_MS` / `EVENTLOG_INTERVAL_MS` | 300000 / 60000 / 60000 | |
| `UPDATES_INTERVAL_MS` / `UPDATES_SEARCH_INTERVAL_S` | 1800000 / 21600 | Windows Update |
| `PUBLISH_INTERVAL_MS` / `OFFLINE_FLUSH_INTERVAL_S` | 5000 / 30 | flush cadence online / offline |
| `BATCH_SIZE` / `QUEUE_MAX_BATCHES` / `QUEUE_MAX_MB` / `QUEUE_MAX_AGE_H` | 50 / 20000 / 200 / 72 | sync + queue limits |
| `SYNC_MAX_ATTEMPTS` / `SYNC_BACKOFF_BASE_S` / `SYNC_BACKOFF_MAX_S` | 5 / 1 / 60 | retry policy |
| `ENABLE_SECURITY_COLLECTION` / `ENABLE_UPDATE_COLLECTION` / `ENABLE_EVENTLOG_COLLECTION` | true | feature switches |
| `SERVICE_ALLOWLIST` | Defender, Firewall, Update, BITS, EventLog, DHCP, DNS, WLAN, Workstation, Time, Security Health | services to report |
| `LOG_LEVEL` / `AGENT_LOG_MAX_MB` / `AGENT_LOG_BACKUPS` | INFO / 5 / 5 | logging |
| `AGENT_DATA_DIR` | see above | local data directory |

The operator can also change the base interval, process cadence and process-detail opt-in at runtime
from Settings (the agent polls `/api/v1/agent/config`).

## Install as a Windows service

From an elevated PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-agent-service.ps1             # install / update + start
powershell -ExecutionPolicy Bypass -File scripts\install-agent-service.ps1 -Uninstall  # remove
```

The service starts at boot (delayed auto-start), runs as LocalSystem, restarts after a crash
(5 s / 30 s / 60 s), handles stop and system shutdown gracefully (final flush to the local queue and a
short sync attempt), and removes the old per-user logon task. Console mode for development:
`cd agent; .venv\Scripts\python -m app.main` (`--once` prints every metric; `--discover` the inventory).

## Validation (this laptop: ThinkPad L14 Gen 4, Windows 11 Pro 26200, non-elevated session)

| Check | Result | How |
|---|---|---|
| Collection without browser/dashboard | PASS | agent ran with no browser open; backend received ~1 batch/s |
| Backend/API unavailable → no crash, local caching | PASS | backend container stopped 2 min: collection continued, batches in SQLite, single warning logged |
| Connectivity restored → queued telemetry synchronises | PASS | backlog uploaded newest-first then replayed; queue 0 within 11 s after backend up |
| Agent hard-killed and restarted → state recovers | PASS | queue, sequence counter and DPAPI token survived; no re-registration |
| Graceful stop (service control path) | PASS | integration test: stop requested from another thread, final flush incl. `agent_stopped` |
| One collector fails / hangs → others continue | PASS | unit tests (timeouts, crashes, hung lane replacement); live: LHM unavailable reported per metric |
| Unsupported hardware → graceful fallback | PASS | unit tests (no Security Center, no NVMe, no TPM, no route, no battery); live: 14 metrics Unavailable with reasons |
| Queue limit → controlled cleanup | PASS (unit) | count, size and age limits; oldest dropped and counted |
| Corrupt local cache | PASS (unit) | file quarantined, new database created, agent continues |
| Sensitive information not collected | PASS | outgoing inventory/events, queue, health file: no user name, paths, keys or passwords |
| Agent resource use | MEASURED | 1 s collectors + 1 s publish: 1.05 % CPU avg (max 1.32 %), 78 MB; Phase-1 defaults (5 s): 0.40 % avg (max 0.67 %), 78 MB — whole machine, 12 logical CPUs, 120 s each |
| Windows service install / auto-start after reboot | NOT AVAILABLE | requires an elevated prompt and a reboot; service host verified to load (`service_entry.py`), lifecycle covered by the integration test |
| Tests | PASS | agent 76, backend 75, frontend 14; ruff + mypy --strict + oxlint clean |

## Known limitations

* CPU package temperature, package power, PL1/PL2, core voltage, GPU temperature/clock/power and fan
  RPM need LibreHardwareMonitor running as administrator (`scripts/install-lhm.ps1`); this laptop does
  not expose fan RPM to Windows at all.
* Per-process network *throughput* needs ETW kernel tracing (administrator); socket counts are reported.
* Boot-duration events are readable only by administrators: available when running as the service.
* Windows Security Center (registered AV products) does not exist on Windows Server.
* Pending-update counts reflect Windows' own last scan (the agent searches offline).
* Hostname is sent by default for fleet identification (`INCLUDE_HOSTNAME=false` to disable).
* The agent footprint grows with shorter intervals; 1 s collection is intended for the live dashboard,
  5 s defaults for fleet operation.

## Phase 2

The near-real-time pipeline is described in [telemetry-pipeline.md](telemetry-pipeline.md). It covers:
- schema 1.1, priorities, the outbox priority and backpressure;
- gzip uploads, `Retry-After`, the heartbeat and presence;
- durable idempotency, WebSocket subscriptions, latency measurement and load-test results.

Agent version 1.3.0.

## Phase 2+ extension points

* New collectors: subclass `TelemetryProvider`, pick a lane, optionally implement `pop_events()`.
* Anomaly detection / prediction / AI diagnosis consume `MetricSample`, `DeviceEvent`, `DeviceHealth`
  on the backend; nothing on the agent needs to change.
* Remediation will need a separate, explicitly authorised command channel: the Phase-1 agent is
  read-only by design.
