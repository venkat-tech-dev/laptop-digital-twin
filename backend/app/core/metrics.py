"""Prometheus metrics for the backend itself (service observability, not device telemetry)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_LATENCY = Histogram(
    "ldt_http_request_duration_seconds", "API latency", ["method", "route", "status"], registry=REGISTRY
)
INGEST_BATCHES = Counter(
    "ldt_ingest_batches_total", "Telemetry batches ingested", ["result"], registry=REGISTRY
)
INGEST_SAMPLES = Counter(
    "ldt_ingest_samples_total", "Telemetry samples ingested", ["quality"], registry=REGISTRY
)
EVENT_PROCESSING = Histogram(
    "ldt_event_processing_seconds",
    "Twin update latency per batch",
    registry=REGISTRY,
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
WS_CONNECTIONS = Gauge("ldt_websocket_connections", "Open WebSocket connections", registry=REGISTRY)
WS_DROPPED = Counter(
    "ldt_websocket_dropped_total", "Slow WebSocket consumers disconnected", registry=REGISTRY
)
DB_LATENCY = Histogram("ldt_db_operation_seconds", "Database latency", ["operation"], registry=REGISTRY)
DB_ERRORS = Counter("ldt_db_errors_total", "Database errors", ["operation"], registry=REGISTRY)
REDIS_LATENCY = Histogram("ldt_redis_operation_seconds", "Redis latency", ["operation"], registry=REGISTRY)
PERSIST_QUEUE = Gauge("ldt_persist_queue_depth", "Samples waiting to be persisted", registry=REGISTRY)
PERSIST_DROPPED = Counter("ldt_persist_dropped_total", "Samples dropped (queue full)", registry=REGISTRY)
SENSOR_AVAILABLE = Gauge(
    "ldt_sensor_available", "1 if the metric is currently available", ["metric"], registry=REGISTRY
)
COLLECTION_FAILURES = Gauge(
    "ldt_agent_provider_failures", "Agent-reported provider failures", ["provider"], registry=REGISTRY
)
ANOMALIES_ACTIVE = Gauge("ldt_anomalies_active", "Active anomalies", ["severity"], registry=REGISTRY)

# ---- Phase-2 pipeline observability
_LATENCY_BUCKETS_MS = (1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)
PIPELINE_LATENCY = Histogram(
    "ldt_pipeline_latency_ms",
    "Pipeline latency by stage: collection_to_server, server_processing, ws_queue, "
    "websocket_delivery, end_to_end (ms)",
    ["stage"],
    registry=REGISTRY,
    buckets=_LATENCY_BUCKETS_MS,
)
INGEST_BYTES = Counter(
    "ldt_ingest_bytes_total",
    "Agent request bytes (wire = as sent, decoded = after gzip)",
    ["route", "kind"],
    registry=REGISTRY,
)
INGEST_REJECTED = Counter(
    "ldt_ingest_rejected_total", "Rejected batches by reason", ["reason"], registry=REGISTRY
)
INGEST_RATE_LIMITED = Counter(
    "ldt_ingest_rate_limited_total", "Agent requests answered with 429", registry=REGISTRY
)
SEQUENCE_OBSERVED = Counter(
    "ldt_sequence_observed_total", "Batch sequence classification", ["kind"], registry=REGISTRY
)
DEVICES_BY_PRESENCE = Gauge(
    "ldt_devices_by_presence", "Devices per presence state", ["presence"], registry=REGISTRY
)
WS_SUBSCRIPTIONS = Gauge(
    "ldt_websocket_subscriptions", "Active WebSocket topic subscriptions", registry=REGISTRY
)
WS_MESSAGES = Counter(
    "ldt_websocket_messages_total", "Messages queued to WebSocket clients", registry=REGISTRY
)
RECEIPTS_PENDING = Gauge(
    "ldt_ingest_receipts_pending", "Idempotency receipts waiting to be persisted", registry=REGISTRY
)

# ---- Phase-4 anomaly intelligence
ANOMALIES_DETECTED = Counter(
    "anomalies_detected_total", "Anomalies opened", ["type", "level"], registry=REGISTRY
)
ANOMALIES_RESOLVED = Counter(
    "anomalies_resolved_total", "Anomalies closed", ["type", "how"], registry=REGISTRY
)
ANOMALIES_SUPPRESSED = Counter(
    "anomalies_suppressed_total",
    "Anomaly candidates suppressed (dedupe/feedback)",
    ["type"],
    registry=REGISTRY,
)
FALSE_POSITIVE_FEEDBACK = Counter(
    "false_positive_feedback_total",
    "Operator feedback marking an anomaly as a false positive",
    ["type"],
    registry=REGISTRY,
)
DETECTOR_LATENCY = Histogram(
    "detector_latency_ms",
    "Behavioral evaluation time per device (ms)",
    registry=REGISTRY,
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 25, 50, 100, 250),
)
BASELINE_TRAINING = Histogram(
    "baseline_training_duration_seconds",
    "Baseline training time per device",
    registry=REGISTRY,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
MODEL_TRAINING = Histogram(
    "model_training_duration_seconds",
    "Isolation Forest training time per device",
    registry=REGISTRY,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
DETECTOR_ERRORS = Counter(
    "detector_error_total", "Errors in anomaly evaluation/training", ["stage"], registry=REGISTRY
)
ACTIVE_ANOMALIES = Gauge(
    "active_anomalies",
    "Active anomalies by type and level (all devices)",
    ["type", "level"],
    registry=REGISTRY,
)

# ---- Phase-5 forecasting
PREDICTIONS_TOTAL = Counter(
    "predictions_total",
    "Prediction lifecycle transitions (created/updated/invalidated/expired/confirmed)",
    ["kind", "target"],
    registry=REGISTRY,
)
FORECAST_LATENCY = Histogram(
    "forecast_latency_ms",
    "Forecast time per device and target (ms)",
    registry=REGISTRY,
    buckets=(0.5, 1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1000),
)
FORECAST_ERRORS = Counter(
    "forecast_error_total", "Forecasting failures by stage/target", ["stage"], registry=REGISTRY
)
PREDICTION_TIMING_ERROR = Histogram(
    "prediction_timing_error_seconds",
    "Absolute timing error of confirmed predictions",
    ["target"],
    registry=REGISTRY,
    buckets=(30, 60, 300, 900, 3600, 6 * 3600, 86400, 7 * 86400),
)
LOW_CONFIDENCE_PREDICTIONS = Gauge(
    "low_confidence_predictions",
    "Targets whose forecast is below the publishing confidence",
    registry=REGISTRY,
)
STALE_PREDICTIONS = Gauge(
    "stale_prediction_count", "Targets that cannot be forecast: stale data", registry=REGISTRY
)

# ---- Phase-6 alerting and notifications
ALERTS_TOTAL = Counter(
    "alerts_total",
    "Alert lifecycle transitions (created/updated/resolved/suppressed/escalated/...)",
    ["kind", "severity"],
    registry=REGISTRY,
)
NOTIFICATIONS_TOTAL = Counter(
    "notifications_total",
    "Notifications created / delivered / failed by channel",
    ["kind", "channel"],
    registry=REGISTRY,
)
NOTIFICATION_RETRIES = Counter(
    "notification_retry_total", "Delivery retries by channel", ["channel"], registry=REGISTRY
)
PROVIDER_LATENCY = Histogram(
    "notification_provider_latency_ms",
    "Provider call latency (ms)",
    ["provider"],
    registry=REGISTRY,
    buckets=(1, 5, 10, 50, 100, 250, 500, 1000, 2500, 5000, 15000),
)
OPEN_ALERTS = Gauge("open_alerts", "Open alerts by severity", ["severity"], registry=REGISTRY)
ALERT_EVENTS_DROPPED = Counter(
    "alert_events_dropped_total",
    "Events dropped because the alert queue was full (back-pressure)",
    registry=REGISTRY,
)
# ---- Phase 7: diagnosis
DIAGNOSES_TOTAL = Counter(
    "ldt_diagnoses_total", "Diagnoses produced", ["status", "reasoning"], registry=REGISTRY
)
DIAGNOSIS_LATENCY = Histogram(
    "ldt_diagnosis_seconds",
    "End-to-end diagnosis latency (context, evidence, rules, optional model)",
    ["reasoning"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
    registry=REGISTRY,
)
DIAGNOSIS_QUEUE = Gauge("ldt_diagnosis_queue_depth", "Diagnosis jobs waiting", registry=REGISTRY)
DIAGNOSIS_MODEL_FAILURES = Counter(
    "ldt_diagnosis_model_failures_total", "Local model failures", ["reason"], registry=REGISTRY
)
DIAGNOSIS_REJECTED_CLAIMS = Counter(
    "ldt_diagnosis_rejected_claims_total",
    "Model statements rejected by validation",
    ["reason"],
    registry=REGISTRY,
)
DIAGNOSIS_CACHE_HITS = Counter(
    "ldt_diagnosis_cache_hits_total", "Diagnoses served from cache", registry=REGISTRY
)
DIAGNOSIS_DROPPED = Counter(
    "ldt_diagnosis_jobs_dropped_total",
    "Diagnosis jobs refused (queue full / cooldown)",
    ["reason"],
    registry=REGISTRY,
)
# ---- Phase 8: remediation
REMEDIATION_EVENTS = Counter(
    "ldt_remediation_events_total",
    "Remediation lifecycle events (proposed, approved, rejected, succeeded, failed, cancelled, expired ...)",
    ["event", "action", "risk"],
    registry=REGISTRY,
)
REMEDIATION_LATENCY = Histogram(
    "ldt_remediation_seconds",
    "Remediation latency by stage (approval, execution, verification)",
    ["stage"],
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900, 1800, 3600),
    registry=REGISTRY,
)
REMEDIATION_CIRCUIT_OPEN = Counter(
    "ldt_remediation_circuit_open_total",
    "Circuit breakers opened after repeated failures",
    ["action"],
    registry=REGISTRY,
)
REMEDIATION_PRECONDITION_WAITS = Counter(
    "ldt_remediation_precondition_waits_total",
    "Dispatch postponed by a transient precondition",
    ["check"],
    registry=REGISTRY,
)
# ---- Phase 9: tenancy, identity, governance (no per-tenant labels: unbounded cardinality; per-tenant
# usage is served by /api/v1/org/usage instead)
CROSS_TENANT_ATTEMPTS = Counter(
    "ldt_cross_tenant_access_attempts_total",
    "Requests for another organization's resources",
    ["resource"],
    registry=REGISTRY,
)
AUTHZ_DENIALS = Counter(
    "ldt_authorization_denials_total", "Authorization denials", ["permission"], registry=REGISTRY
)
AUTH_FAILURES = Counter(
    "ldt_authentication_failures_total", "Authentication failures", ["kind"], registry=REGISTRY
)
ENROLLMENT_FAILURES = Counter(
    "ldt_enrollment_failures_total", "Failed device enrollments", ["reason"], registry=REGISTRY
)
QUOTA_REJECTIONS = Counter(
    "ldt_quota_rejections_total", "Requests throttled or rejected by quota", ["quota"], registry=REGISTRY
)
AUDIT_EVENTS = Counter(
    "ldt_audit_events_total", "Audit events recorded", ["category", "result"], registry=REGISTRY
)
AUDIT_DROPPED = Counter(
    "ldt_audit_events_dropped_total", "INFO audit events dropped (buffer full)", registry=REGISTRY
)
POLICY_EVALUATIONS = Counter(
    "ldt_policy_evaluations_total", "Effective-policy evaluations (cache misses)", registry=REGISTRY
)
TENANTS = Gauge("ldt_tenants", "Organizations by status", ["status"], registry=REGISTRY)
ACTIVE_DEVICES = Gauge(
    "ldt_registered_devices", "Registered devices by lifecycle state", ["lifecycle"], registry=REGISTRY
)
ACTIVE_SESSIONS = Gauge(
    "ldt_websocket_connections_by_role", "WebSocket connections by legacy role", ["role"], registry=REGISTRY
)

# ---------------------------------------------------------------- Phase 10: operations / SLIs
BACKGROUND_TASK_UP = Gauge(
    "ldt_background_task_up", "1 while a supervised background loop runs", ["task"], registry=REGISTRY
)
BACKGROUND_TASK_RESTARTS = Counter(
    "ldt_background_task_restarts_total",
    "Supervised background loop restarts after a failure",
    ["task"],
    registry=REGISTRY,
)
RECORDER_QUEUE = Gauge("ldt_event_recorder_queue_depth", "Event-record jobs waiting", registry=REGISTRY)
RECORDER_DROPPED = Counter(
    "ldt_event_recorder_dropped_total",
    "Event-record jobs dropped (queue full or failed)",
    ["reason"],
    registry=REGISTRY,
)
RECEIPTS_DROPPED = Counter(
    "ldt_ingest_receipts_dropped_total", "Ingest receipts dropped before they were written", registry=REGISTRY
)
PERSIST_LAG = Gauge(
    "ldt_persist_oldest_queued_age_seconds",
    "Age of the oldest sample waiting to be written",
    registry=REGISTRY,
)
INGEST_INFLIGHT = Gauge("ldt_ingest_inflight", "Ingest requests being processed", registry=REGISTRY)
INGEST_DUPLICATES = Counter(
    "ldt_ingest_duplicates_total", "Duplicate batches ignored", ["where"], registry=REGISTRY
)
REDIS_CONNECTED = Gauge("ldt_redis_connected", "1 when Redis answers", registry=REGISTRY)
NOTIFICATION_BACKLOG = Gauge("ldt_notification_backlog", "Notifications due for delivery", registry=REGISTRY)
NOTIFICATION_OLDEST_DUE = Gauge(
    "ldt_notification_oldest_due_seconds",
    "Age of the oldest notification waiting for delivery",
    registry=REGISTRY,
)
NOTIFICATIONS_REQUEUED = Counter(
    "ldt_notifications_stuck_requeued_total",
    "Notifications stuck in SENDING (crash) returned to RETRYING",
    registry=REGISTRY,
)
