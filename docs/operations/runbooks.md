# Incident runbooks

Each runbook gives the detection signal, severity, investigation, safe mitigation, recovery,
verification, escalation and post-incident review. **No mitigation may bypass authorization, approval,
the remediation allowlist or the audit trail.** If a step seems to need that, stop and escalate.

Severity: **SEV1** data loss or a security breach in progress · **SEV2** a core journey is down (ingest,
API, alert delivery) · **SEV3** degraded with a workaround · **SEV4** cosmetic or single device.

Common tools: `GET /health/ready` (dependencies, background loops, instance role),
`GET /api/v1/ops/slo` (platform admin), Prometheus `/metrics`, container logs
(`docker compose logs backend --since 30m`), the audit trail (Organization → Audit).

Every SEV1/SEV2 needs a written review within 5 working days: timeline, impact (devices, organizations,
data), root cause, what detection missed, and follow-up actions with owners. Keep reviews blameless.

---

## API errors / database saturation
**Detection:** `LdtApiErrorBudgetFastBurn` / `SlowBurn`; readiness 503 with `database: error`; 503 with
`Retry-After: 10` on API calls; `ldt_db_errors_total` rising. **Severity:** SEV2 (SEV1 if persistence lag
grows).
1. `GET /health/ready`: is the database the failing check?
2. In PostgreSQL: `SELECT state, wait_event_type, count(*) FROM pg_stat_activity WHERE datname='ldt' GROUP BY 1,2;`
   and the longest queries: `SELECT pid, now()-query_start AS age, left(query,120) FROM pg_stat_activity WHERE state <> 'idle' ORDER BY age DESC LIMIT 10;`
3. Check disk space on the database volume and `docker stats` for memory pressure.
**Safe mitigation:** cancel (not terminate) a runaway query with `pg_cancel_backend(pid)` (it is
also stopped by `DB_STATEMENT_TIMEOUT_MS`); free disk space by checking that the TimescaleDB
retention and compression jobs run (`SELECT * FROM timescaledb_information.job_stats;`). Do **not**
delete audit rows or disable the audit trigger.
**Recovery:** the backend reconnects by itself (pool pre-ping); agents keep data queued and replay it.
**Verify:** readiness 200, `ldt_persist_oldest_queued_age_seconds` falling, SLO page back to MEETING.
**Escalate:** database owner after 15 min, or at once if the disk is > 90 %.

## Database saturation or outage
**Detection:** `LdtPersistenceLagging` (> 60 s), `LdtPersistenceDropping`, readiness `persistence_queue:
backlogged`. **Severity:** SEV1 if `ldt_persist_dropped_total` increases (accepted samples lost).
1. Is PostgreSQL up (`docker compose ps postgres`, `pg_isready`)?
2. Watch `ldt_persist_queue_depth` against the capacity (50,000 rows).
**Safe mitigation:** restore the database first. If the outage will be long, the backend sheds
replayed uploads at 80 % fill (agents keep them). Live data still overflows at 100 % (oldest dropped,
counted). Do not raise `PERSIST_QUEUE_MAX` far beyond what the backend's memory limit (1 GiB) allows.
**Recovery:** the persister flushes automatically with backoff (2→60 s).
**Verify:** queue depth near 0, lag < 5 s, no new drops. Record the count of dropped samples in the review.

## Telemetry ingestion outage or overload
**Detection:** `LdtIngestShedding` (`ldt_ingest_rejected_total{reason="overloaded"}`), agents reporting
503 / 429, devices turning STALE. **Severity:** SEV2.
1. Is it everyone, or one organization? `ldt_quota_rejections_total{quota="telemetry_batches_per_min"}`
   means a tenant quota (expected throttling, not an outage).
2. `ldt_ingest_inflight` at `INGEST_MAX_INFLIGHT` (32) means the process is saturated: check CPU in
   `docker stats`.
**Safe mitigation:** none needed for quota throttling (agents back off and replay). For saturation,
reduce load by raising the agent interval through the agent policy (Organization → Policies → Agent),
not by disabling validation or authentication.
**Recovery:** agents replay their queues (bounded, oldest first); the dedupe guard prevents double counting.
**Verify:** shed ratio 0, devices ONLINE, `ldt_sequence_observed_total{kind="gap"}` not growing.

## Queue backlog (diagnosis, alerts, remediation events)
**Detection:** `ldt_diagnosis_queue_depth` near `DIAGNOSIS_QUEUE_MAX`; `alert_events_dropped_total`;
`ldt_event_recorder_dropped_total`. **Severity:** SEV3 (SEV2 for alert events).
**Mitigation:** diagnosis falls back to rules and drops excess jobs by design (counted). For alert
event drops, find the burst source (one device flapping?) in the alerts list and suppress that alert
(audited) instead of disabling alerting.
**Verify:** queues drain; no new drops.

## WebSocket delivery failures
**Detection:** dashboards stop updating; `ldt_websocket_dropped_total` rising; browsers show
"reconnecting". **Severity:** SEV3.
1. `ldt_websocket_connections`: is it zero? Then check the proxy (nginx) and the backend.
2. `ldt_redis_connected == 0`: cross-replica fan-out is off (single instance: no impact on its clients).
**Mitigation:** clients reconnect with backoff and resubscribe, and they receive a fresh snapshot
(twin consistency model). A backend restart is safe: twin documents are restored from Redis or rebuilt.
**Verify:** connections back; the live view shows the current twin version.

## Notification provider outage
**Detection:** `LdtNotificationBacklog`; `notification_retry_total` rising; FAILED notifications.
**Severity:** SEV2 for critical alerts.
**Mitigation:** in-app notifications still work (the inbox). Delivery retries 4 times (5 s, 30 s,
120 s with jitter), then FAILED. Notifications stuck in SENDING after a crash are returned to RETRYING
after 5 minutes (`ldt_notifications_stuck_requeued_total`). Tell the people on call through another
channel while the provider is down.
**Recovery:** FAILED notifications are not re-sent automatically (to avoid floods); the open alerts stay
visible in the console.

## Identity provider outage
**Detection:** `auth.oidc_failed` audit events; sign-in errors `OIDC_*`. **Severity:** SEV2 when SSO
is the only sign-in.
**Mitigation:** existing sessions keep working until they expire. Use the documented break-glass
owner account (local sign-in + TOTP) to administer. Do **not** set `local_login_allowed` to true for
everyone during the outage without the security admin's approval (it changes the organization's
authentication policy, and the change is audited).

## AI model unavailable
**Detection:** diagnosis status `provider.available=false`; `ldt_diagnosis_model_failures_total`.
**Severity:** SEV4. Telemetry, twin, detection, predictions, alerts and remediation policy do not
depend on the model.
**Mitigation:** none required: diagnoses are produced from evidence and rules with the notice
"AI reasoning unavailable". To stop the attempts, empty `DIAGNOSIS_MODELS`. Never present a
rules-only diagnosis as model output.

## Prediction job failures
**Detection:** `forecast_error_total` rising; predictions stop updating (`forecast_latency_ms` count flat);
the `forecasting` background loop not `running` in readiness. **Severity:** SEV3.
**Mitigation:** the supervisor restarts the loop with backoff; if it is crash-looping, read
`last_error` in `/health/ready` → `background.not_running.forecasting`. Existing predictions expire on
schedule; nothing is invented while it is down.

## Remediation execution failures
**Detection:** remediation FAILED / verification FAILED; `ldt_remediation_circuit_open_total`;
SLO `remediation-traceability` (in-flight > 1 h). **Severity:** SEV3.
**Mitigation:** the per-device circuit breaker stops repeats automatically. Use the kill switch
(platform, organization or device scope) to stop new actions. Never re-run an action outside the
approval flow; never edit envelopes; never widen the agent allowlist to "make it work".
**Verify:** each item has a terminal state with a reason in its audit trail.

## Compromised agent credential
**Detection:** telemetry from an unexpected network or at impossible rates for one device; a device
token used after a laptop was reported stolen. **Severity:** SEV1.
1. Organization → Devices → device → **Revoke** (credential stops at once; ingest 401).
2. Review the device's audit trail and the remediations requested for it.
3. Re-enroll the real laptop with a new single-use token.
**Verify:** requests with the old token get 401 (`ldt_authentication_failures_total`).

## Suspected tenant isolation incident
**Detection:** `LdtCrossTenantProbes`; a customer report of seeing another organization's data.
**Severity:** SEV1 until disproven.
1. Platform audit: `GET /api/v1/platform/audit?action=security.cross_tenant_attempt`.
2. Reproduce with the isolation suite (`pytest tests/unit/test_tenant_isolation.py`). Add the
   reported route or case to it first.
3. If exposure is confirmed: disable the affected endpoint (a code change), suspend sessions of the
   affected organization if credentials could be involved, and notify the organizations' owners as
   your incident policy requires.
**Do not** "fix" by giving the caller more permissions or by turning off authorization checks.

## Corrupted or stale Digital Twin state
**Detection:** twin values that disagree with recent telemetry; `twin.sync.required` storms; version
gaps in the browser. **Severity:** SEV3.
**Mitigation:** twin state is derived, never authoritative: a client resync (`twin.sync`) or a backend
restart rebuilds it from the stored document plus new telemetry. If the Redis copy is corrupt, delete
`ldt:twindoc:<device>`. The backend rebuilds it from incoming telemetry (fields show "no data" until
then; nothing is invented).

## Background loop crash-looping
**Detection:** `LdtBackgroundLoopDown`; readiness 503 with `background.status = failing`.
1. `/health/ready` → `background.not_running.<task>.last_error`.
2. Logs: `background_task_failed task=<name>` with the stack trace.
**Mitigation:** fix the cause (often a dependency such as the database). The supervisor keeps retrying
(backoff up to 60 s); no manual restart is needed once the cause is gone.

## Failed deployment
**Detection:** the new backend never becomes ready, or SLOs drop right after a release.
**Mitigation:** roll back the image (`docs/operations/release.md`). Migrations are expand-only by
policy, so the previous version runs against the migrated schema. Do **not** run `alembic downgrade`
on production without a restore point; prefer roll-forward fixes.

## Failed migration
**Detection:** the backend container exits at start with an `alembic` error (the command is
`alembic upgrade head && …`).
1. The failed migration's transaction was rolled back (PostgreSQL DDL is transactional), so the schema
   is at the previous revision: `SELECT version_num FROM alembic_version;`.
2. Start the previous image (it does not run the new migration).
3. Reproduce on a restored copy (`scripts/backup_restore.py restore`), fix, and release again.

## Backup restoration
Follow `docs/operations/backup-restore.md` (restore next to the live database, verify, then switch).
After restoring, verify: readiness 200, audit chain valid (Organization → Audit → Verify integrity),
device count as expected, agents reconnecting (they keep their credentials; devices enrolled after the
backup must re-enroll).
