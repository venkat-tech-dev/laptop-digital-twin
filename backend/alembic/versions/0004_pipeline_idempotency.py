"""Phase-2 pipeline: durable ingest receipts, idempotent device events, event priority/category.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_receipts",
        sa.Column("batch_id", sa.String(64), primary_key=True),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("sequence", sa.BigInteger, nullable=False),
        sa.Column("schema_version", sa.String(8), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("samples", sa.Integer, nullable=False),
        sa.Column("events", sa.Integer, nullable=False),
        sa.Column("replay", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_ingest_receipts_received", "ingest_receipts", ["received_at"])
    op.create_index("ix_ingest_receipts_device_seq", "ingest_receipts", ["device_id", "sequence"])

    op.add_column("system_events", sa.Column("event_uid", sa.String(64), nullable=True))
    op.add_column("system_events", sa.Column("priority", sa.String(16), nullable=True))
    op.add_column("system_events", sa.Column("category", sa.String(32), nullable=True))
    # Backfill uids of device events persisted by Phase 1 (event_id lived in the JSON payload), keeping
    # the first copy of any duplicate so the unique constraint can be created.
    op.execute(
        """
        UPDATE system_events SET event_uid = data->>'event_id'
        WHERE event_type LIKE 'device.%' AND data ? 'event_id'
          AND id IN (SELECT min(id) FROM system_events
                     WHERE event_type LIKE 'device.%' AND data ? 'event_id'
                     GROUP BY data->>'event_id')
        """
    )
    op.create_unique_constraint("uq_system_events_event_uid", "system_events", ["event_uid"])


def downgrade() -> None:
    op.drop_constraint("uq_system_events_event_uid", "system_events", type_="unique")
    for col in ("category", "priority", "event_uid"):
        op.drop_column("system_events", col)
    op.drop_table("ingest_receipts")
