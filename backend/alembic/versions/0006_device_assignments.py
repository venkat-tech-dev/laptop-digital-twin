"""Phase-3: device ownership (employee accounts see only their assigned devices).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_assignments",
        sa.Column("device_id", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(64)),
        sa.Column("employee_name", sa.String(128)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(64)),
    )
    op.create_index("ix_device_assignments_username", "device_assignments", ["username"])


def downgrade() -> None:
    op.drop_table("device_assignments")
