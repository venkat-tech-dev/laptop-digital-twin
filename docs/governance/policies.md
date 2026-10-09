# Policies

Organizations control platform behaviour through versioned policies. A policy is one **kind** attached to
one **scope**, and has a lifecycle: draft → validate → publish → (archive | roll back).

## 1. Kinds and fields

| Kind | Fields (default) |
|---|---|
| remediation | `auto_remediation_enabled` (false), `auto_min_confidence` (0.75), `low_mode` / `medium_mode` / `high_mode` (MANUAL_APPROVAL), `critical_mode` (DISABLED), `four_eyes_min_risk` (HIGH), `approval_ttl_s` (1800), `kill_switch` (false) |
| diagnosis | `enabled` (true), `auto_min_severity` (HIGH) |
| notifications | `max_per_user_hour` (30), `allowed_channels` (in_app, browser, windows, email, webhook) |
| security *(org only)* | `mfa` (MFA_OPTIONAL), `session_ttl_minutes` (480), `reauth_minutes` (15), `local_login_allowed` (true) |
| agent | `minimum_version` (1.4.0), `recommended_version` (1.6.0), `deprecated_versions`, `blocked_versions`, `reject_blocked` (false), `telemetry_interval_ms` (5000) |
| enrollment *(org only)* | `token_max_ttl_hours` (24), `allow_multi_use` (false), `credential_ttl_days` (90) |
| retention *(org only)* | `raw_telemetry_days` (30), `diagnoses_days` (90), `notifications_days` (90), `alerts_days` (365) |
| compliance | `required_controls` (antivirus, realtime_protection, firewall), `max_telemetry_age_s` (900), `exempt` (false) |

Ranges and choices are enforced by `POST /org/policies/{id}/validate` and again at publication
(`GET /org/policies/schema` lists them).

## 2. Scopes and precedence

```
platform default < organization < business unit < department < team < device group < device
```

The strongest scope that sets a field wins. For devices in several groups, the group with the lowest
priority number wins, then the group id breaks the tie (deterministic). Fields **locked** at organization
level cannot be overridden: a conflicting draft fails validation, and the resolver ignores it anyway.
Unset fields are inherited. The console's "In effect" panel and `GET /org/policies/effective?kind=…
[&device_id=…]` show every value with its source (for example `device_group:<id>@v2`).

## 3. Lifecycle

1. **Draft:** `POST /org/policies` creates a policy, or a new draft version of the policy at that scope.
   Saving again edits the open draft.
2. **Validate:** returns errors (blocking), warnings (for example "CRITICAL actions become approvable:
   only organization administrators can approve them", or "devices on blocked versions will stop sending
   telemetry until upgraded") and a **preview**: devices affected, the values that change from → to, and
   for agent policies the devices on newly blocked versions.
3. **Publish:** only a valid draft of the current version. The previous published version is archived.
   Publishing security, retention or remediation policy needs the matching permission and a recent
   sign-in.
4. **Roll back:** publishes the body of an earlier version as a **new** version. History is never
   rewritten.
5. **Archive:** removes a policy; its scope falls back to inherited values.
6. Optional `effective_from` / `effective_until` schedule a version.

Every step is audited (`policy.draft_saved`, `policy.published`, `policy.rolled_back`,
`policy.archived`) with the version and the changed fields.

## 4. Permissions

| Action | Needs |
|---|---|
| view policies, effective values, validate | `policy.view` |
| save, publish, roll back, archive | `policy.manage` |
| … remediation policy | + `remediation.manage_policy` |
| … security policy | + `security.manage` |
| … retention policy | + `retention.manage` |

## 5. Relationship to Phase 8 remediation

The organization's effective remediation policy drives approval modes, the four-eyes threshold, the
approval TTL and auto-remediation for its devices. A field the organization does not set keeps the
platform (Phase 8) value; platform defaults never loosen a stricter Phase 8 setting. Kill switches stack:
the global switch, the platform action and device switches, and the organization's `kill_switch` each
stop new remediation independently. No policy can add actions: the catalog defines what is possible.
