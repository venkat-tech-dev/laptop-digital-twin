from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.api.access import ScopedDevice
from app.api.deps import ContainerDep
from app.api.v1.twin import NO_DEVICE
from app.schemas.twin import HistoryOut, HistoryPointOut, MetricDefOut, MetricReadingOut, RecentOut

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


def resolve_window(minutes: int, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """Explicit start/end (timezone-aware) win over ``minutes``. Raises 422 on an invalid window."""
    end = (
        (end or datetime.now(UTC)).astimezone(UTC)
        if end and end.tzinfo
        else (end or datetime.now(UTC)).replace(tzinfo=UTC)
    )
    if start is not None:
        start = start.astimezone(UTC) if start.tzinfo else start.replace(tzinfo=UTC)
    else:
        start = end - timedelta(minutes=minutes)
    if start >= end:
        raise HTTPException(422, "start must be before end")
    if end - start > timedelta(days=400):
        raise HTTPException(422, "Window longer than 400 days")
    return start, end


def _device(container: ContainerDep, device_id: str | None) -> str:
    twin = container.twin.get(device_id)
    if twin is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_DEVICE)
    return twin.device.device_id


@router.get("/latest", response_model=dict[str, MetricReadingOut], summary="Latest reading of every metric")
async def latest(container: ContainerDep, device_id: ScopedDevice, component_id: str | None = None) -> Any:
    twin = container.twin.get(device_id)
    if twin is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_DEVICE)
    now = datetime.now(UTC)
    s = container.settings
    out: dict[str, Any] = {}
    for comp in twin.components.values():
        if component_id and comp.component_id != component_id:
            continue
        for key, r in comp.telemetry.items():
            out[key] = r.to_dict(now, s.degraded_after_s, s.stale_after_s)
    return dict(sorted(out.items()))


@router.get("/history", response_model=HistoryOut, summary="Persisted history, aggregated into time buckets")
async def history(
    container: ContainerDep,
    device_id: ScopedDevice,
    keys: list[str] = Query(
        ..., min_length=1, max_length=20, description="Metric keys, e.g. cpu.usage_percent"
    ),
    minutes: int = Query(default=30, ge=1, le=60 * 24 * 400),
    start: datetime | None = Query(default=None, description="Window start (ISO 8601); overrides minutes"),
    end: datetime | None = Query(default=None, description="Window end (ISO 8601); default now"),
    bucket_seconds: int | None = Query(default=None, ge=1, le=86400),
) -> HistoryOut:
    did = _device(container, device_id)
    start, end = resolve_window(minutes, start, end)
    span_s = (end - start).total_seconds()
    bucket = bucket_seconds or max(5, int(span_s / 360))  # ~360 points per series by default
    series = await container.telemetry.history(did, keys, start, end, bucket)
    return HistoryOut(
        device_id=did,
        start=start,
        end=end,
        bucket_seconds=bucket,
        series={
            k: [HistoryPointOut(t=p.time, avg=p.avg, min=p.min, max=p.max, n=p.count) for p in v]
            for k, v in series.items()
        },
    )


@router.get("/recent", response_model=RecentOut, summary="Short-term buffer for chart backfill")
async def recent(
    container: ContainerDep,
    device_id: ScopedDevice,
    seconds: int = Query(default=300, ge=10, le=1800),
) -> RecentOut:
    did = _device(container, device_id)
    return RecentOut(device_id=did, seconds=seconds, points=await container.telemetry.recent(did, seconds))


@router.get("/catalog", response_model=list[MetricDefOut], summary="Persisted time series for this device")
async def catalog(container: ContainerDep, device_id: ScopedDevice) -> Any:
    did = _device(container, device_id)
    return [asdict(m) for m in await container.telemetry.catalog(did)]
