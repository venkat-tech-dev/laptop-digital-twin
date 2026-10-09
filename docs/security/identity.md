# Identity: sign-in, MFA, SSO and provisioning

## 1. Providers

| Provider | Status | Notes |
|---|---|---|
| Local accounts | available | scrypt password hashes; `local_login_allowed` can be switched off per organization (platform admins keep local login as break-glass) |
| TOTP (authenticator app) | available | needs `DATA_ENCRYPTION_KEY`; RFC 6238, 6 digits, 30 s, ±1 step, each step usable once |
| OIDC | available | authorization code + PKCE (S256); public or confidential client (secret by reference) |
| SAML 2.0 | **metadata only** | the ACS endpoint refuses every assertion (501 `SAML_VALIDATOR_UNAVAILABLE`); no signature validator is bundled and unsigned assertions are never accepted |
| SCIM 2.0 | Users only | per-organization bearer token; Groups not implemented |

Feature switches: `FEATURE_OIDC`, `FEATURE_SAML`, `FEATURE_SCIM` (default on).

## 2. MFA policy (security policy `mfa`)

| Value | Effect |
|---|---|
| `MFA_OPTIONAL` (default) | members may enroll an authenticator; enrolled members must enter a code |
| `MFA_REQUIRED` | local sign-in without an enrolled authenticator gets a restricted `mfa_setup` session (only `/auth/me` and MFA enrolment work); SSO sign-ins must report MFA in `amr`, else 403 `MFA_REQUIRED` |
| `MFA_PROVIDER_MANAGED` | local sign-in is not accepted for MFA; the IdP must assert MFA |

Enrolment: `POST /auth/mfa/totp/setup` returns the secret once (the console shows it once), then
`POST /auth/mfa/totp/activate` with a valid code. Activation ends all sessions of the user. The next
sign-in asks for a code. A code cannot be replayed.

## 3. OIDC sign-in flow

1. The user enters the organization id on the sign-in screen. `GET /auth/providers?organization=<id>`
   lists that organization's active providers.
2. `GET /auth/oidc/{provider}/login` creates `state`, `nonce` and a PKCE verifier (kept server-side,
   10 minutes, single use) and redirects to the IdP's authorization endpoint (from discovery).
3. `GET /auth/oidc/callback` validates the `state`, exchanges the code with the verifier, then validates
   the ID token:
   * signature against the IdP's JWKS, key found by `kid`, refreshed once on an unknown kid;
   * algorithms RS256/RS384/RS512/ES256/ES384/PS256 only (no `none`, no HMAC);
   * `iss` exact, `aud` contains the client id, `exp`, `iat`, 60 s clock skew;
   * `nonce` equal to the stored nonce.
4. Identity mapping: `sub` + issuer, `email` when verified, optional `allowed_domains`. Just-in-time
   membership if `jit_provisioning` is on, with the role from `role_claim` + `role_mapping`, else
   `default_role` (provisionable roles only, never above the configuring admin's level).
5. A session is opened; the browser returns to the same-origin `return_to` with
   `#access_token=…`. The fragment is never sent to a server; the SPA stores it in `sessionStorage` and
   removes it from the address bar and history.

Failures return a stable code (`OIDC_STATE_INVALID`, `OIDC_NONCE_MISMATCH`, `OIDC_TOKEN_INVALID`,
`OIDC_TOKEN_EXPIRED`, `OIDC_UNKNOWN_KEY`, …) and are audited as `auth.oidc_failed`.

Provider configuration accepts only known keys and refuses inline secrets (any key ending in
`secret`, `password` or `key`; a confidential client names its secret with `client_secret_ref`), plain-HTTP issuers (unless `OIDC_ALLOW_HTTP` for local testing), and role mappings
above the creator's level.

## 4. SCIM

* Endpoint `/scim/v2`; `ServiceProviderConfig`, `Users` list (filter `userName eq "…"`), get, create,
  replace, patch (`active`, `roles`), delete.
* The bearer token decides the organization; nothing in the request can select another one.
* `active=false` or DELETE disables the membership and **ends the user's sessions in that organization
  immediately**. Users are never hard-deleted (audit references stay).
* Only provisionable roles (non-administrative) can be set. Administrative members cannot be changed
  through SCIM.
* Tokens are created by identity administrators (`identity.manage`, recent sign-in), shown once and
  stored hashed. Failed SCIM authentication is audited.

## 5. Sessions

See [security-model.md §2](security-model.md#2-sessions-and-tokens). Members can list and end their own
sessions (console → Identity & security). Administrators can end a member's sessions.

## 6. Break-glass

Platform administrators can always sign in locally, even if an organization disables local login, so a
broken IdP configuration cannot lock out every administrator. Protect these accounts with TOTP and keep
their number small.
