"""Phase-10: hourly fleet health snapshots per organization (historical comparison of the fleet score).

Expand-only (a new table; nothing existing is altered), so it is safe while an older backend version is
still running during a rolling deployment. Downgrade drops the table (snapshots can be recomputed only
going forward).

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fleet_health_snapshots",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("score", sa.SmallInteger, nullable=False),
        sa.Column("coverage", sa.Float, nullable=False),
        sa.Column("devices", sa.Integer, nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_fleet_health_org_at", "fleet_health_snapshots", ["org_id", "at"])


def downgrade() -> None:
    op.drop_index("ix_fleet_health_org_at", table_name="fleet_health_snapshots")
    op.drop_table("fleet_health_snapshots")
