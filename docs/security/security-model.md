# Security model (Phase 9)

How the platform authenticates people and devices, decides what they may do, and protects data. Earlier
material stays valid and is summarised here: transport, agent, remediation signing in
[../security.md](../security.md) and [../remediation.md](../remediation.md). Related documents:
[identity.md](identity.md), [device-enrollment.md](device-enrollment.md),
[tenant-isolation.md](tenant-isolation.md), [threat-model.md](threat-model.md),
[baseline-checklist.md](baseline-checklist.md).

Guiding principle: monitor the health and security of enterprise devices **without turning the platform
into employee surveillance software**. See [../governance/privacy.md](../governance/privacy.md).

## 1. Identities

| Identity | Authenticates with | Bound to |
|---|---|---|
| Person (local) | username + password (scrypt) [+ TOTP] | session (one organization) |
| Person (SSO) | OIDC authorization code + PKCE at the organization's IdP | session (that IdP's organization) |
| Provisioning system | SCIM bearer token (hashed, per organization) | one organization; non-admin roles only |
| Device / agent | per-device bearer token (hashed, expiring, rotatable) | one device → one organization |
| Platform administrator | a person with `users.platform_admin` | may act in any organization (audited) |

Legacy modes (`AUTH_MODE=none | api_key | jwt`) remain for single-user local installs. They have no
organizations beyond the default one. Multi-tenancy requires `AUTH_MODE=accounts`.

## 2. Sessions and tokens

* Sign-in creates a row in `user_sessions` and returns an HS256 JWT. The JWT carries `sub`, `sid`, `org`,
  `auth_time`, `amr`, `iat` and `exp`, with lifetime = the organization's `session_ttl_minutes`
  (default 480).
* Every request verifies the signature and then the session row (revoked? expired?). The row is
  re-read at most every 30 s per session. A revoked session therefore stops working within 30 s on every
  replica, and immediately on the replica that revoked it.
* Sessions are revoked by: logout, an organization switch, the member being disabled or changing role or
  scope, SCIM deprovisioning, MFA enrolment (all of the user's sessions), suspension of the organization,
  and an administrator's "end sessions".
* **Secret rotation:** set the new `JWT_SECRET` and move the old value to `JWT_SECRET_PREVIOUS`. Tokens
  signed with either verify; new tokens use the new secret. Remove the previous secret after the longest
  session lifetime has passed.
* Role claims inside a token are ignored: permissions always come from the membership.
* **Recent authentication:** member, role, provider, SCIM-token, publishing of security, retention or
  remediation policy, revoke, retire, data deletion and platform changes need `auth_time` within the
  organization's `reauth_minutes` (default 15). Otherwise the answer is `401 REAUTHENTICATION_REQUIRED`.

## 3. Authorization: RBAC + ABAC

**RBAC:** ten organization roles map to permission sets
(`app/domain/tenancy/permissions.py`):

| Role | Purpose (least privilege) |
|---|---|
| Organization Owner | everything in the organization, including owners |
| Organization Admin | members (up to admin), structure, policy, devices, identity |
| Security Admin | security / identity policy, audit, compliance; **cannot approve or execute remediation** |
| IT Admin | devices, enrollment, groups, remediation approval; no member management |
| IT Operator | day-to-day device operations, remediation request/approve within policy |
| Manager | read fleet and alerts of their scope, request diagnoses |
| Analyst | read telemetry, anomalies, predictions, diagnoses |
| Auditor | read-only plus audit view/export |
| Read Only | read-only |
| Employee / Device User | own assigned device(s) only |

**ABAC** narrows a role:
* **Organization:** always the session's organization.
* **Group scope:** a membership can be limited to device groups. The visible device set then contains
  only devices of those groups.
* **Device ownership:** employees see only devices assigned to them (Phase 3 assignment).
* **Lifecycle:** quarantined devices accept telemetry but no remediation. Disabled, revoked, retired and
  decommissioned devices accept nothing.
* **Policy:** four-eyes threshold, auto-remediation modes and kill switches come from the effective
  remediation policy.

**Escalation guards** (`can_grant`, `set_member`): nobody grants a role above their own level. Nobody
changes their own role. The last owner cannot be removed or demoted. `platform_admin` and `org_id` are
not accepted in member requests (422 extra fields). SCIM and IdP role mapping can only grant
provisionable, non-administrative roles, and a mapping cannot exceed its creator's level.

**Approval ceiling (Phase 8 + 9):** a remediation can be approved only by a person whose role level
covers the action's risk. With four-eyes, never by its requester. Security admins and auditors cannot
approve at all.

## 4. Data protection

| Data | Protection |
|---|---|
| Passwords | scrypt (stdlib), per-user random salt; external (SSO) users have an unusable hash |
| Device, enrollment, SCIM tokens | random 256-bit secrets, stored as SHA-256; shown once |
| TOTP seeds | Fernet-encrypted (AES-128-CBC + HMAC) with `DATA_ENCRYPTION_KEY`; MFA is unavailable without it |
| JWT / encryption keys | environment or files in `SECRETS_DIR` (Docker / Kubernetes secrets); never in the database, logs or API responses |
| IdP configuration | no client secrets accepted (refused with 422); confidential clients reference a secret by name (`client_secret_ref`) |
| Remediation signing key | Ed25519 private key from the secret store (Phase 8) |
| Audit metadata | scrubbed of secret-looking keys; values capped |
| Transport | HTTPS for agents (plain HTTP only to loopback); HSTS optional (`HSTS_ENABLED`) |

Encryption at rest of the database volume is the host's responsibility (BitLocker / LUKS / cloud disk
encryption). The application does not encrypt telemetry columns.

## 5. API hardening

* **Errors:** every error body has a stable `code`, a human `message` and the `request_id`, which is also
  the `X-Request-ID` header. Stack traces are never returned.
* **Validation:** request models forbid unknown fields (`extra="forbid"`), so mass assignment is
  refused with 422. Lengths and ranges are bounded.
* **Rate limits:** per IP (middleware), login and MFA (10/min), enrollment (20/min), per organization
  and per user API limits, per-device ingest bucket, and organization quotas.
* **Headers:** the API sends `X-Content-Type-Options`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: DENY`, `Permissions-Policy` (camera, microphone, geolocation and others disabled) and
  optional HSTS. nginx adds a strict CSP (`default-src 'self'`, `frame-ancestors 'none'`) for the SPA and
  repeats every header for `/assets/`.
* **CORS:** explicit origins only. Methods GET/POST/PUT/PATCH/DELETE; headers include
  `X-Organization-Id`.
* **Redirects:** the OIDC `return_to` accepts same-origin paths only (no `//`, no backslash, no scheme).

## 6. Security events and audit

Authentication success and failure, MFA, sessions, membership and role changes, provider and SCIM
changes, enrollment (including failures), device lifecycle, credential rotation, policy changes, exports,
deletions, authorization denials and cross-tenant attempts are written to the central hash-chained audit.
Quota rejections are counted (metric and the usage dashboard's `rejected_total`) but not audited one by
one, so a flood cannot flood the audit trail. See [../governance/audit.md](../governance/audit.md). Prometheus counters cover the same
events (`ldt_cross_tenant_access_attempts_total`, `ldt_authorization_denials_total`,
`ldt_authentication_failures_total`, `ldt_enrollment_failures_total`, `ldt_quota_rejections_total`,
`ldt_audit_events_total`, `ldt_audit_events_dropped_total`, `ldt_policy_evaluations_total`).

## 7. Non-negotiables carried from earlier phases

* No collection of keystrokes, screenshots, clipboard, passwords, personal files, browser history,
  microphone, webcam or message contents.
* No arbitrary command execution. Remediation remains the signed, allowlisted Phase 8 catalog,
  human-approved, and independently validated by the agent. Phase 9 adds organization kill switches and
  policy, never new execution paths.
* Agent input is untrusted. Telemetry strings never become instructions; the AI does not change
  endpoints.
