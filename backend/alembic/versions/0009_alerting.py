"""Phase-6: alerts, alert audit trail, notifications (durable delivery queue) and preferences.
Non-destructive: new tables only.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
OPEN = "status IN ('OPEN', 'ONGOING', 'ACKNOWLEDGED', 'SUPPRESSED')"


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column(
            "device_id", sa.String(64), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("alert_type", sa.String(48), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("priority", sa.SmallInteger, nullable=False),
        sa.Column("confidence", sa.Float),
        sa.Column("deduplication_key", sa.String(64), nullable=False),
        sa.Column("correlation_key", sa.String(160)),
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by", sa.String(64)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", sa.String(64)),
        sa.Column("suppressed_at", sa.DateTime(timezone=True)),
        sa.Column("suppressed_by", sa.String(64)),
        sa.Column("suppressed_until", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("escalation_level", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("next_escalation_at", sa.DateTime(timezone=True)),
        sa.Column("occurrences", sa.Integer, nullable=False, server_default="1"),
        sa.Column("metadata", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_alerts_tenant_status_created", "alerts", ["tenant_id", "status", "created_at"])
    op.create_index("ix_alerts_device_created", "alerts", ["device_id", "created_at"])
    op.create_index("ix_alerts_severity_created", "alerts", ["severity", "created_at"])
    op.create_index("ix_alerts_correlation", "alerts", ["correlation_key"])
    # at most one open alert per condition: duplicate alerts are impossible, not just unlikely
    op.create_index(
        "uq_alerts_open_dedupe",
        "alerts",
        ["tenant_id", "deduplication_key"],
        unique=True,
        postgresql_where=sa.text(OPEN),
    )

    op.create_table(
        "alert_audit",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("alert_id", sa.String(36), sa.ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("from_status", sa.String(16)),
        sa.Column("to_status", sa.String(16)),
        sa.Column("detail", sa.Text),
    )
    op.create_index("ix_alert_audit_alert_at", "alert_audit", ["alert_id", "at"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("alert_id", sa.String(36), sa.ForeignKey("alerts.id", ondelete="CASCADE")),
        sa.Column("user_id", sa.String(96), nullable=False),
        sa.Column("device_id", sa.String(64)),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("priority", sa.SmallInteger, nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("payload", JSONB),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32)),
        sa.Column("provider_message_id", sa.String(128)),
        sa.Column("attempt_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("deliver_after", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("failure_reason", sa.Text),
        sa.Column("last_error", sa.Text),
        sa.Column("escalation_level", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("history", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_notifications_idempotency", "notifications", ["idempotency_key"], unique=True)
    op.create_index("ix_notifications_user_created", "notifications", ["tenant_id", "user_id", "created_at"])
    op.create_index("ix_notifications_user_unread", "notifications", ["user_id", "read_at", "channel"])
    op.create_index("ix_notifications_due", "notifications", ["status", "next_retry_at", "deliver_after"])
    op.create_index("ix_notifications_alert", "notifications", ["alert_id"])

    op.create_table(
        "notification_preferences",
        sa.Column("user_id", sa.String(96), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("preferences", JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("notification_preferences")
    op.drop_table("notifications")
    op.drop_table("alert_audit")
    op.drop_table("alerts")
