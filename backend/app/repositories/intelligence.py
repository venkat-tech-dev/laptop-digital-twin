"""Storage for learned anomaly-detection state: device baselines and versioned models.

Baselines are stored one row per (device, signal, context) so they can be inspected with plain SQL.
Model artifacts are JSON (``MultivariateModel.to_dict``) - never pickle, so a tampered row can at
worst produce a wrong score, never execute code. Old model versions are kept (inactive) for audit
and rollback; only one version per device and kind is active.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import delete, select, update

from app.domain.anomalies.baseline import BaselineStatus, ContextStats, SignalBaseline
from app.domain.anomalies.iforest import MultivariateModel
from app.domain.anomalies.stats import Summary
from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import AnomalyModelRow, DeviceBaselineRow


@dataclass(frozen=True, slots=True)
class StoredBaseline:
    baseline: SignalBaseline
    source_key: str | None


class IntelligenceRepository(Protocol):
    async def save_baseline(self, device_id: str, stored: StoredBaseline) -> None: ...

    async def load_baselines(self, device_id: str) -> dict[str, StoredBaseline]: ...

    async def save_model(self, model: MultivariateModel) -> None: ...

    async def active_model(self, device_id: str) -> MultivariateModel | None: ...

    async def list_models(self, device_id: str) -> list[dict[str, Any]]: ...


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _summary_dict(s: Summary) -> dict[str, float]:
    return {
        "count": s.count,
        "median": s.median,
        "mad": s.mad,
        "mean": s.mean,
        "std": s.std,
        "p05": s.p05,
        "p25": s.p25,
        "p50": s.p50,
        "p75": s.p75,
        "p90": s.p90,
        "p95": s.p95,
        "p99": s.p99,
    }


def _summary(d: dict[str, Any]) -> Summary:
    return Summary(
        count=int(d["count"]),
        median=float(d["median"]),
        mad=float(d["mad"]),
        mean=float(d["mean"]),
        std=float(d["std"]),
        p05=float(d["p05"]),
        p25=float(d["p25"]),
        p50=float(d["p50"]),
        p75=float(d["p75"]),
        p90=float(d["p90"]),
        p95=float(d["p95"]),
        p99=float(d["p99"]),
    )


def _model_meta(m: MultivariateModel, active: bool, created_at: datetime | None) -> dict[str, Any]:
    return {
        "model_id": m.model_id,
        "version": m.version,
        "kind": "iforest",
        "features": m.features,
        "n_train": m.n_train,
        "threshold": round(m.threshold, 5),
        "trained_from": m.trained_from,
        "trained_until": m.trained_until,
        "params": m.params,
        "active": active,
        "created_at": _iso(created_at),
    }


class MemoryIntelligenceRepository:
    def __init__(self) -> None:
        self.baselines: dict[str, dict[str, StoredBaseline]] = {}
        self.models: dict[str, list[tuple[MultivariateModel, datetime]]] = {}

    async def save_baseline(self, device_id: str, stored: StoredBaseline) -> None:
        self.baselines.setdefault(device_id, {})[stored.baseline.signal_id] = stored

    async def load_baselines(self, device_id: str) -> dict[str, StoredBaseline]:
        return dict(self.baselines.get(device_id, {}))

    async def save_model(self, model: MultivariateModel) -> None:
        versions = self.models.setdefault(model.device_id, [])
        versions.append((model, datetime.now(UTC)))
        del versions[:-10]  # bounded in memory

    async def active_model(self, device_id: str) -> MultivariateModel | None:
        versions = self.models.get(device_id)
        return versions[-1][0] if versions else None

    async def list_models(self, device_id: str) -> list[dict[str, Any]]:
        versions = self.models.get(device_id, [])
        return [_model_meta(m, i == len(versions) - 1, at) for i, (m, at) in enumerate(versions)][::-1]


class SqlIntelligenceRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def save_baseline(self, device_id: str, stored: StoredBaseline) -> None:
        b = stored.baseline
        now = datetime.now(UTC)
        async with self._db.sessions.begin() as session:
            await session.execute(
                delete(DeviceBaselineRow).where(
                    DeviceBaselineRow.device_id == device_id, DeviceBaselineRow.signal_id == b.signal_id
                )
            )
            contexts = b.contexts or {}
            rows = [
                DeviceBaselineRow(
                    device_id=device_id,
                    signal_id=b.signal_id,
                    context=key,
                    status=b.status.value,
                    version=b.version,
                    source_key=stored.source_key,
                    sample_count=b.sample_count,
                    excluded_count=b.excluded_count,
                    trained_from=b.trained_from,
                    trained_until=b.trained_until,
                    stats=_summary_dict(c.stats),
                    updated_at=now,
                )
                for key, c in contexts.items()
            ]
            if not rows:  # COLD: keep a marker row so the status is visible
                rows = [
                    DeviceBaselineRow(
                        device_id=device_id,
                        signal_id=b.signal_id,
                        context="-",
                        status=b.status.value,
                        version=b.version,
                        source_key=stored.source_key,
                        sample_count=b.sample_count,
                        excluded_count=b.excluded_count,
                        trained_from=None,
                        trained_until=None,
                        stats={},
                        updated_at=now,
                    )
                ]
            session.add_all(rows)

    async def load_baselines(self, device_id: str) -> dict[str, StoredBaseline]:
        async with self._db.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(DeviceBaselineRow).where(DeviceBaselineRow.device_id == device_id)
                    )
                )
                .scalars()
                .all()
            )
        out: dict[str, StoredBaseline] = {}
        for r in rows:
            cur = out.get(r.signal_id)
            if cur is None:
                cur = out[r.signal_id] = StoredBaseline(
                    SignalBaseline(
                        r.signal_id,
                        BaselineStatus(r.status),
                        r.version,
                        r.trained_from,
                        r.trained_until,
                        r.sample_count,
                        r.excluded_count,
                    ),
                    r.source_key,
                )
            if r.context != "-" and r.stats:
                cur.baseline.contexts[r.context] = ContextStats(r.context, _summary(r.stats))
        return out

    async def save_model(self, model: MultivariateModel) -> None:
        async with self._db.sessions.begin() as session:
            await session.execute(
                update(AnomalyModelRow)
                .where(AnomalyModelRow.device_id == model.device_id, AnomalyModelRow.kind == "iforest")
                .values(active=False)
            )
            session.add(
                AnomalyModelRow(
                    model_id=model.model_id,
                    device_id=model.device_id,
                    kind="iforest",
                    version=model.version,
                    features=model.features,
                    n_train=model.n_train,
                    threshold=model.threshold,
                    trained_from=datetime.fromisoformat(model.trained_from),
                    trained_until=datetime.fromisoformat(model.trained_until),
                    artifact=model.to_dict(),
                    active=True,
                    created_at=datetime.now(UTC),
                )
            )

    async def active_model(self, device_id: str) -> MultivariateModel | None:
        async with self._db.sessions() as session:
            row = (
                await session.execute(
                    select(AnomalyModelRow)
                    .where(AnomalyModelRow.device_id == device_id, AnomalyModelRow.active.is_(True))
                    .order_by(AnomalyModelRow.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return MultivariateModel.from_dict(row.artifact) if row is not None else None

    async def list_models(self, device_id: str) -> list[dict[str, Any]]:
        async with self._db.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(AnomalyModelRow)
                        .where(AnomalyModelRow.device_id == device_id)
                        .order_by(AnomalyModelRow.version.desc())
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
        return [_model_meta(MultivariateModel.from_dict(r.artifact), r.active, r.created_at) for r in rows]
