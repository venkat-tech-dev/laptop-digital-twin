# Audit trail

One central, append-only, hash-chained record of security and governance events for every organization
(table `audit_events`). The Phase 8 remediation audit (`remediation_audit`) keeps its own chain for the
fine-grained action lifecycle; both are verifiable.

## 1. Event

| Field | Meaning |
|---|---|
| `event_id`, `at` | unique id, UTC time |
| `org_id` | owning organization; **null** for platform-level events (failed login of an unknown user, cross-tenant probes, platform changes) |
| `actor_id`, `actor_type` | who: user, agent, system, scim, api_key, anonymous |
| `action`, `category` | for example `auth.login` / authentication, `user.role_changed` / user, `device.revoked` / device, `policy.published` / policy, `data.exported` / data, `security.cross_tenant_attempt` / security |
| `resource_type`, `resource_id` | the object acted on |
| `result`, `reason` | SUCCESS / FAILURE / DENIED and why |
| `severity` | INFO / WARNING / HIGH |
| `source`, `request_id`, `ip` | api, websocket, agent, worker, scim, oidc; correlation id; client address |
| `metadata` | small structured detail, **scrubbed** of keys that look secret (`password`, `token`, `secret`, `key`, `assertion`, `code_verifier`, `otp`, `authorization`) and size-capped |
| `prev_hash`, `hash` | SHA-256 chain: `hash = sha256(prev_hash + "|" + canonical JSON of the event)` |

Never recorded: passwords, tokens, OTP codes, secrets, telemetry values, process lists, personal content.

## 2. Integrity

* **Append-only:** a database trigger (`audit_events_no_change`) refuses UPDATE and DELETE. It survives
  backup and restore (verified).
* **Hash chain:** every event links to the previous one. `GET /org/audit/verify` (console: "Verify
  integrity") recomputes the chain and reports the first broken row. `scripts/backup_restore.py verify`
  does the same on a restored copy.
* **Ordering:** events are buffered in memory and appended in batches every second under a PostgreSQL
  advisory lock, so concurrent backends cannot interleave a broken chain. An event lost in a crash
  (≤ 1 s of buffer) is counted in `ldt_audit_events_dropped_total`.
* Limits: an attacker with database owner rights could disable the trigger and rewrite the whole chain.
  Separating the migration and runtime database roles (threat model RR3) closes this. Exporting the
  chain head regularly to external storage detects it.

## 3. Search and export

* `GET /org/audit` (`audit.view`): filters for actor, action prefix, category, resource, result,
  severity and time range; newest first; cursor pagination (`before_id`); up to 500 per page.
* `GET /org/audit/export?fmt=csv|json` (`audit.export`): up to 50,000 rows, quota `exports_per_hour`.
  The export itself is audited (`data.exported`). In CSV, cells beginning with `=`, `+`, `-`, `@`, tab
  or carriage return are prefixed with `'` (spreadsheet formula injection).
* `GET /platform/audit` (platform administrators): every organization plus platform events, including
  cross-tenant probes that neither the prober's nor the target's organization can see.

## 4. Main actions recorded

| Area | Actions |
|---|---|
| Authentication | `auth.login`, `auth.login_failed`, `auth.failed`, `auth.logout`, `auth.oidc_failed`, `auth.saml_refused`, `auth.scim_failed`, `auth.mfa_enrolled`, `auth.session_revoked`, `auth.sessions_revoked` |
| Users | `user.setup`, `user.created`, `user.updated`, `user.deleted`, `user.member_added`, `user.role_changed` (role, scope and status changes), `user.provisioned`, `user.deprovisioned` |
| Organization | `organization.created`, `organization.updated` (rename, status, quotas), `organization.unit_created`, `organization.unit_archived`, `group.saved`, `group.membership_changed` |
| Devices | `device.registered`, `device.enrolled`, `device.enrollment_failed`, `device.enrollment_token_created`, `device.enrollment_token_revoked`, `device.<lifecycle>` (`device.disabled`, `device.quarantined`, `device.revoked`, `device.retired`, `device.active`, `device.decommissioned`), `device.credential_rotated` |
| Policy | `policy.draft_saved`, `policy.published`, `policy.rolled_back`, `policy.archived` |
| Identity | `identity.provider_added`, `identity.provider_status`, `identity.scim_token_created`, `identity.scim_token_revoked` |
| Data | `data.exported`, `data.deletion_requested`, `data.deleted`, `data.deletion_failed`, `data.retention_applied` |
| Security | `security.cross_tenant_attempt` (platform level only), `security.authorization_denied` |

## 5. Retention

Audit events are not removed by retention or data deletion. Archiving them to cold storage is an operator
task (export JSON, keep with the chain head hash).
