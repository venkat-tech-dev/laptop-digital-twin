# Phase 9 audit: the platform before multi-tenancy

This audit covers the backend (FastAPI, PostgreSQL/TimescaleDB, Redis), the WebSocket layer, the Windows
agent, the React frontend and the Docker deployment. It was done before any Phase 9 code. Its findings
drive the implementation; each finding is marked **fixed**, **mitigated** or **open** at the end of
Phase 9 in [`multi-tenancy.md`](multi-tenancy.md) and [`../security/security-model.md`](../security/security-model.md).

## 1. Current architecture (Phases 1–8)

```
Windows agent ──HTTPS/JSON──> ingest API ──> DigitalTwinService (in memory) ──> EventBus ──> WebSocket fan-out
      │  (enrollment key or            │            │                              │
      │   per-device token)            │            ├─> TwinEngine (documents, Redis snapshot)
      │                                │            ├─> Intelligence (anomalies, baselines)
      │                                │            ├─> Forecasts ─> predictions
      │                                │            ├─> AlertService ─> notifications (in-app, browser, toast, mail, webhook)
      │                                │            ├─> DiagnosisService (rules / local LLM)
      │                                │            └─> RemediationService (signed envelopes back to the agent)
      │                                └─> PostgreSQL (samples, events, anomalies, predictions, alerts, ...)
      └── pulls config, notifications and signed actions
```

It runs as one backend process: background loops in the container, no external queue.

## 2. Authentication

**Modes** (`AUTH_MODE`):
- `none`: everyone is an anonymous admin (development only; refused in production).
- `api_key`: shared keys, every key an admin.
- `jwt`: HS256 tokens minted from an API key.
- `accounts`: local users with scrypt password hashes; HS256 JWT with `sub`, `role` and `exp`.

What works well:
- Accounts are re-read on every request, so disabling a user or changing a role applies immediately.
- WebSocket authentication takes a token in the query string, with origin checks.

Gaps:
- No session store: logout cannot revoke a token, and there is no session list.
- No `auth_time`, so "recent authentication" cannot be required.
- No MFA, OIDC, SAML or SCIM.
- One HS256 secret, with no rotation support (previous keys are not accepted).
- API keys are global administrators with no organisation.

## 3. Authorization

Four fixed roles, compared by rank (`employee < viewer < operator < admin`):
- `Reader`, `Operator`, `Admin` and `Staff` dependencies: 75, 13, 27 and 18 uses.
- `has_role` in 9 places.
- Phase 8 added a permission table for remediation only.

Device-level authorization exists, and **every device-scoped route and WebSocket subscription goes through `api/access.py`**:
- `check_device`, `visible_devices`, `scoped_device`.
- Employees see only assigned devices. Every other role sees **all** devices (`visible_devices` returns `None`).

There is no resource scope (device groups) and no ABAC.

## 4. Data ownership and tenant assumptions

- **Single organisation everywhere:**
  - `devices` has no owner.
  - `tenant_id="default"` is a constant in AlertService, DiagnosisService and RemediationService.
  - "Workspaces" are departments of the one organisation.
  - The *primary device* is platform-wide.

**Ownership matrix** (Phase 9 target in the right column):

| Table | Today | Phase 9 ownership |
|---|---|---|
| devices | global | TENANT_OWNED (`organization_id`) |
| hardware_components, telemetry_metrics, telemetry_samples, health_events, anomalies, device_baselines, anomaly_models, predictions, system_events, ingest_receipts, device_credentials, device_assignments, anomaly_acknowledgements | device | DEVICE_OWNED (tenant through `devices.organization_id`, FK or device id) |
| alerts, alert_audit | "default" | TENANT_OWNED (`tenant_id` = device's organisation) + DEVICE_OWNED |
| notifications, notification_preferences | user | USER_OWNED (recipients restricted to the device's organisation) |
| diagnoses, diagnosis_feedback | "default" | TENANT_OWNED (`tenant_id` = device's organisation) |
| remediations, remediation_audit | "default" | TENANT_OWNED (`tenant_id` = device's organisation) |
| users | global | GLOBAL identity; access through `organization_members` |
| workspaces, workspace_devices | global | TENANT_OWNED (department of an organisation) |
| app_settings | global | PLATFORM_ADMIN_ONLY (platform defaults); organisation settings move to `policies` |
| *new:* organizations, org_units, device_groups, organization_members, enrollment_tokens, policies, audit_events, user_sessions, identity_providers, user_mfa, scim_tokens | — | TENANT_OWNED, except platform administration |

Telemetry is not given its own `organization_id` column (TimescaleDB hypertable). Ownership comes from the device, and every query is device-scoped. Device ids are hardware-derived and globally unique.

## 5. Agent and enrollment

- **Enrollment key:** one global `AGENT_INGEST_KEY` (environment). It is permanent and shared by every device: a stolen key enrolls a device anywhere.
- **Per-device tokens:** issued by `/agent/register` and stored only as a SHA-256 hash. They are revocable, but have no expiry or rotation.
- **Agent storage:** the token is protected with DPAPI.
- **Device states:** only presence (ONLINE / STALE / OFFLINE). No lifecycle states (disabled, revoked, retired).

## 6. Audit, logging and governance

- **Per-domain trails:**
  - `alert_audit`;
  - `remediation_audit` (hash-chained, append-only trigger);
  - `system_events` (device timeline).
- **Not audited:** logins, failed logins, user and role changes, settings changes, exports.
- **Logging:** structured (structlog) with request ids. Telemetry strings and secrets are not logged (checked in Phases 1–8).
- **Retention:** global only (raw/aggregate samples, events, diagnoses, notifications). No per-organisation retention, and no controlled deletion workflow.

## 7. API and transport security

- **Rate limiting:** per-IP sliding window (middleware) and a per-device token bucket for ingestion. No per-user, per-organisation or quota limits.
- **Headers:** `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` and `Cache-Control` from the backend; nginx adds a CSP.
  - nginx's `location /assets/` uses `add_header`, which **drops the inherited security headers** for assets (nginx inheritance rule).
  - There is no `Permissions-Policy`, and no HSTS (TLS terminates outside this stack).
- **Errors:** `{"detail": ...}` without a code or request id. Validation errors do not echo input (Phase 2). Database errors are mapped to 503/500 without internals.
- **CORS:** explicit origins only; `*` is refused in production.

## 8. WebSocket

- Connections are authenticated.
- Employees are restricted per connection.
- Staff clients receive every device.
- Messages without `device_id` (heartbeat with the platform-wide primary device id, system events) go to **every** client, which is a cross-tenant leak once there are tenants.

## 9. Background jobs and caches

- **Jobs:** forecasting, intelligence, alerting, diagnosis and remediation loops are keyed by device id. They are tenant-agnostic, but each acts only on its own device's data.
- **Alert recipients:** **all users** of the platform (by role and assignment). This becomes cross-tenant notification delivery once there are tenants.
- **Redis:** twin documents are keyed `twin:<device>`. In-process caches are per device.

## 10. Deployment and secrets

- **Images:** non-root users (backend uid 10001, nginx-unprivileged). Ports are published on 127.0.0.1. Health checks are present.
- **Secrets:** in `.env` (gitignored, including `.env.bak*`; the repository has no commits). No secrets were found in source.
- **Database:** one database user (`ldt`) for migrations and runtime, which is not least privilege.
- **Resource limits:** no container memory or CPU limits.
- **Dependencies:** small and current: FastAPI, SQLAlchemy 2, asyncpg, redis, structlog, httpx, prometheus-client, pyjwt, tzdata, cryptography (Phase 8).

## 11. Risks found (before Phase 9)

| # | Severity | Finding |
|---|---|---|
| R1 | High (once multi-tenant) | Every non-employee role sees every device; there is no tenant boundary |
| R2 | High (once multi-tenant) | Alert notifications are routed to all users of the platform |
| R3 | High (once multi-tenant) | WebSocket messages without `device_id` reach every client; the platform-wide primary device leaks |
| R4 | High | A global, permanent enrollment key enrolls any device |
| R5 | Medium | JWTs cannot be revoked (no session store); no recent-authentication or MFA |
| R6 | Medium | No central audit of authentication and administration |
| R7 | Medium | No per-user, per-tenant rate limits or quotas (noisy neighbour) |
| R8 | Medium | Device credentials never expire and cannot be rotated |
| R9 | Low | nginx `/assets/` drops security headers; no `Permissions-Policy` |
| R10 | Low | One database credential for migrations and runtime |
| R11 | Low | Error bodies lack machine-readable codes and request ids |

## 12. Migration and compatibility strategy

- **Default organisation:** one organisation, `default`, is created. All existing devices, workspaces and users are attached to it, so the deployment behaves exactly as before.
- **Additive, reversible schema:** new tables, plus nullable or backfilled columns. No column is removed.
- **Roles:**
  - Existing roles map to organisation roles: admin → `org_admin`, operator → `it_operator`, viewer → `read_only`, employee → `employee`.
  - Existing administrators become owners of `default`.
  - The account that ran the first-time setup becomes the platform super-admin.
- **Legacy compatibility:** rank checks (`Admin`, `Operator`, `Staff`) keep working through a rank derived from the organisation role. New and sensitive routes check permissions.
- **Legacy enrollment:** the enrollment key keeps working, but only into the `default` organisation, and it can be switched off (`ALLOW_LEGACY_ENROLLMENT`). New devices enroll with organisation-issued, expiring, single-use tokens.
- **Device scoping:** `visible_devices` always returns an explicit set: the organisation's devices, narrowed by group scope and employee assignment. It never returns "all devices".
