# Threat model (Phase 9)

STRIDE-oriented review of the multi-tenant platform. Each threat lists its mitigation and the evidence:
a test in `backend/tests/unit/` or a measurement. Residual risks are stated plainly.

## 1. Assets

1. Tenant data: device telemetry, twin state, alerts, diagnoses, remediations, inventory, audit.
2. Control: the ability to request or approve remediation on endpoints (Phase 8 signed actions).
3. Identity: passwords, sessions, MFA seeds, device credentials, enrollment and SCIM tokens, signing
   keys.
4. Governance: policies, audit trail integrity, retention and deletion decisions.
5. Employee privacy: the platform must not become a surveillance tool.

## 2. Actors and trust boundaries

```
 Browser (member) ──HTTPS──▶ nginx ──▶ API (FastAPI) ──▶ PostgreSQL / Redis
 Agent (device)  ──HTTPS──▶           API ingest
 IdP (OIDC)      ◀─redirect─▶ API     SCIM client ──HTTPS──▶ API /scim/v2
 Platform admin (a member with platform_admin)
```

Untrusted: everything a browser, agent, IdP response or SCIM client sends. That includes tenant ids,
roles, device ids, telemetry strings and ID-token claims until validated. Trusted: the server's session
store, membership table, device registry, and the secrets in the environment or `SECRETS_DIR`.

Adversaries considered: a member of tenant A attacking tenant B; a malicious or compromised member inside
a tenant (escalation); a stolen device or agent credential; an attacker on the network; a malicious IdP
response or replayed callback; a noisy (not malicious) tenant; an insider with database access (detection
only).

## 3. Threats

| # | Threat (STRIDE) | Mitigation | Evidence |
|---|---|---|---|
| T1 | **Cross-tenant read** by changing a device id, path or query (I) | Visible-device set + `check_device` on every device route; 404 for foreign ids; org filter on org-owned rows | `test_tenant_isolation.py` sweeps every GET route in the OpenAPI schema with tenant B's ids |
| T2 | **Tenant id spoofing** via `X-Organization-Id` or a body field (S/E) | Org comes from the session; header honoured only for platform admins; bodies forbid unknown fields | ``test_organization_header_cannot_be_spoofed``, mass-assignment test |
| T3 | **Cross-tenant write** (approve or request remediation, change lifecycle, add to group, policy scope on a foreign device or group) (T/E) | Same device/group checks on writes; policy scopes validated against the organization | isolation write tests, group/policy scope tests |
| T4 | **Existence inference** (timing or status differences, provider enumeration, audit of probes) (I) | 404 for "not yours"; `/auth/providers` needs an organization id and lists only that one; probes are audited at platform level, not in the victim's or attacker's org audit | provider enumeration test, `test_audit_records_security_events…` |
| T5 | **WebSocket leakage** (subscribe to a foreign device; global events) (I) | Subscription limited to the visible set; events without a device id are dropped (fail closed); per-client primary device | WS isolation tests |
| T6 | **Notification leakage** (alert for B's device reaching A's users) (I) | Recipients resolved per organization and visibility | notification isolation test |
| T7 | **Token forgery or manipulation** (alg none, wrong key, edited claims, expired) (S) | HS256 with required claims (`exp`, `iat`, `sub`, `sid`); `none` refused; role claims ignored | `test_jwt_manipulation_is_rejected`, `test_role_claim_in_token_is_not_trusted` |
| T8 | **Session theft or reuse after logout or removal** (S) | Server-side sessions, revocation on logout / role change / disable / SCIM / suspension; 30 s recheck | `test_logout_and_revocation…`, SCIM deprovision test |
| T9 | **Credential stuffing / brute force** (S) | Login 10/min per client; generic failure message; audit + metric; MFA available, or required by policy | login rate-limit behaviour, MFA tests |
| T10 | **MFA bypass / code replay** (S) | TOTP with replay protection (a step can be used once); a `mfa_setup`-scoped session can only enroll; `MFA_REQUIRED` enforced for SSO via `amr` | `test_local_mfa_enrolment_login_and_replay`, `test_mfa_required_policy…`, `test_oidc_unknown_state_and_mfa_policy` |
| T11 | **OIDC attacks** (CSRF/state, nonce replay, issuer or audience confusion, alg none / HS256 key confusion, unknown key, expired token, open redirect) (S/T) | PKCE S256, single-use state, nonce, strict iss/aud, RS/ES allowlist, JWKS by kid with refresh, 60 s skew, same-origin `return_to` | `test_oidc_rejects_invalid_id_tokens` (8 cases), `test_oidc_return_to_cannot_leave_the_site` (5 cases) |
| T12 | **SAML assertion forgery** (S) | ACS refuses all assertions (501) until a signature-validating library is installed; never "accept unsigned" | `test_saml_assertions_are_refused_not_skipped` |
| T13 | **Privilege escalation inside a tenant** (grant above own level, self-promotion, last-owner removal, IdP or SCIM mapping to admin) (E) | `can_grant`, self-change and last-owner guards; SCIM and mappings limited to provisionable roles | `test_privilege_escalation_and_mass_assignment`, `test_provider_config_cannot_hold_secrets_or_escalate`, SCIM tests |
| T14 | **Stale privileged session** doing sensitive admin (E) | Recent authentication (`reauth_minutes`) for sensitive changes | `test_recent_authentication_required…` |
| T15 | **Rogue device enrollment** (guessing, reuse, expired or revoked token, takeover of another tenant's device id) (S) | 256-bit single-use expiring tokens stored hashed; atomic consumption; device owned elsewhere → 409; enrollment rate limit; device quota | `test_enrollment_token_abuse`, `test_device_quota_rejects_enrollment` |
| T16 | **Stolen device credential** (S) | Per-device tokens, expiry (policy, default 90 days), rotation, revocation on revoke/retire; the shared legacy key cannot re-register a revoked device | `test_device_lifecycle_enforcement`, `test_credential_rotation` |
| T17 | **Telemetry injection** (prompt injection, oversize, forged device id) (T) | Phase 2 validation; token bound to its device; Phase 7 treats telemetry as data only | Phase 2/7 suites |
| T18 | **Unauthorized remediation** (D/E) | Phase 8 signed catalog, human approval, ceiling, four-eyes, agent-side validation; quarantine blocks actions; organization kill switch | `test_remediation_four_eyes_follows_organization_policy`, lifecycle test, Phase 8 suites |
| T19 | **Noisy neighbour / resource exhaustion** (D) | Per-organization and per-user rate limits and quotas; agent backs off on 429 | `test_noisy_tenant_is_throttled…`; [tenancy bench](../tenancy-bench-results.json) |
| T20 | **Audit tampering or repudiation** (R/T) | Append-only (DB trigger), hash chain, verification endpoint; secrets scrubbed | `test_audit_records_security_events_and_is_tamper_evident`; migration test confirms the trigger (also after restore) |
| T21 | **CSV / formula injection in exports** (T) | Cells starting with `= + - @` (and tab/CR) are prefixed with `'` | `test_audit_export_neutralises_formula_injection` |
| T22 | **Secret disclosure** (keys in logs, responses, provider configs) (I) | Secret store; log redaction; tokens shown once; provider configs refuse secrets | provider config test; logging redaction (Phase 1) |
| T23 | **Clickjacking / XSS impact** (T) | CSP `default-src 'self'`, `frame-ancestors 'none'`, `X-Frame-Options: DENY`; React escaping; OIDC token in the URL fragment only | nginx config; `org.test.tsx` fragment handling |
| T24 | **Over-collection / surveillance** (I, privacy) | Fixed collector set; no content collection; process names per policy; see privacy | [privacy.md](../governance/privacy.md) |

## 4. Residual risks (accepted or open)

| # | Risk | Status |
|---|---|---|
| RR1 | Isolation is logical (application code), not row-level security or a database per tenant. A future code path that forgets `check_device` would leak | Mitigated by the route sweep test, which fails for any new GET route that leaks. RLS is the recommended next step |
| RR2 | Quota and rate windows are per backend process; N replicas allow up to N× the limit | Open; a Redis-backed window is the follow-up |
| RR3 | One database credential for migrations and runtime (R10) | Open; recommendation: a migration role owning the schema and a runtime role with DML only (no DDL, no `ALTER TABLE … DISABLE TRIGGER`). Until then a database-level attacker could disable the audit trigger. The hash chain still detects later edits unless the whole chain is rewritten |
| RR4 | Redis has no password and Postgres keeps default capabilities; both are bound to loopback only | Accepted for single-host deployment; add `requirepass` and network policies when hosts are shared |
| RR5 | SAML login unavailable (refused) | By design until a validator dependency is approved |
| RR6 | Session revocation can take up to 30 s on other replicas | Accepted (documented) |
| RR7 | The audit buffer flushes asynchronously; a crash can lose the last ≤ 1 s of events | Counted in `ldt_audit_events_dropped_total`; accepted |
| RR8 | The JWT is stored in `sessionStorage` (readable by script in an XSS) | Mitigated by the strict CSP and short sessions; HttpOnly cookies would need CSRF protection and are a possible follow-up |
| RR9 | Encryption at rest depends on the host disk | Documented in the deployment checklist |

## 5. Review cadence

Re-run this review when a route, a role, an identity integration, an execution capability or a data
category is added. `test_tenant_isolation.py::test_every_get_route…` must stay green. A new route that
needs an exception must be added explicitly, with a reason.
