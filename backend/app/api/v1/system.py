from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status

from app.api.access import ScopedDevice
from app.api.deps import ContainerDep, Reader
from app.api.v1.twin import NO_DEVICE
from app.core.container import VERSION
from app.core.security import AuthError
from app.schemas.misc import AuthConfigOut, SystemInfoOut, TokenOut, TokenRequest
from app.schemas.twin import ProcessListOut, SystemEventOut

router = APIRouter(prefix="/system", tags=["system"])
auth_router = APIRouter(prefix="/auth", tags=["auth"])

_SORT_KEYS = {
    "cpu": lambda p: p.get("cpu_percent") or 0.0,
    "memory": lambda p: p.get("memory_rss_bytes") or 0,
    "gpu": lambda p: p.get("gpu_percent") or 0.0,
    "disk": lambda p: (p.get("io_read_bytes_per_sec") or 0.0) + (p.get("io_write_bytes_per_sec") or 0.0),
}


@router.get("/processes", response_model=ProcessListOut, summary="Top processes (read-only)")
async def processes(
    container: ContainerDep,
    device_id: ScopedDevice,
    sort_by: Literal["cpu", "memory", "gpu", "disk"] = "cpu",
    limit: int = Query(default=15, ge=1, le=100),
) -> Any:
    twin = container.twin.get(device_id)
    if twin is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_DEVICE)
    snap = twin.processes or {}
    procs = sorted(snap.get("processes", []), key=_SORT_KEYS[sort_by], reverse=True)[:limit]
    return {
        "timestamp": snap.get("timestamp"),
        "source": snap.get("source"),
        "total_processes": snap.get("total_processes", 0),
        "sort_by": sort_by,
        "unavailable_fields": snap.get("unavailable_fields", {}),
        "details_collected": snap.get("details_collected", False),
        "processes": procs,
    }


@router.get("/events", response_model=list[SystemEventOut])
async def system_events(
    container: ContainerDep,
    device_id: ScopedDevice,
    limit: int = Query(default=100, ge=1, le=1000),
) -> Any:
    twin = container.twin.get(device_id)
    if twin is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_DEVICE)
    return [asdict(e) for e in await container.event_repo.list_system_events(twin.device.device_id, limit)]


@router.get("/info", response_model=SystemInfoOut, summary="Backend configuration summary (no secrets)")
async def info(container: ContainerDep, _: Reader) -> SystemInfoOut:
    redis = "disabled"
    if container.redis is not None:
        redis = "connected" if container.redis.connected else "unreachable"
    return SystemInfoOut(
        app_env=container.settings.app_env.value,
        version=VERSION,
        persistence=container.persistence_mode,
        timescaledb=bool(container.db and container.db.timescale),
        redis=redis,
        auth_mode=container.settings.auth_mode.value,
        websocket_clients=container.ws.count,
        persisted_samples=container.persister.written,
        persist_queue_depth=container.persister.depth,
        uptime_s=round(time.monotonic() - container.started_monotonic, 1),
        retention_days=container.settings.retention_days,
        retention_mechanism=(
            "TimescaleDB retention policy"
            if container.db is not None and container.db.timescale
            else "hourly purge task"
            if container.db is not None
            else "in-memory only (no persistence)"
        ),
        persist_sample_interval_s=container.settings.persist_sample_interval_s,
        history_aggregation="avg / min / max per time bucket (bucket size chosen per query)",
        persist_excluded_prefixes=container.settings.persist_exclude_prefixes,
    )


@auth_router.get("/config", response_model=AuthConfigOut, summary="Public: which auth mode the UI must use")
async def auth_config(container: ContainerDep) -> AuthConfigOut:
    accounts = container.settings.auth_mode.value == "accounts"
    setup = accounts and await container.admin.setup_required()
    return AuthConfigOut(
        mode=container.settings.auth_mode.value,
        setup_required=setup,
        setup_token_required=setup and bool(container.settings.setup_token),
    )


@auth_router.post("/token", response_model=TokenOut, summary="Exchange an API key for a short-lived JWT")
async def token(body: TokenRequest, container: ContainerDep) -> TokenOut:
    try:
        access, expires = container.auth.issue_jwt(body.api_key)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return TokenOut(access_token=access, expires_at=expires)
