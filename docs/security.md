# Security and privacy

The twin monitors sensitive local system information. The defaults are **local-first** and
**least exposure**.

> **Phase 9 (organizations, enterprise security, governance):** see
> [architecture/multi-tenancy.md](architecture/multi-tenancy.md),
> [security/security-model.md](security/security-model.md), [security/threat-model.md](security/threat-model.md),
> [security/tenant-isolation.md](security/tenant-isolation.md), [security/identity.md](security/identity.md),
> [security/device-enrollment.md](security/device-enrollment.md),
> [security/baseline-checklist.md](security/baseline-checklist.md),
> [security/compliance-readiness.md](security/compliance-readiness.md),
> [security/dependency-review.md](security/dependency-review.md),
> [governance/policies.md](governance/policies.md), [governance/data-retention.md](governance/data-retention.md),
> [governance/audit.md](governance/audit.md), [governance/privacy.md](governance/privacy.md),
> [operations/enterprise-admin.md](operations/enterprise-admin.md) and
> [operations/backup-restore.md](operations/backup-restore.md). This page covers the single-device
> baseline that still applies.

## Network exposure

- The backend binds to `127.0.0.1` (`API_HOST`). Docker publishes every port on `127.0.0.1` only.
- Nothing is sent to any external service. The agent talks only to `AGENT_BACKEND_URL`, and
  optionally to LibreHardwareMonitor on localhost. The frontend bundles its own fonts, and its 3D
  lighting is procedural, so the UI loads no CDN assets and works offline.
- CORS is restricted to `CORS_ORIGINS` (`*` is refused in production). The WebSocket checks the
  `Origin` header against the same list, to prevent cross-site WebSocket hijacking.
- nginx (Docker frontend) adds a strict Content-Security-Policy, `X-Frame-Options: DENY`,
  `nosniff` and `Referrer-Policy: no-referrer`. The API adds the same headers plus
  `Cache-Control: no-store`.

## Authentication

| Credential | Used by | Notes |
|---|---|---|
| `AGENT_INGEST_KEY` | agent → `/api/v1/ingest/*` | Required in every mode; constant-time compare; ≥ 24 random chars enforced in production |
| `API_KEYS` | UI / API clients | `AUTH_MODE=api_key`: `X-API-Key` header |
| JWT (HS256, `JWT_SECRET` ≥ 32 chars) | UI / API clients | `POST /api/v1/auth/token` exchanges an API key for a short-lived token (`JWT_TTL_MINUTES`) |

- `AUTH_MODE=none` is for local development only. `APP_ENV=production` refuses to start with it.
- Browsers cannot set headers on WebSockets, so the token travels as a query parameter
  (`/ws/twin?token=`) over the same origin. Use TLS whenever the UI is reached beyond localhost.
- The UI keeps keys and tokens in `sessionStorage` only (cleared when the tab closes).

## Abuse protection and input validation

- Per-client sliding-window rate limit (`RATE_LIMIT_PER_MINUTE`, default 600/min). Agent ingest
  and probes are exempt. For multi-instance deployments, rate-limit at the gateway as well.
- Every request and response uses Pydantic schemas:
  - Metric names must match `^[a-z0-9_]+(\.[a-z0-9_]+)+$`.
  - Batch sizes are bounded (5000 samples, 500 processes).
  - Unknown fields are rejected.
- SQL is parameterised through SQLAlchemy. History queries take metric keys as bound parameters.
- Validation errors never echo submitted values. Unhandled errors return a generic 500 and are
  logged server-side.
- Model files are only served from inside `MODELS_DIR`; path traversal is blocked.

## Privacy

The agent does **not** collect:

- passwords, keystrokes, browser history, documents, screenshots, microphone or webcam
- process command lines, user names, environment variables or window titles (processes are
  reported by image name, PID and resource usage only)
- serial numbers (`INCLUDE_SERIAL_NUMBERS=false`) or MAC addresses (`INCLUDE_MAC_ADDRESSES=false`)
  unless explicitly enabled. Even when collected, the API hides serials unless
  `EXPOSE_SERIAL_NUMBERS=true`.

The device ID is a truncated SHA-256 of manufacturer, model and the SMBIOS UUID hash. It is
stable but does not reveal the identifiers. Structured logs redact credential headers, tokens,
inventory and telemetry payloads.

## Process control

The application is read-only. It never terminates, suspends or signals a process. If process
termination is ever added, it must require explicit per-action user confirmation and an
authenticated principal.

## Secrets

- Secrets come only from environment variables or `.env`, which is git-ignored.
- `.env.example` contains placeholders only.
- Generate keys with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Rotate `AGENT_INGEST_KEY` by updating `.env` and restarting the backend and the agent.

## Production checklist

- [ ] `APP_ENV=production`, `AUTH_MODE=api_key` or `jwt`, strong `API_KEYS` / `JWT_SECRET` / `AGENT_INGEST_KEY`
- [ ] TLS terminator (reverse proxy) in front of the frontend and API; never expose port 8000 directly
- [ ] `POSTGRES_PASSWORD` changed; the database is not published beyond localhost
- [ ] `CORS_ORIGINS` set to the exact UI origin
- [ ] Backups of the PostgreSQL volume
- [ ] Logs shipped to a store with access control
