# Tenant isolation

What keeps organizations apart, and how it is verified. The architecture is in
[../architecture/multi-tenancy.md](../architecture/multi-tenancy.md).

## Guarantees

1. A member only ever acts in the organization of their session. The organization is never taken from
   a request header or body, except `X-Organization-Id` for platform administrators.
2. Device-scoped data (twin, telemetry, history, timeline, anomalies, predictions, alerts, diagnoses,
   remediations, compliance) is reachable only for devices in the caller's visible set. Other ids return
   **404**, the same as ids that do not exist.
3. Organization-owned objects (members, groups, units, tokens, policies, providers, SCIM tokens, audit)
   are always filtered by the session's organization, and modifications check the object's organization.
4. Real-time channels carry only events of visible devices. An event that cannot be attributed to a
   device is not sent (fail closed).
5. Background work (alert routing, notifications, retention, deletion jobs) runs per organization, and
   records carry their owning organization.
6. Platform-wide views (pipeline statistics, alert statistics, workspaces, platform audit, organization
   list) require a platform administrator.
7. Probing another organization's resources is recorded only in the platform audit. Neither the prober's
   nor the target's organization audit shows it, so the audit cannot be used to infer other tenants.

## Verification (automated)

`backend/tests/unit/test_tenant_isolation.py` builds two organizations (acme, globex), each with an owner
and an enrolled device, through the public API. It then checks:

| Test | What it proves |
|---|---|
| `test_every_get_route_is_tenant_scoped` | **Every GET route in the OpenAPI schema**, called by acme's owner with globex's ids substituted for path parameters, returns 403/404 or contains none of globex's identifiers. New routes are covered automatically |
| `test_organization_header_cannot_be_spoofed` | A member sending `X-Organization-Id: globex` is refused |
| `test_writes_on_other_tenant_resources_fail` | Lifecycle, remediation request, compliance, deletion and similar writes on globex's device fail |
| `test_adding_other_tenant_device_to_own_group_fails`, `test_policy_scope_on_other_tenant_device_fails` | Groups and policies cannot reference foreign devices |
| `test_websocket_never_carries_other_tenant_data`, `test_websocket_org_spoof_is_refused` | Subscriptions and broadcasts stay inside the organization |
| `test_notifications_reach_only_the_device_organization` | Alert notifications reach only the device's organization |
| `test_background_records_carry_the_owning_tenant` | Records created by background services carry the owning tenant |
| `test_audit_search_and_export_are_tenant_scoped` | Audit search and export are limited to the organization |
| `test_policy_cache_is_per_tenant` | A cached effective policy of one tenant is never served to another |
| `test_platform_routes_require_platform_admin` | Platform routes are refused to organization owners |
| `test_public_provider_listing_does_not_enumerate_tenants` | Sign-in provider listing needs an organization id and reveals nothing else |

`test_security_phase9.py` adds authentication, enrollment, lifecycle, escalation, quota and audit attacks
(39 tests). Leaks found and fixed while building these suites: pipeline statistics, workspace listing,
provider enumeration, cross-tenant probes visible in the target's audit, and the OIDC open redirect.

## Performance cost

Measured in-process (see [../tenancy-bench-results.json](../tenancy-bench-results.json)): tenancy and
authorization add no measurable latency to a device read compared with the unauthenticated local mode,
and the cost stays flat from 1 to 200 organizations (details in the results file).

## Known limits

Isolation is enforced in application code with a single database (no row-level security). See residual
risks RR1 to RR4 in [threat-model.md](threat-model.md).
