"""Initial schema: devices, components, metric catalog, samples, health events, anomalies, system events.

Revision ID: 0001
Revises:
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _timescale_available() -> bool:
    bind = op.get_bind()
    row = bind.execute(sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")).first()
    return row is not None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("manufacturer", sa.String(128)),
        sa.Column("model", sa.String(128)),
        sa.Column("model_number", sa.String(64)),
        sa.Column("os_name", sa.String(128)),
        sa.Column("agent_version", sa.String(32), nullable=False),
        sa.Column("inventory", JSONB, nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True)),
        sa.Column("last_inventory_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "hardware_components",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.String(64), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("component_id", sa.String(96), nullable=False),
        sa.Column("component_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("parent_component_id", sa.String(96)),
        sa.Column("manufacturer", sa.String(128)),
        sa.Column("model", sa.String(256)),
        sa.Column("properties", JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("device_id", "component_id", name="uq_component_device"),
    )
    op.create_index("ix_hardware_components_device_id", "hardware_components", ["device_id"])
    op.create_table(
        "telemetry_metrics",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.String(64), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric_key", sa.String(256), nullable=False),
        sa.Column("metric", sa.String(96), nullable=False),
        sa.Column("component_id", sa.String(96), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("labels", JSONB, nullable=False),
        sa.UniqueConstraint("device_id", "metric_key", name="uq_metric_device_key"),
    )
    op.create_index("ix_telemetry_metrics_device_id", "telemetry_metrics", ["device_id"])
    op.create_index("ix_telemetry_metrics_metric", "telemetry_metrics", ["metric"])
    op.create_table(
        "telemetry_samples",
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric_id", sa.Integer, sa.ForeignKey("telemetry_metrics.id", ondelete="CASCADE"), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("quality", sa.SmallInteger, nullable=False),
        sa.PrimaryKeyConstraint("time", "metric_id", name="pk_telemetry_samples"),
    )
    op.create_index("ix_samples_metric_time", "telemetry_samples", ["metric_id", "time"])
    op.create_table(
        "health_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.String(64), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("component_id", sa.String(96), nullable=False),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_score", sa.Integer),
        sa.Column("score", sa.Integer),
        sa.Column("previous_status", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reasons", JSONB, nullable=False),
    )
    op.create_index("ix_health_events_device_time", "health_events", ["device_id", "time"])
    op.create_table(
        "anomalies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("device_id", sa.String(64), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("detector", sa.String(16), nullable=False),
        sa.Column("rule_id", sa.String(64), nullable=False),
        sa.Column("component_id", sa.String(96), nullable=False),
        sa.Column("metric_key", sa.String(256), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("value", sa.String(64)),
        sa.Column("threshold", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("context", JSONB, nullable=False),
    )
    op.create_index("ix_anomalies_device_started", "anomalies", ["device_id", "started_at"])
    op.create_index("ix_anomalies_active", "anomalies", ["device_id", "resolved_at"])
    op.create_table(
        "system_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.String(64), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("data", JSONB, nullable=False),
    )
    op.create_index("ix_system_events_device_time", "system_events", ["device_id", "time"])

    if _timescale_available():
        op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        op.execute(
            "SELECT create_hypertable('telemetry_samples', 'time', chunk_time_interval => INTERVAL '1 day', "
            "migrate_data => true)"
        )
        op.execute(
            "ALTER TABLE telemetry_samples SET (timescaledb.compress, "
            "timescaledb.compress_segmentby = 'metric_id', timescaledb.compress_orderby = 'time DESC')"
        )
        op.execute("SELECT add_compression_policy('telemetry_samples', INTERVAL '2 days')")
        op.execute("SELECT add_retention_policy('telemetry_samples', INTERVAL '30 days')")


def downgrade() -> None:
    for table in ("system_events", "anomalies", "health_events", "telemetry_samples", "telemetry_metrics",
                  "hardware_components", "devices"):
        op.drop_table(table)
