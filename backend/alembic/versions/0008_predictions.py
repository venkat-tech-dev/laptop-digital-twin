"""Phase-5: durable predictions (forecast lifecycle + calibration). Non-destructive: new table only.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "predictions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "device_id", sa.String(64), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("correlation_key", sa.String(160), nullable=False),
        sa.Column("target_id", sa.String(32), nullable=False),
        sa.Column("prediction_type", sa.String(32), nullable=False),
        sa.Column("metric", sa.String(96), nullable=False),
        sa.Column("unit", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(16)),
        sa.Column("current_value", sa.Float),
        sa.Column("threshold", sa.Float, nullable=False),
        sa.Column("forecast_value", sa.Float),
        sa.Column("forecast_at", sa.DateTime(timezone=True)),
        sa.Column("time_to_threshold_s", sa.Float),
        sa.Column("crossing_at", sa.DateTime(timezone=True)),
        sa.Column("crossing_earliest", sa.DateTime(timezone=True)),
        sa.Column("crossing_latest", sa.DateTime(timezone=True)),
        sa.Column("lower_bound", sa.Float),
        sa.Column("upper_bound", sa.Float),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("confidence_band", sa.String(8), nullable=False),
        sa.Column("model_type", sa.String(16), nullable=False),
        sa.Column("model_version", sa.String(32), nullable=False),
        sa.Column("feature_version", sa.String(32), nullable=False),
        sa.Column("baseline_version", sa.String(64)),
        sa.Column("history_start", sa.DateTime(timezone=True)),
        sa.Column("history_end", sa.DateTime(timezone=True)),
        sa.Column("statement", sa.Text, nullable=False),
        sa.Column("evidence", JSONB),
        sa.Column("reason", sa.Text),
        sa.Column("revisions", sa.Integer, nullable=False, server_default="0"),
        sa.Column("first_crossing_at", sa.DateTime(timezone=True)),
        sa.Column("actual_crossing_at", sa.DateTime(timezone=True)),
        sa.Column("timing_error_s", sa.Float),
        sa.Column("first_timing_error_s", sa.Float),
        sa.Column("lead_time_s", sa.Float),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_predictions_device_status", "predictions", ["device_id", "status"])
    op.create_index(
        "ix_predictions_device_target_created", "predictions", ["device_id", "target_id", "created_at"]
    )
    op.create_index("ix_predictions_correlation", "predictions", ["correlation_key", "created_at"])
    op.create_index("ix_predictions_type_status", "predictions", ["prediction_type", "status"])
    op.create_index("ix_predictions_updated", "predictions", ["updated_at"])
    op.create_index("ix_predictions_expires", "predictions", ["expires_at"])


def downgrade() -> None:
    op.drop_table("predictions")
