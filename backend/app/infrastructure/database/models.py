"""SQLAlchemy ORM models. Schema changes go through Alembic migrations only."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class DeviceRow(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    manufacturer: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(128))
    model_number: Mapped[str | None] = mapped_column(String(64))
    os_name: Mapped[str | None] = mapped_column(String(128))
    agent_version: Mapped[str] = mapped_column(String(32))
    inventory: Mapped[dict[str, Any]] = mapped_column(JsonType)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_inventory_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HardwareComponentRow(Base):
    __tablename__ = "hardware_components"
    __table_args__ = (UniqueConstraint("device_id", "component_id", name="uq_component_device"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    component_id: Mapped[str] = mapped_column(String(96))
    component_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(256))
    parent_component_id: Mapped[str | None] = mapped_column(String(96))
    manufacturer: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(256))
    properties: Mapped[dict[str, Any]] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TelemetryMetricRow(Base):
    """Catalog of time series (one row per metric+labels per device)."""

    __tablename__ = "telemetry_metrics"
    __table_args__ = (UniqueConstraint("device_id", "metric_key", name="uq_metric_device_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    metric_key: Mapped[str] = mapped_column(String(256))
    metric: Mapped[str] = mapped_column(String(96), index=True)
    component_id: Mapped[str] = mapped_column(String(96))
    unit: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))
    labels: Mapped[dict[str, str]] = mapped_column(JsonType)


class TelemetrySampleRow(Base):
    """Narrow time-series table (TimescaleDB hypertable when the extension is available)."""

    __tablename__ = "telemetry_samples"
    __table_args__ = (Index("ix_samples_metric_time", "metric_id", "time"),)

    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    metric_id: Mapped[int] = mapped_column(
        ForeignKey("telemetry_metrics.id", ondelete="CASCADE"), primary_key=True
    )
    value: Mapped[float] = mapped_column(Float)
    quality: Mapped[int] = mapped_column(SmallInteger)  # 0=GOOD 1=DEGRADED 2=STALE


class HealthEventRow(Base):
    __tablename__ = "health_events"
    __table_args__ = (Index("ix_health_events_device_time", "device_id", "time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    component_id: Mapped[str] = mapped_column(String(96))
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    previous_score: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[int | None] = mapped_column(Integer)
    previous_status: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JsonType)


class AnomalyRow(Base):
    __tablename__ = "anomalies"
    __table_args__ = (
        Index("ix_anomalies_device_started", "device_id", "started_at"),
        Index("ix_anomalies_active", "device_id", "resolved_at"),
        Index("ix_anomalies_device_level", "device_id", "level", "started_at"),
        Index("ix_anomalies_correlation", "correlation_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    detector: Mapped[str] = mapped_column(String(16))
    rule_id: Mapped[str] = mapped_column(String(64))
    component_id: Mapped[str] = mapped_column(String(96))
    metric_key: Mapped[str] = mapped_column(String(256))
    severity: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    value: Mapped[str | None] = mapped_column(String(64))
    threshold: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context: Mapped[dict[str, Any]] = mapped_column(JsonType)
    # ---- Phase 4 (migration 0007; NULL for legacy rows)
    anomaly_type: Mapped[str | None] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(32))
    level: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    lifecycle: Mapped[str | None] = mapped_column(String(16))
    signal_id: Mapped[str | None] = mapped_column(String(32))
    model_version: Mapped[str | None] = mapped_column(String(96))
    baseline_version: Mapped[str | None] = mapped_column(String(64))
    expected_value: Mapped[float | None] = mapped_column(Float)
    expected_min: Mapped[float | None] = mapped_column(Float)
    expected_max: Mapped[float | None] = mapped_column(Float)
    deviation_score: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    related: Mapped[list[dict[str, Any]] | None] = mapped_column(JsonType)
    correlation_key: Mapped[str | None] = mapped_column(String(160))
    occurrences: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    feedback: Mapped[dict[str, Any] | None] = mapped_column(JsonType)


class DeviceBaselineRow(Base):
    """Learned behavioral baseline: one row per (device, signal, context)."""

    __tablename__ = "device_baselines"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    context: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str | None] = mapped_column(String(256))
    sample_count: Mapped[int] = mapped_column(Integer)
    excluded_count: Mapped[int] = mapped_column(Integer)
    trained_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trained_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AnomalyModelRow(Base):
    """Versioned per-device model artifact (JSON - a stored model can never execute code)."""

    __tablename__ = "anomaly_models"
    __table_args__ = (Index("ix_anomaly_models_device", "device_id", "kind", "version"),)

    model_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    features: Mapped[list[str]] = mapped_column(JsonType)
    n_train: Mapped[int] = mapped_column(Integer)
    threshold: Mapped[float] = mapped_column(Float)
    trained_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trained_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    artifact: Mapped[dict[str, Any]] = mapped_column(JsonType)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PredictionRow(Base):
    """Phase 5: one forecast with its lifecycle and calibration (migration 0008)."""

    __tablename__ = "predictions"
    __table_args__ = (
        Index("ix_predictions_device_status", "device_id", "status"),
        Index("ix_predictions_device_target_created", "device_id", "target_id", "created_at"),
        Index("ix_predictions_correlation", "correlation_key", "created_at"),
        Index("ix_predictions_type_status", "prediction_type", "status"),
        Index("ix_predictions_updated", "updated_at"),
        Index("ix_predictions_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    correlation_key: Mapped[str] = mapped_column(String(160))
    target_id: Mapped[str] = mapped_column(String(32))
    prediction_type: Mapped[str] = mapped_column(String(32))
    metric: Mapped[str] = mapped_column(String(96))
    unit: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16))
    severity: Mapped[str | None] = mapped_column(String(16))
    current_value: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    forecast_value: Mapped[float | None] = mapped_column(Float)
    forecast_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    time_to_threshold_s: Mapped[float | None] = mapped_column(Float)
    crossing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    crossing_earliest: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    crossing_latest: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lower_bound: Mapped[float | None] = mapped_column(Float)
    upper_bound: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    confidence_band: Mapped[str] = mapped_column(String(8))
    model_type: Mapped[str] = mapped_column(String(16))
    model_version: Mapped[str] = mapped_column(String(32))
    feature_version: Mapped[str] = mapped_column(String(32))
    baseline_version: Mapped[str | None] = mapped_column(String(64))
    history_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    statement: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    reason: Mapped[str | None] = mapped_column(Text)
    revisions: Mapped[int] = mapped_column(Integer, default=0)
    first_crossing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_crossing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timing_error_s: Mapped[float | None] = mapped_column(Float)
    first_timing_error_s: Mapped[float | None] = mapped_column(Float)
    lead_time_s: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertRow(Base):
    """Phase 6: one alert per ongoing condition (migration 0009)."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_tenant_status_created", "tenant_id", "status", "created_at"),
        Index("ix_alerts_device_created", "device_id", "created_at"),
        Index("ix_alerts_severity_created", "severity", "created_at"),
        Index("ix_alerts_correlation", "correlation_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default")
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    event_id: Mapped[str] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(24))
    alert_type: Mapped[str] = mapped_column(String(48))
    category: Mapped[str] = mapped_column(String(24))
    severity: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    priority: Mapped[int] = mapped_column(SmallInteger)
    confidence: Mapped[float | None] = mapped_column(Float)
    deduplication_key: Mapped[str] = mapped_column(String(64))
    correlation_key: Mapped[str | None] = mapped_column(String(160))
    first_detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    suppressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppressed_by: Mapped[str | None] = mapped_column(String(64))
    suppressed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalation_level: Mapped[int] = mapped_column(SmallInteger, default=0)
    next_escalation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlertAuditRow(Base):
    __tablename__ = "alert_audit"
    __table_args__ = (Index("ix_alert_audit_alert_at", "alert_id", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    alert_id: Mapped[str] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(Text)


class NotificationRow(Base):
    """Phase 6: one message to one user on one channel; also the durable delivery queue."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_notifications_idempotency"),
        Index("ix_notifications_user_created", "tenant_id", "user_id", "created_at"),
        Index("ix_notifications_user_unread", "user_id", "read_at", "channel"),
        Index("ix_notifications_due", "status", "next_retry_at", "deliver_after"),
        Index("ix_notifications_alert", "alert_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default")
    alert_id: Mapped[str | None] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(String(96))
    device_id: Mapped[str | None] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    priority: Mapped[int] = mapped_column(SmallInteger)
    severity: Mapped[str] = mapped_column(String(10))
    category: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    attempt_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deliver_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    escalation_level: Mapped[int] = mapped_column(SmallInteger, default=0)
    history: Mapped[list[dict[str, Any]] | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NotificationPreferenceRow(Base):
    __tablename__ = "notification_preferences"

    user_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default")
    preferences: Mapped[dict[str, Any]] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SystemEventRow(Base):
    __tablename__ = "system_events"
    __table_args__ = (
        Index("ix_system_events_device_time", "device_id", "time"),
        UniqueConstraint("event_uid", name="uq_system_events_event_uid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JsonType)
    # Agent event id (device events only): makes event persistence idempotent across replays/restarts.
    event_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(16), nullable=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AppSettingRow(Base):
    """Operator-editable settings (agent configuration, sync target...). Secrets are never stored here."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str | None] = mapped_column(String(64))


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16))
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # Phase 9: platform super admin


class WorkspaceRow(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(96), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkspaceDeviceRow(Base):
    __tablename__ = "workspace_devices"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)


class AnomalyAckRow(Base):
    __tablename__ = "anomaly_acknowledgements"

    anomaly_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    acknowledged_by: Mapped[str] = mapped_column(String(64))
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class DeviceAssignmentRow(Base):
    """Which employee account uses which device (employees only see their own devices)."""

    __tablename__ = "device_assignments"

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    employee_name: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str | None] = mapped_column(String(64))


class DeviceCredentialRow(Base):
    """Per-device agent token (only a SHA-256 hash is stored; the token itself never is)."""

    __tablename__ = "device_credentials"

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # Phase 9: rotation


class IngestReceiptRow(Base):
    """One row per accepted telemetry batch: durable idempotency across backend restarts and audit
    of what arrived when (kept RECEIPT_RETENTION_HOURS, longer than the agent's queue max age)."""

    __tablename__ = "ingest_receipts"
    __table_args__ = (
        Index("ix_ingest_receipts_received", "received_at"),
        Index("ix_ingest_receipts_device_seq", "device_id", "sequence"),
    )

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(BigInteger)
    schema_version: Mapped[str] = mapped_column(String(8))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    samples: Mapped[int] = mapped_column(Integer)
    events: Mapped[int] = mapped_column(Integer)
    replay: Mapped[bool] = mapped_column(Boolean, default=False)


class DiagnosisRow(Base):
    """Phase 7: one diagnosis version (migration 0010). Versions of one diagnosis share ``series_id``;
    a new version supersedes the previous one, nothing is overwritten. ``body`` holds the evidence,
    hypotheses and explanation (platform-derived data only; never raw endpoint secrets)."""

    __tablename__ = "diagnoses"
    __table_args__ = (
        UniqueConstraint("series_id", "version", name="uq_diagnoses_series_version"),
        Index("ix_diagnoses_device_created", "device_id", "created_at"),
        Index("ix_diagnoses_device_fingerprint", "device_id", "context_fingerprint"),
        Index("ix_diagnoses_alert", "alert_id"),
        Index("ix_diagnoses_anomaly", "anomaly_id"),
        Index("ix_diagnoses_prediction", "prediction_id"),
        Index("ix_diagnoses_status", "status"),
        Index("ix_diagnoses_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    series_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    tenant_id: Mapped[str] = mapped_column(String(64))
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    trigger_kind: Mapped[str] = mapped_column(String(16))
    trigger_id: Mapped[str | None] = mapped_column(String(64))
    alert_id: Mapped[str | None] = mapped_column(String(36))
    anomaly_id: Mapped[str | None] = mapped_column(String(64))
    prediction_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(24))
    diagnosis_type: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str | None] = mapped_column(String(10))
    summary: Mapped[str] = mapped_column(Text)
    likely_cause: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    confidence_level: Mapped[str] = mapped_column(String(16))
    reasoning_model: Mapped[str] = mapped_column(String(96))
    model_version: Mapped[str] = mapped_column(String(96))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    context_fingerprint: Mapped[str] = mapped_column(String(64))
    supersedes: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    body: Mapped[dict[str, Any]] = mapped_column(JsonType)


class DiagnosisFeedbackRow(Base):
    """Phase 7: human feedback on a diagnosis (append-only; used for evaluation, never for retraining)."""

    __tablename__ = "diagnosis_feedback"
    __table_args__ = (
        Index("ix_diagnosis_feedback_diagnosis", "diagnosis_id"),
        Index("ix_diagnosis_feedback_verdict_created", "verdict", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    diagnosis_id: Mapped[str] = mapped_column(ForeignKey("diagnoses.id", ondelete="CASCADE"))
    series_id: Mapped[str] = mapped_column(String(36))
    device_id: Mapped[str] = mapped_column(String(64))
    diagnosis_type: Mapped[str] = mapped_column(String(32))
    verdict: Mapped[str] = mapped_column(String(24))
    actual_cause: Mapped[str | None] = mapped_column(String(500))
    note: Mapped[str | None] = mapped_column(String(1000))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RemediationRow(Base):
    """Phase 8: one remediation (proposal -> approval -> execution -> verification), migration 0011.
    Terminal rows are not edited by the application; corrections are audit entries."""

    __tablename__ = "remediations"
    __table_args__ = (
        UniqueConstraint("execution_id", name="uq_remediations_execution"),
        Index("ix_remediations_device_created", "device_id", "created_at"),
        Index("ix_remediations_status", "status"),
        Index("ix_remediations_tenant_status", "tenant_id", "status"),
        Index("ix_remediations_action_created", "action_type", "created_at"),
        Index("ix_remediations_diagnosis", "diagnosis_id"),
        Index("ix_remediations_alert", "alert_id"),
        Index("ix_remediations_correlation", "correlation_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    device_id: Mapped[str] = mapped_column(String(64))  # no FK: history outlives a removed device
    action_type: Mapped[str] = mapped_column(String(48))
    risk_level: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(24))
    requested_by: Mapped[str] = mapped_column(String(128))
    approved_by: Mapped[str | None] = mapped_column(String(128))
    alert_id: Mapped[str | None] = mapped_column(String(36))
    diagnosis_id: Mapped[str | None] = mapped_column(String(36))
    prediction_id: Mapped[str | None] = mapped_column(String(36))
    correlation_id: Mapped[str] = mapped_column(String(64))
    execution_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    body: Mapped[dict[str, Any]] = mapped_column(JsonType)


class RemediationAuditRow(Base):
    """Phase 8: append-only, hash-chained audit trail (a trigger rejects UPDATE / DELETE)."""

    __tablename__ = "remediation_audit"
    __table_args__ = (
        Index("ix_remediation_audit_remediation", "remediation_id", "id"),
        Index("ix_remediation_audit_at", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    remediation_id: Mapped[str] = mapped_column(String(36))
    device_id: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(48))
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str | None] = mapped_column(String(24))
    detail: Mapped[dict[str, Any]] = mapped_column(JsonType)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


# ---------------------------------------------------------------------------- Phase 9: tenancy & governance
class OrganizationRow(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    quotas: Mapped[dict[str, Any]] = mapped_column(JsonType)
    settings: Mapped[dict[str, Any]] = mapped_column(JsonType)


class OrgUnitRow(Base):
    __tablename__ = "org_units"
    __table_args__ = (Index("ix_org_units_org", "org_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(120))
    parent_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16))


class DeviceGroupRow(Base):
    __tablename__ = "device_groups"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_device_groups_org_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(24))
    unit_id: Mapped[str | None] = mapped_column(String(36))
    priority: Mapped[int] = mapped_column(Integer)
    tags: Mapped[list[str]] = mapped_column(JsonType)
    status: Mapped[str] = mapped_column(String(16))


class DeviceGroupMemberRow(Base):
    __tablename__ = "device_group_members"
    __table_args__ = (Index("ix_device_group_members_device", "device_id"),)

    group_id: Mapped[str] = mapped_column(
        ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True
    )
    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64))


class OrganizationMemberRow(Base):
    __tablename__ = "organization_members"
    __table_args__ = (Index("ix_organization_members_username", "username"),)

    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16))
    group_scope: Mapped[list[str]] = mapped_column(JsonType)
    source: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeviceRegistryRow(Base):
    """Authoritative device ownership (organisation) and lifecycle."""

    __tablename__ = "device_registry"
    __table_args__ = (Index("ix_device_registry_org_lifecycle", "org_id", "lifecycle"),)

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    lifecycle: Mapped[str] = mapped_column(String(24))
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrollment_id: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(String(300))


class EnrollmentTokenRow(Base):
    __tablename__ = "enrollment_tokens"
    __table_args__ = (Index("ix_enrollment_tokens_org_created", "org_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[int] = mapped_column(Integer)
    uses: Mapped[int] = mapped_column(Integer)
    group_id: Mapped[str | None] = mapped_column(String(36))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label: Mapped[str] = mapped_column(String(120))


class PolicyRow(Base):
    __tablename__ = "policies"
    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_policies_version"),
        Index("ix_policies_org_kind_status", "org_id", "kind", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    policy_id: Mapped[str] = mapped_column(String(36))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    scope_type: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    body: Mapped[dict[str, Any]] = mapped_column(JsonType)
    locked: Mapped[list[str]] = mapped_column(JsonType)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(String(300))


class AuditEventRow(Base):
    """Append-only, hash-chained enterprise audit (a trigger rejects UPDATE / DELETE)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_org_at", "org_id", "at"),
        Index("ix_audit_events_org_action_at", "org_id", "action", "at"),
        Index("ix_audit_events_org_actor_at", "org_id", "actor_id", "at"),
        Index("ix_audit_events_org_resource", "org_id", "resource_type", "resource_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    org_id: Mapped[str | None] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(128))
    actor_type: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(24))
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(500))
    severity: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class UserSessionRow(Base):
    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_user_sessions_username", "username"),
        Index("ix_user_sessions_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64))
    org_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(120))
    auth_method: Mapped[str] = mapped_column(String(16))
    mfa: Mapped[bool] = mapped_column(Boolean, default=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(200))


class IdentityProviderRow(Base):
    __tablename__ = "identity_providers"
    __table_args__ = (Index("ix_identity_providers_org", "org_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    kind: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16))
    config: Mapped[dict[str, Any]] = mapped_column(
        JsonType
    )  # no secrets: client secrets are secret references
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserMfaRow(Base):
    __tablename__ = "user_mfa"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    secret_enc: Mapped[str] = mapped_column(String(512))  # Fernet-encrypted TOTP seed
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_step: Mapped[int] = mapped_column(BigInteger, default=0)


class ScimTokenRow(Base):
    __tablename__ = "scim_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(120))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
