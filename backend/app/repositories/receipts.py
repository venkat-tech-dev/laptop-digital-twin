"""Durable ingest receipts (PostgreSQL). In memory mode there is no receipt store: de-duplication
then only spans the backend's lifetime, which the in-memory repositories cannot outlive anyway."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import IngestReceiptRow
from app.repositories.sql import _Timed

if TYPE_CHECKING:
    from app.services.ingest_pipeline import Receipt


class SqlReceiptRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def write_receipts(self, receipts: list[Receipt]) -> int:
        if not receipts:
            return 0
        rows = [
            {
                "batch_id": r.batch_id,
                "device_id": r.device_id,
                "sequence": r.sequence,
                "schema_version": r.schema_version,
                "collected_at": r.collected_at,
                "received_at": r.received_at,
                "samples": r.samples,
                "events": r.events,
                "replay": r.replay,
            }
            for r in receipts
        ]
        with _Timed("receipt_insert"):
            async with self._db.sessions.begin() as session:
                for i in range(0, len(rows), 1000):
                    await session.execute(
                        pg_insert(IngestReceiptRow).values(rows[i : i + 1000]).on_conflict_do_nothing()
                    )
        return len(rows)

    async def existing_receipt_ids(self, device_id: str, batch_ids: list[str]) -> list[str]:
        if not batch_ids:
            return []
        async with self._db.sessions() as session:
            rows = await session.execute(
                select(IngestReceiptRow.batch_id).where(
                    IngestReceiptRow.device_id == device_id, IngestReceiptRow.batch_id.in_(batch_ids)
                )
            )
        return [r[0] for r in rows]

    async def recent_receipt_ids(self, since: datetime, limit: int) -> list[str]:
        stmt = (
            select(IngestReceiptRow.batch_id)
            .where(IngestReceiptRow.received_at >= since)
            .order_by(IngestReceiptRow.received_at.desc())
            .limit(limit)
        )
        async with self._db.sessions() as session:
            return list((await session.execute(stmt)).scalars().all())

    async def purge_receipts_older_than(self, cutoff: datetime) -> int:
        with _Timed("receipt_purge"):
            async with self._db.sessions.begin() as session:
                result = await session.execute(
                    delete(IngestReceiptRow).where(IngestReceiptRow.received_at < cutoff)
                )
        return int(getattr(result, "rowcount", 0) or 0)
