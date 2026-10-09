"""Fleet intelligence and operations views (Phase 10). Tenant-scoped: every figure covers only the
caller's visible devices (organization and group scope, Phase 9); platform-only figures (storage,
pipeline, notification backlog) are included only for platform scope."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.access import visible_devices
from app.api.deps import ContainerDep, Reader
from app.core.security import Principal

router = APIRouter(prefix="/fleet", tags=["fleet-intelligence"])


def _scope(principal: Principal, container: Any) -> tuple[str, set[str], bool]:
    if not principal.can("device.view"):
        raise HTTPException(403, {"code": "PERMISSION_DENIED", "message": "Permission device.view required"})
    platform = principal.org_id == "default" or principal.platform_admin
    return (
        principal.org_id,
        set(visible_devices(principal, container)),
        bool(platform and principal.has_role("admin")),
    )


@router.get("/health", summary="Explainable fleet health score with contributors and critical conditions")
async def fleet_health(principal: Reader, container: ContainerDep) -> dict[str, Any]:
    org, devices, _ = _scope(principal, container)
    return await container.fleet.health(org, devices)


@router.get("/insights", summary="Cross-device anomaly correlation, recurring issues, remediation outcomes")
async def fleet_insights(
    principal: Reader,
    container: ContainerDep,
    window_minutes: int = Query(default=30, ge=5, le=240),
    since_hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    org, devices, _ = _scope(principal, container)
    return await container.fleet.insights(org, devices, window_minutes, since_hours)


@router.get("/capacity", summary="Capacity projections from observed history (INSUFFICIENT_DATA when short)")
async def fleet_capacity(principal: Reader, container: ContainerDep) -> dict[str, Any]:
    org, devices, platform = _scope(principal, container)
    return await container.fleet.capacity(org, devices, platform)


@router.get("/operations", summary="IT operations summary of the visible fleet")
async def fleet_operations(principal: Reader, container: ContainerDep) -> dict[str, Any]:
    org, devices, platform = _scope(principal, container)
    return await container.fleet.operations(org, devices, platform)


@router.get("/models", summary="Model governance: detector false positives, forecast accuracy, diagnosis ops")
async def fleet_models(
    principal: Reader, container: ContainerDep, days: int = Query(default=30, ge=1, le=365)
) -> dict[str, Any]:
    from app.services.fleet import model_governance

    org, devices, platform = _scope(principal, container)
    return await model_governance(container, org, devices, platform, days)
