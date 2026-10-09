# Data retention and deletion

## 1. What is kept, and for how long

| Data | Platform setting (ceiling) | Organization policy (`retention`) | Notes |
|---|---|---|---|
| Raw telemetry samples | `RETENTION_DAYS` (30) | `raw_telemetry_days` | TimescaleDB retention job platform-wide; the organization job deletes earlier for its devices |
| 5-minute aggregates | `AGGREGATE_RETENTION_DAYS` (365) | – | platform only |
| Health / system events | `EVENT_RETENTION_DAYS` (365) | – | platform only |
| Ingest receipts (idempotency) | `RECEIPT_RETENTION_HOURS` (168) | – | platform only |
| Diagnoses | `DIAGNOSIS_RETENTION_DAYS` (90) | `diagnoses_days` | |
| Closed alerts | `NOTIFICATION_RETENTION_DAYS` (90) | `alerts_days` | open, ongoing, acknowledged and suppressed alerts are never removed |
| Delivered / failed notifications | `NOTIFICATION_RETENTION_DAYS` (90) | `notifications_days` | pending, queued and retrying notifications are never removed |
| Remediations and their audit | kept | – | governance record |
| Central audit events | kept (append-only) | – | never deleted by retention or by data deletion |

**An organization can shorten retention, never extend it beyond the platform setting.** The platform
purge removes everything older than the ceiling for all organizations. Per organization, the hourly job
(`DataGovernanceService.apply_retention`) deletes that organization's rows older than its policy when the
policy is shorter. Each run that deletes rows writes a `data.retention_applied` audit event with the row
counts and the policy values.

Verified against PostgreSQL (migration test, see [../operations/backup-restore.md](../operations/backup-restore.md)):
old closed alerts and delivered notifications are deleted; old open alerts and pending notifications
remain.

## 2. Deleting a device's data

For a device that has left service (a returned laptop, or an employee's data deletion request):

1. Retire the device (`device.remove`, recent sign-in). Its credential is revoked.
2. Console → Devices → select device → **Delete data**. This needs `device.remove` + `retention.manage`,
   a recent sign-in, the device id typed as confirmation, and a reason (for example the request
   reference).
3. An asynchronous job deletes raw samples (and refreshes the aggregates over that range), the metrics
   catalogue, health and system events, anomalies, baselines and models, predictions, diagnoses and
   feedback, notifications, and ingest receipts. The device then becomes **DECOMMISSIONED**.
4. Kept on purpose: the device identity row, alerts, remediations, the registry entry and every audit
   event. These are governance records that show what happened. They contain no telemetry values beyond
   what the alert or remediation states.

Audit: `data.deletion_requested` (who, reason) and `data.deleted` (row counts per table).

## 3. Organization data

Suspending an organization signs all its members out and stops its devices' ingest. Data is kept.
Deleting an entire organization is not offered in the product: it is an operator procedure (retire and
delete each device, then archive the organization) so that it is always deliberate and audited.
