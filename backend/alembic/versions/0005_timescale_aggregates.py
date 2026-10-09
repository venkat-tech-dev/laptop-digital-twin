"""Phase-2 retention tiers: 5-minute continuous aggregate over raw samples (TimescaleDB only).

Raw samples stay short (RETENTION_DAYS, default 30), the 5-minute aggregate is kept longer
(AGGREGATE_RETENTION_DAYS, default 365). Policies are (re)applied from settings at backend startup, so
changing the env vars needs no new migration. On plain PostgreSQL this migration is a no-op and
history is aggregated on the fly from raw samples.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _timescale_installed() -> bool:
    row = op.get_bind().execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).first()
    return row is not None


def upgrade() -> None:
    if not _timescale_installed():
        return
    # Continuous aggregates cannot be created inside a transaction block.
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_samples_5m
            WITH (timescaledb.continuous) AS
            SELECT time_bucket(INTERVAL '5 minutes', time) AS bucket,
                   metric_id,
                   avg(value) AS avg_value,
                   min(value) AS min_value,
                   max(value) AS max_value,
                   count(*)   AS samples
            FROM telemetry_samples
            GROUP BY bucket, metric_id
            WITH NO DATA
            """
        )
        op.execute(
            "SELECT add_continuous_aggregate_policy('telemetry_samples_5m', "
            "start_offset => INTERVAL '3 days', end_offset => INTERVAL '5 minutes', "
            "schedule_interval => INTERVAL '5 minutes', if_not_exists => true)"
        )
        op.execute("CALL refresh_continuous_aggregate('telemetry_samples_5m', NULL, now() - INTERVAL '5 minutes')")


def downgrade() -> None:
    if not _timescale_installed():
        return
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS telemetry_samples_5m")
