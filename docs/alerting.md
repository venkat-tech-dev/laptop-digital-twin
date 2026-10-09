# Notification & alerting engine (Phase 6)

Turns validated Phase 4 anomalies, Phase 5 predictions, agent connectivity and device security/agent
health into **alerts**, and alerts into **notifications** delivered reliably, audited end to end and
without alert storms. No LLM, no remediation: escalation means *notifying* someone, nothing else.

## 1. Event, alert, notification

| Concept | What it is | Where |
|---|---|---|
| Event | something was detected (`AnomalyChanged`, `PredictionChanged`, `PresenceChanged`, twin health events) | existing EventBus |
| Alert | the system decided it needs attention; **one alert per ongoing condition** | `alerts` + `alert_audit` |
| Notification | one message to one user on one channel (one alert -> many) | `notifications` (also the delivery queue) |

```text
EventBus -> adapters (validation) -> AlertEngine (policy, dedupe, persistence, cooldown, correlation)
  -> alerts (+ audit) -> alert.* WebSocket + twin timeline (alert.created / acknowledged / resolved ...)
  -> routing (recipients, preferences, quiet hours, grouping, fatigue guard)
  -> notifications = durable queue -> delivery worker -> providers -> retry / dead letter
  -> notification.* WebSocket to the recipient only
```

Code: `app/domain/alerting/` (pure: `models.py`, `policy.py`, `engine.py`, `routing.py`,
`delivery.py`), `app/services/alerting.py`, `app/services/notify_providers.py`,
`app/repositories/alerting.py`, `app/api/v1/alerting.py`, migration `0009_alerting.py`; agent:
`agent/app/platform/toast.py`.

## 2. Sources

| Source | Opens | Closes | Notes |
|---|---|---|---|
| Phase 4 anomaly | `anomaly.detected` (level, confidence) | `anomaly.resolved` (also EXPIRED) | SUPPRESSED anomalies are ignored |
| Phase 5 prediction | `prediction.created` | invalidated / expired / confirmed / cancelled | severity raised by time to threshold |
| Connectivity | presence OFFLINE | presence ONLINE | STALE is too transient to alert |
| Security / agent health | twin health WARNING/CRITICAL with `security`/`posture` or `agent` reasons | health recovers | performance reasons already alert through Phase 4 (no double alerts) |

## 3. Policy (configuration, `GET/PUT /api/v1/alert-policy`, admin, versioned)

| Rule | Sources | Min severity | Min confidence | Persistence | Notes |
|---|---|---|---|---|---|
| critical-immediate | all | CRITICAL | - | 0 | |
| high-immediate | all | HIGH | 0.5 | 0 | |
| medium-persistent | anomaly, prediction | MEDIUM | 0.5 | 300 s | must persist before alerting |
| connectivity | connectivity | MEDIUM | - | 0 | |
| device-health | device health, security | HIGH | - | 0 | |
| low-grouped | anomaly, prediction | LOW | 0.5 | 0 | notifications grouped |

INFO never alerts. Prediction bands: <= 15 min -> HIGH, <= 1 h -> MEDIUM, <= 24 h -> LOW (raise only;
LOW-confidence forecasts are not raised). Cooldown 15 min, correlation window 10 min, expiry after 24 h
without updates, escalation for HIGH/CRITICAL: 30 min unacknowledged -> operators, 60 min -> admins.

## 4. Lifecycle (invalid transitions are rejected, every transition audited with the actor)

```text
Alert:         OPEN -> ONGOING -> ACKNOWLEDGED -> RESOLVED      (+ SUPPRESSED, EXPIRED)
Notification:  PENDING -> SENDING -> DELIVERED -> READ
               SENDING -> RETRYING -> ... -> FAILED (dead letter) | QUEUED (agent pickup) | EXPIRED
               PENDING/RETRYING -> CANCELLED (alert closed, merged into a digest)
```

## 5. Non-spam guarantees

* **Deduplication**: key = tenant + device + source + alert type + condition (+ threshold). The database
  enforces at most one open alert per key (partial unique index `uq_alerts_open_dedupe`).
* **Hysteresis / material change**: sources already debounce (Phase 4 recovery bars, Phase 5 gates,
  presence thresholds); the alert resolves only when the source closes; updates notify only when the
  severity *rises*. Ongoing alerts read "... (ongoing for 18 min)", never "CPU high" x 10.
* **Persistence** for MEDIUM; **cooldown**: a recurrence within 15 min is a new, visible alert that does
  not notify again unless more severe.
* **Grouping**: LOW alerts and users with frequency "grouped" are held for the group window and merged
  into one digest per user and channel; daily digest at the user's hour. CRITICAL is never grouped.
* **Fatigue guard**: beyond 30 notifications per user per hour, further non-critical ones are grouped.
* **Correlation**: alerts of one device and family within 10 min share a `correlation_key`.

## 6. Routing and preferences (`GET/PUT /api/v1/notification-preferences`, own only)

Recipients are authorization-aware: operators and admins; viewers from HIGH; the employee a device is
assigned to (never other employees). Preferences: channels (in-app, browser, Windows, e-mail), severities,
categories, frequency (immediate / grouped / digest), quiet hours (start, end, HIGH and MEDIUM behaviour)
in the user's IANA time zone (midnight crossing and DST handled). Quiet hours and digests *defer*
(`deliver_after`), they never drop. CRITICAL: always immediate and always in the in-app inbox
(enterprise safeguard, cannot be disabled by a user).

## 7. Providers (`NotificationProvider`)

| Channel | Provider | Delivered when | Failure handling |
|---|---|---|---|
| in_app | inbox row + push to the user's sessions | stored | - |
| browser | push to the user's open sessions; the page shows an OS notification only with permission | a session is open | retried (no session) up to 1 h, then EXPIRED; the inbox copy remains |
| windows | queued for the device's agent (device token), shown as a Windows toast | the agent acknowledges | expires after 1 h |
| email | SMTP (`SMTP_HOST/PORT/USERNAME/FROM/STARTTLS`, password only from `SMTP_PASSWORD`) | server accepts | 4xx transient, 5xx/auth permanent |
| webhook | HTTPS POST, HMAC-SHA256 `X-LDT-Signature` over `timestamp.body`, `Idempotency-Key`, 5 s timeout | 2xx | 429 Retry-After, 5xx/timeout transient, 4xx permanent |

Retry: immediately, then 5 s, 30 s, 2 min (+-20 % jitter); the 4th failure is a dead letter (FAILED, kept
with `last_error`). Permanent failures are never retried. The worker claims due notifications with
`FOR UPDATE SKIP LOCKED` (several replicas are safe) and delivers with bounded concurrency (16) and a 15 s
provider timeout; provider calls never run in an API request.

Webhooks: the signing secret only comes from `WEBHOOK_SIGNING_SECRET` (never stored or returned); URLs
must be https without credentials, and private / loopback / link-local addresses are refused
(`WEBHOOK_ALLOW_HTTP`, `WEBHOOK_ALLOW_PRIVATE` for lab use). Payload: identifiers, severity, wording and
the alert's facts only.

Verification on the receiver: `hmac_sha256(secret, X-LDT-Timestamp + "." + raw_body) == X-LDT-Signature`
(reject old timestamps to prevent replay).

## 8. Real time and recovery

`alert.*` events go to the device topic (authorised subscribers); `notification.*` only to the
recipient's own sessions (`recipient` field, enforced in the WebSocket manager, also across replicas via
Redis). On every (re)connect the browser resynchronises the inbox over REST, so a disconnected browser
never loses a notification; ids de-duplicate and `created_at` orders.

## 9. APIs

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/alerts?device_id&severity&category&status&source_type&since&until&limit&offset` | reader (device-scoped) |
| GET | `/api/v1/alerts/{id}` (facts, evidence, audit, delivery history) | reader (device-checked) |
| POST | `/api/v1/alerts/{id}/acknowledge` | operator or the device owner |
| POST | `/api/v1/alerts/{id}/resolve`, `/suppress` | operator |
| GET | `/api/v1/notifications?unread&severity&category&device_id&since&until&limit&offset` | own |
| GET | `/api/v1/notifications/unread-count`, `/{id}` | own |
| POST | `/api/v1/notifications/{id}/read`, `/read-all` | own |
| GET/PUT | `/api/v1/notification-preferences` | own |
| GET/PUT | `/api/v1/alert-policy`, `/api/v1/notification-webhooks` | admin |
| GET | `/api/v1/alerting/stats` (fatigue indicators, delivery, channels) | staff |
| GET | `/api/v1/agent/notifications`, POST `/{id}/ack` | device token (own device only) |

## 10. Observability

Prometheus: `alerts_total{kind,severity}` (created, updated, resolved, suppressed, escalated ...),
`notifications_total{kind,channel}` (created, delivered, failed), `notification_retry_total{channel}`,
`notification_provider_latency_ms{provider}`. `GET /alerting/stats`: alerts per device and severity,
deduplication rate, suppression, acknowledgement rate and mean time, mean resolution time, escalations,
notifications per user, delivery failure rate. Logs carry `alert_id` (correlation id), `event_id`,
`notification_id`, provider and failure class; never payload contents or secrets.

## 11. Retention

Closed alerts and finished notifications older than `NOTIFICATION_RETENTION_DAYS` (90) are purged hourly;
open alerts and pending deliveries are never purged.
