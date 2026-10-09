# Multi-tenancy architecture (Phase 9)

This document describes how the platform separates organizations (tenants). It covers the data model, how
a request is bound to one organization, and where isolation is enforced. Read
[phase-9-audit.md](phase-9-audit.md) for the state before Phase 9, and
[../security/tenant-isolation.md](../security/tenant-isolation.md) for the tests that prove the boundary.

Rule: a tenant must never be able to observe, modify, infer, subscribe to or operate on another tenant's
data or resources.

## 1. Model

```
Platform
└── Organization (tenant)            status ACTIVE | SUSPENDED | ARCHIVED, quotas
    ├── Business unit → Department → Team     (org_units, optional hierarchy)
    ├── Device group                          (kind, priority, optional unit; many-to-many with devices)
    ├── Membership (user × organization)      role, group scope, status, source (local / oidc / scim)
    ├── Device registry entry                 lifecycle, enrollment, groups
    ├── Enrollment tokens, SCIM tokens, identity providers
    ├── Policies (versioned)                  per scope: organization … device
    └── Audit events (hash chain, shared table, org column)
```

* **Users are platform-wide; memberships are per organization.** One account can belong to several
  organizations, with a different role in each. A session is always bound to exactly one organization.
* **Devices belong to exactly one organization** (`device_registry.org_id`). Ownership is decided when the
  device enrolls with a token. Telemetry, twin state, anomalies, predictions, alerts, diagnoses and
  remediations stay keyed by `device_id`. Their tenant is the device's organization, resolved on every
  access. This "device-owned" design avoids duplicating an organization column on high-volume tables. It
  makes the device check (`check_device`) the single gate for device-scoped data.
* **The default organization** holds everything that existed before Phase 9. The migration backfilled
  it: users became members with mapped roles, and devices became `ACTIVE` registry entries marked
  `legacy`. The earliest administrator became the platform administrator.

## 2. Request binding: the TenantContext

Every authenticated request resolves a `Principal` (`app/core/security.py`, `app/api/deps.py`):

| Field | Source | Trusted from client? |
|---|---|---|
| `subject` | verified JWT `sub` | no (signature-verified) |
| `session_id` | JWT `sid`, checked against `user_sessions` (revocation, expiry) | no |
| `org_id` | the session's organization; for platform administrators an `X-Organization-Id` header is honoured | **never** for members |
| `org_role`, `permissions`, `group_scope` | the membership row, loaded server-side | never (role claims in a token are ignored) |
| `platform_admin` | `users.platform_admin` | never |
| `auth_time`, `mfa` | JWT claims set at sign-in | signature-verified |

A member who sends `X-Organization-Id` for another organization is refused. A member switches
organization with `POST /api/v1/auth/switch-organization`, which checks the membership, issues a new
session and revokes the old one. `api/access.py` turns the principal into the explicit set of visible
devices: all of the organization's devices, or only those of the member's groups when the role is
group-scoped. Every device-scoped route checks membership of that set. A miss is answered with **404**,
not 403, so no existence is confirmed, and is counted and audited as a cross-tenant attempt in the
platform audit.

## 3. Enforcement points

| Layer | Mechanism |
|---|---|
| API routes | `require_permission(...)` (RBAC), `check_device` / `visible_devices` (ABAC scope), `require_recent_auth` for sensitive changes, `require_platform_scope` for platform-wide data |
| Service layer | services receive a `TenantContext`; tenancy, policy, identity and governance operations validate the organization of every object they touch |
| Database | queries for organization-owned rows always filter by `org_id`; device-owned rows are reached only through a device id that passed `check_device`. No query takes an organization id from the request body |
| Cache | the policy cache is keyed by (kind, org, device); twin documents are per device and reached only after the device check |
| WebSocket | the connection resolves the same principal (`?token=`, optional `?org=` for platform admins); subscriptions are limited to the visible set; events without a device id are dropped (fail closed); per-organization connection quota |
| Background jobs | alert routing, notifications, retention and deletion iterate per organization; recipients come from `tenancy.recipients(org, device)` |
| Agent ingest | the device credential identifies the device, and its registry entry gives the organization; lifecycle, organization status, blocked versions and the organization's telemetry quota are checked before data is accepted |

## 4. Policy resolution

Policies are versioned documents of one kind (remediation, diagnosis, notifications, security, agent,
enrollment, retention, compliance), attached to one scope. The effective value of each field is resolved
deterministically, from weakest to strongest:

```
platform default < organization < business unit < department < team < device group < device
```

* Several groups: the group with the **lower priority number wins**, then the group id breaks the tie.
* A field **locked** at organization level cannot be overridden lower down. Publication of a conflicting
  draft is refused, and the resolver ignores it as a second line of defence.
* Every effective value carries its provenance, for example `organization:acme@v3`. The console and
  `GET /org/policies/effective` show it.
* Security, enrollment and retention policies are organization-wide only.

The Phase 8 remediation engine reads the organization's effective remediation policy. Values the
organization sets override the platform policy. Platform defaults never weaken the Phase 8
configuration, and the platform kill switch and the organization kill switch both apply.

## 5. Quotas and noisy neighbours

Per organization (defaults in `QUOTA_DEFAULTS`, changed by platform administrators):

| Quota | Default | Mode when exceeded |
|---|---|---|
| devices | 1000 | REJECT (enrollment refused) |
| active members | 500 | REJECT |
| telemetry batches / min | 12,000 | THROTTLE: 429 + Retry-After; the agent keeps the batch queued |
| API requests / min (org) | 6000 | THROTTLE |
| API requests / min (user) | 1200 | THROTTLE |
| WebSocket connections | 500 | REJECT |
| diagnosis jobs / hour | 600 | THROTTLE |
| remediation requests / day | 2000 | REJECT |
| exports / hour | 30 | THROTTLE |

Windows are in-process sliding windows per backend instance. With several backend replicas the
effective limit is per replica; a shared (Redis) window is a documented follow-up, not implemented.
Measured isolation is in [../tenancy-bench-results.json](../tenancy-bench-results.json).

## 6. What is deliberately not separated

* **Infrastructure:** one database, one Redis, one backend process pool. Isolation is logical, enforced
  in code and tested, not physical (no database per tenant, no row-level security). This suits the
  current scale. Row-level security is the next hardening step if the database is ever exposed to other
  clients.
* **Users** are shared across organizations by design (one person, several memberships).
* **Platform administrators** can act in any organization (`X-Organization-Id`). Every such action is
  audited under that organization, with the administrator as actor.
