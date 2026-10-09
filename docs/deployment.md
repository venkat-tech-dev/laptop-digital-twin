# Deployment

## Topology

```
Windows host
 ├─ Telemetry agent (Python, runs as the logged-in user or as a service)
 ├─ Browser
 └─ Docker
     ├─ backend   (FastAPI, runs `alembic upgrade head` on start)      127.0.0.1:8000
     ├─ frontend  (nginx: SPA + reverse proxy for /api, /ws, /models)  127.0.0.1:8088
     ├─ postgres  (TimescaleDB 2.17 / PostgreSQL 16, volume pgdata)    127.0.0.1:15432
     └─ redis     (7.4, no persistence, 128 MB LRU)                    127.0.0.1:16379
```

The agent cannot run in a container: Windows performance counters, WMI, the ACPI battery driver,
DXGI and `NtQuerySystemInformation` are only reachable from the host.

## Local production-like stack

```powershell
copy .env.example .env      # then set APP_ENV=production, AUTH_MODE, API_KEYS, JWT_SECRET,
                            # AGENT_INGEST_KEY, POSTGRES_PASSWORD (+ DATABASE_URL password)
docker compose up -d --build
cd agent; .\.venv\Scripts\python -m app.main
```

Open http://127.0.0.1:8088 and enter an API key when prompted.

## Running the agent as a background service

**Option A: Scheduled task at logon.** This is the simplest, and it runs as you.

```powershell
.\scripts\install-agent-task.ps1          # registers "LaptopDigitalTwinAgent" (runs at logon, restarts on failure)
Unregister-ScheduledTask -TaskName LaptopDigitalTwinAgent -Confirm:$false   # remove
```

**Option B: Windows service** with NSSM or WinSW wrapping
`agent\.venv\Scripts\python.exe -m app.main`, with the working directory set to `agent\`.
Running as `LocalSystem` additionally allows reading `MSAcpi_ThermalZoneTemperature` and the
storage reliability counters, but the default providers do not require it.

**Richer sensors.** Run LibreHardwareMonitor as administrator at logon, with its Remote Web
Server enabled (see README).

## Database operations

- Migrations: `cd backend; .\.venv\Scripts\alembic upgrade head` (the backend container runs this automatically).
- TimescaleDB is detected at migration time:
  - available: hypertable, compression after 2 days, retention of 30 days;
  - plain PostgreSQL: the backend purges samples older than `RETENTION_DAYS` every hour.
- Back up with `docker exec laptop-digital-twin-postgres-1 pg_dump -U ldt ldt > backup.sql`.

## Observability

- Logs are JSON on stdout (structlog) with `request_id`.
- Prometheus: scrape `http://backend:8000/metrics`. A sample config is in
  `infrastructure/monitoring/prometheus.yml`.
- Tracing: `pip install -e "backend[otel]"` and set `OTEL_EXPORTER_OTLP_ENDPOINT`. FastAPI is
  then instrumented and spans are exported over OTLP/HTTP.
- Probes: `/health/live` (liveness) and `/health/ready` (readiness; 503 when the database is
  unreachable).

## Scaling notes

- The backend is a single process by design. The twin state lives in memory per instance, with a
  hot copy in Redis.
- With Redis enabled, WebSocket fan-out goes through Redis pub/sub, so several replicas can serve
  browsers. Agent ingest for a given device should still reach a single replica (sticky routing
  by device ID).

## Cloud adapters (optional, not required)

The agent publishes through the `TelemetryPublisher` protocol (`agent/app/publisher/base.py`). A
cloud deployment adds an implementation without touching providers or the UI:

```
Laptop agent ──MQTT/TLS──▶ AWS IoT Core / Azure IoT Hub ──▶ Kafka / Event Hubs
      ──▶ telemetry processor (this backend's TelemetryService) ──▶ Redis + TimescaleDB ──▶ WebSocket UI
```

- **AWS IoT Core**: an `MqttPublisher` (e.g. `awsiotsdk`) with X.509 device certificates. Topics
  are `ldt/{device_id}/telemetry` and `ldt/{device_id}/inventory`, and the payload is the same
  `TelemetryBatch` JSON. An IoT Rule forwards to Kinesis or MSK, and a consumer calls the backend
  ingest service.
- **Azure IoT Hub**: an `AzureIoTPublisher` (e.g. `azure-iot-device`) sends device-to-cloud
  messages carrying the same JSON. Event Hubs-compatible routing feeds the consumer.

These adapters are **not implemented**; only the interface and contract exist. No telemetry leaves
the machine unless such an adapter is explicitly added and configured.
