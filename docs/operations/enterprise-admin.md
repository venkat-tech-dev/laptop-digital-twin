# Enterprise administration guide

For platform and organization administrators. The console is **Organization** in the sidebar. Tabs
appear according to your permissions; the server checks every action again.

## 1. First setup (platform administrator)

1. Run with `AUTH_MODE=accounts`, a `JWT_SECRET` of at least 32 characters and a `DATA_ENCRYPTION_KEY`
   (Fernet key, `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
   in `.env` or as files in `SECRETS_DIR`. Without `DATA_ENCRYPTION_KEY`, MFA cannot be enrolled.
2. The first visit creates the first account. It owns the **default** organization and is the
   **platform administrator**. Enroll an authenticator for it straight away (Organization → Identity &
   security).
3. Organization → Platform: create organizations (id, name, optional existing owner account), set quotas
   and suspend or re-activate them.

## 2. Organization setup (organization owner or admin)

1. **Members:** add accounts with a role and, optionally, a device-group scope. Use the least privilege
   that works (see [../security/security-model.md](../security/security-model.md#3-authorization-rbac--abac)).
   Owners and admins cannot grant roles above their own.
2. **Structure & groups:** create business units, departments and teams, and device groups (with a
   priority for policy conflicts). Add devices to groups.
3. **Devices & enrollment:** create an enrollment token per laptop (or per batch, if the enrollment
   policy allows multi-use). Install the agent with `AGENT_ENROLLMENT_TOKEN`. Watch the device appear in
   the fleet list with its lifecycle, compliance and agent version.
4. **Policies:** set the security policy (MFA, session length, re-authentication window), agent version
   rules, compliance controls, retention and remediation behaviour. Validate first; read the preview;
   then publish.
5. **Identity & security:** optionally connect an OIDC provider (redirect URI shown in the form) and a
   SCIM client. Keep at least one owner with a local login and TOTP as break-glass.

## 3. Day-to-day

| Task | Where | Needs |
|---|---|---|
| Disable / quarantine a laptop | Devices → select → Disable / Quarantine | `device.manage` |
| Lost or stolen laptop | Devices → select → **Revoke** (credential stops immediately) | `device.remove` + recent sign-in |
| Laptop returned | Retire, then Delete data if required | `device.remove` (+ `retention.manage`) |
| Employee leaves | Members → Disable (sessions end) or deprovision in the IdP (SCIM) | `user.disable` |
| Compromised account | Members → End sessions; reset the password; check the audit | `user.disable` |
| Block an agent version | Policies → Agent → `blocked_versions` (+ `reject_blocked`), validate, publish | `policy.manage` |
| Stop all remediation in the organization | Policies → Remediation → `kill_switch` | `remediation.manage_policy` |
| Investigate | Audit tab: filter by actor, action, result; Verify integrity; export | `audit.view` / `audit.export` |

"Sign in again" errors (`REAUTHENTICATION_REQUIRED`) mean the action is sensitive and your sign-in is
older than the organization's `reauth_minutes`. Sign out and in, then retry.

## 4. Platform operations

* **Secrets:** rotate `JWT_SECRET` by moving the old value to `JWT_SECRET_PREVIOUS` (comma-separated
  list) for one session lifetime. Keep `DATA_ENCRYPTION_KEY` safe and backed up: losing it makes TOTP
  seeds unreadable (users would re-enroll). Prefer `SECRETS_DIR` (one file per secret) over `.env` in
  production.
* **Legacy enrollment:** after re-enrolling every legacy device with tokens, set
  `ALLOW_LEGACY_ENROLLMENT=false`.
* **HTTPS:** behind a TLS-terminating proxy, set `PUBLIC_BASE_URL=https://…` (OIDC redirect URIs) and
  `HSTS_ENABLED=true`.
* **Monitoring:** Prometheus `/metrics` counters for authentication failures, authorization denials,
  cross-tenant attempts, enrollment failures, quota rejections, audit events and drops, active sessions,
  tenants and active devices.
* **Backups:** [backup-restore.md](backup-restore.md).
* **Upgrades:** the backend container runs `alembic upgrade head` at start. Migration 0012 backfills
  the default organization; its downgrade removes the Phase 9 tables. Never delete migration history.
