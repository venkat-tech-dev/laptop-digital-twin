# Security baseline checklist (deployment)

Tick every item before onboarding a second organization or exposing the platform beyond one machine.
"Default" is the behaviour of this repository's Docker deployment.

## Identity and access

- [ ] `AUTH_MODE=accounts` (multi-tenancy needs it; default `none` is single-user local mode)
- [ ] `JWT_SECRET` ≥ 32 random characters; rotation plan with `JWT_SECRET_PREVIOUS`
- [ ] `DATA_ENCRYPTION_KEY` set (MFA) and backed up separately from database dumps
- [ ] Platform administrator(s) have TOTP enrolled; keep them few
- [ ] Security policy per organization: `mfa` ≥ `MFA_REQUIRED` for administrators' organizations, `session_ttl_minutes` ≤ 480, `reauth_minutes` ≤ 15
- [ ] At least one break-glass owner with local login + TOTP when SSO is enforced
- [ ] OIDC providers use HTTPS issuers (`OIDC_ALLOW_HTTP=false`) and `allowed_domains` where possible
- [ ] Members reviewed quarterly (security dashboard: inactive members, MFA adoption)

## Devices

- [ ] Devices enroll with tokens; `ALLOW_LEGACY_ENROLLMENT=false` once no legacy devices remain
- [ ] `ALLOW_ENROLLMENT_KEY_INGEST=false` (default): telemetry only with per-device credentials
- [ ] Enrollment policy: single-use tokens, `token_max_ttl_hours` ≤ 24, `credential_ttl_days` ≤ 90
- [ ] Agent policy: `minimum_version` set; vulnerable versions in `blocked_versions` with `reject_blocked`
- [ ] Agent runs as the Windows service; data directory ACL limited to SYSTEM and Administrators

## Network and transport

- [ ] TLS in front of the API and console; `PUBLIC_BASE_URL=https://…`; `HSTS_ENABLED=true`
- [ ] `CORS_ORIGINS` lists only the console origin(s)
- [ ] Postgres and Redis not reachable from other hosts (default: bound to 127.0.0.1)
- [ ] Redis `requirepass` and Postgres network policy if the host is shared (threat model RR4)

## Data

- [ ] Disk encryption on the database host (BitLocker / LUKS / cloud volume encryption)
- [ ] Retention policies at the shortest period that fits operations
- [ ] Daily backups, encrypted, off-host, restore tested ([../operations/backup-restore.md](../operations/backup-restore.md))
- [ ] Privacy notice to employees ([../governance/privacy.md](../governance/privacy.md)); process-name settings chosen deliberately

## Remediation (Phase 8)

- [ ] `REMEDIATION_SIGNING_KEY` from the secret store; agents pinned to it
- [ ] Automated remediation off unless deliberately enabled per organization
- [ ] Four-eyes threshold set (`four_eyes_min_risk`)
- [ ] Kill switches known to on-call staff

## Monitoring and audit

- [ ] Prometheus scraping `/metrics`; alerts on `ldt_cross_tenant_access_attempts_total`, `ldt_authentication_failures_total` spikes, `ldt_audit_events_dropped_total` > 0
- [ ] Audit integrity verified weekly (console or API); chain head exported off-host
- [ ] Logs shipped without secrets (redaction is built in; verify after changing log settings)

## Containers

- [ ] Images rebuilt monthly for base-image security fixes
- [ ] Backend and frontend run non-root, `no-new-privileges`, all capabilities dropped, memory and PID limits (default in `docker-compose.yml`)
- [ ] Separate database roles for migrations and runtime (threat model RR3; not yet default)
