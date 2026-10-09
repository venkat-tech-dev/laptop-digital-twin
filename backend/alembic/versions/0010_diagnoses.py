"""Phase-7: diagnoses (versioned) and diagnosis feedback. Non-destructive: new tables only.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "diagnoses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("series_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column(
            "device_id", sa.String(64), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("trigger_kind", sa.String(16), nullable=False),
        sa.Column("trigger_id", sa.String(64)),
        sa.Column("alert_id", sa.String(36)),
        sa.Column("anomaly_id", sa.String(64)),
        sa.Column("prediction_id", sa.String(36)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("diagnosis_type", sa.String(32), nullable=False),
        sa.Column("category", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(10)),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("likely_cause", sa.Text),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("confidence_level", sa.String(16), nullable=False),
        sa.Column("reasoning_model", sa.String(96), nullable=False),
        sa.Column("model_version", sa.String(96), nullable=False),
        sa.Column("prompt_version", sa.String(32)),
        sa.Column("context_fingerprint", sa.String(64), nullable=False),
        sa.Column("supersedes", sa.String(36)),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("expires_at", TS),
        sa.Column("body", JSONB, nullable=False),
        sa.UniqueConstraint("series_id", "version", name="uq_diagnoses_series_version"),
    )
    op.create_index("ix_diagnoses_device_created", "diagnoses", ["device_id", "created_at"])
    op.create_index("ix_diagnoses_device_fingerprint", "diagnoses", ["device_id", "context_fingerprint"])
    op.create_index("ix_diagnoses_alert", "diagnoses", ["alert_id"])
    op.create_index("ix_diagnoses_anomaly", "diagnoses", ["anomaly_id"])
    op.create_index("ix_diagnoses_prediction", "diagnoses", ["prediction_id"])
    op.create_index("ix_diagnoses_status", "diagnoses", ["status"])
    op.create_index("ix_diagnoses_expires", "diagnoses", ["expires_at"])
    op.create_table(
        "diagnosis_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "diagnosis_id", sa.String(36), sa.ForeignKey("diagnoses.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("series_id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("diagnosis_type", sa.String(32), nullable=False),
        sa.Column("verdict", sa.String(24), nullable=False),
        sa.Column("actual_cause", sa.String(500)),
        sa.Column("note", sa.String(1000)),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_diagnosis_feedback_diagnosis", "diagnosis_feedback", ["diagnosis_id"])
    op.create_index("ix_diagnosis_feedback_verdict_created", "diagnosis_feedback", ["verdict", "created_at"])


def downgrade() -> None:
    op.drop_table("diagnosis_feedback")
    op.drop_table("diagnoses")
