"""Phase-8: remediations and their append-only, hash-chained audit trail.
Non-destructive: new tables only. UPDATE / DELETE on remediation_audit are rejected by a trigger.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "remediations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("action_type", sa.String(48), nullable=False),
        sa.Column("risk_level", sa.String(10), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("approved_by", sa.String(128)),
        sa.Column("alert_id", sa.String(36)),
        sa.Column("diagnosis_id", sa.String(36)),
        sa.Column("prediction_id", sa.String(36)),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("execution_id", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("body", JSONB, nullable=False),
        sa.UniqueConstraint("execution_id", name="uq_remediations_execution"),
    )
    for name, cols in (
        ("ix_remediations_device_created", ["device_id", "created_at"]),
        ("ix_remediations_status", ["status"]),
        ("ix_remediations_tenant_status", ["tenant_id", "status"]),
        ("ix_remediations_action_created", ["action_type", "created_at"]),
        ("ix_remediations_diagnosis", ["diagnosis_id"]),
        ("ix_remediations_alert", ["alert_id"]),
        ("ix_remediations_correlation", ["correlation_id"]),
    ):
        op.create_index(name, "remediations", cols)
    op.create_table(
        "remediation_audit",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("remediation_id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("from_status", sa.String(24)),
        sa.Column("to_status", sa.String(24)),
        sa.Column("detail", JSONB, nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_remediation_audit_remediation", "remediation_audit", ["remediation_id", "id"])
    op.create_index("ix_remediation_audit_at", "remediation_audit", ["at"])
    op.execute(
        """
        CREATE OR REPLACE FUNCTION remediation_audit_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'remediation_audit is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER remediation_audit_no_change BEFORE UPDATE OR DELETE ON remediation_audit "
        "FOR EACH ROW EXECUTE FUNCTION remediation_audit_append_only();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS remediation_audit_no_change ON remediation_audit;")
    op.execute("DROP FUNCTION IF EXISTS remediation_audit_append_only();")
    op.drop_table("remediation_audit")
    op.drop_table("remediations")
