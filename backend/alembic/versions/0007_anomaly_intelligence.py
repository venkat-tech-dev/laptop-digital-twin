"""Phase-4: intelligent anomaly detection (non-destructive: new nullable columns, new tables).

* anomalies: type, level, confidence, lifecycle, evidence, expected range, correlation, feedback ...
  (legacy rows keep NULL / defaults and are still valid)
* device_baselines: one row per (device, signal, context) - learned behavioral baselines
* anomaly_models: versioned per-device Isolation Forest artifacts (JSON, never pickle)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())

NEW_ANOMALY_COLUMNS = (
    sa.Column("anomaly_type", sa.String(32)),
    sa.Column("category", sa.String(32)),
    sa.Column("level", sa.String(16)),
    sa.Column("confidence", sa.Float),
    sa.Column("lifecycle", sa.String(16)),
    sa.Column("signal_id", sa.String(32)),
    sa.Column("model_version", sa.String(96)),
    sa.Column("baseline_version", sa.String(64)),
    sa.Column("expected_value", sa.Float),
    sa.Column("expected_min", sa.Float),
    sa.Column("expected_max", sa.Float),
    sa.Column("deviation_score", sa.Float),
    sa.Column("evidence", JSONB),
    sa.Column("related", JSONB),
    sa.Column("correlation_key", sa.String(160)),
    sa.Column("occurrences", sa.Integer),
    sa.Column("updated_at", sa.DateTime(timezone=True)),
    sa.Column("feedback", JSONB),
)


def upgrade() -> None:
    for col in NEW_ANOMALY_COLUMNS:
        op.add_column("anomalies", col)
    op.create_index("ix_anomalies_device_level", "anomalies", ["device_id", "level", "started_at"])
    op.create_index("ix_anomalies_correlation", "anomalies", ["correlation_key"])

    op.create_table(
        "device_baselines",
        sa.Column(
            "device_id", sa.String(64), sa.ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("signal_id", sa.String(32), primary_key=True),
        sa.Column("context", sa.String(32), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("source_key", sa.String(256)),
        sa.Column("sample_count", sa.Integer, nullable=False),
        sa.Column("excluded_count", sa.Integer, nullable=False),
        sa.Column("trained_from", sa.DateTime(timezone=True)),
        sa.Column("trained_until", sa.DateTime(timezone=True)),
        sa.Column("stats", JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "anomaly_models",
        sa.Column("model_id", sa.String(96), primary_key=True),
        sa.Column(
            "device_id", sa.String(64), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("features", JSONB, nullable=False),
        sa.Column("n_train", sa.Integer, nullable=False),
        sa.Column("threshold", sa.Float, nullable=False),
        sa.Column("trained_from", sa.DateTime(timezone=True)),
        sa.Column("trained_until", sa.DateTime(timezone=True)),
        sa.Column("artifact", JSONB, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_anomaly_models_device", "anomaly_models", ["device_id", "kind", "version"])


def downgrade() -> None:
    op.drop_table("anomaly_models")
    op.drop_table("device_baselines")
    op.drop_index("ix_anomalies_correlation", "anomalies")
    op.drop_index("ix_anomalies_device_level", "anomalies")
    for col in reversed(NEW_ANOMALY_COLUMNS):
        op.drop_column("anomalies", col.name)
