"""Anomalies, analytics, predictions and what-if simulation endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.access import ScopedDevice, Staff, visible_devices
from app.api.deps import ContainerDep, Operator, Reader
from app.schemas.misc import SimulationOut, SimulationRequest
from app.schemas.twin import AnomalyOut

anomalies_router = APIRouter(prefix="/anomalies", tags=["anomalies"])
analytics_router = APIRouter(prefix="/analytics", tags=["analytics"])
simulation_router = APIRouter(prefix="/simulation", tags=["simulation (generated data)"])


@anomalies_router.get("", response_model=list[AnomalyOut])
async def list_anomalies(
    container: ContainerDep,
    device_id: ScopedDevice,
    status_: Literal["active", "resolved"] | None = Query(default=None, alias="status"),
    severity: Literal["info", "warning", "critical"] | None = None,
    since: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> Any:
    return await container.anomalies.query(device_id, status_, severity, since, limit)


@anomalies_router.get("/rules", summary="Configured detection rules (V1 rules + V2 statistical)")
async def anomaly_rules(container: ContainerDep, _: Reader) -> list[dict[str, Any]]:
    return container.anomalies.rules()


class AckIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@anomalies_router.post("/{anomaly_id}/acknowledge", summary="Acknowledge an anomaly (persisted)")
async def acknowledge(
    anomaly_id: str, body: AckIn, principal: Operator, container: ContainerDep
) -> dict[str, Any]:
    try:
        return await container.anomalies.acknowledge(anomaly_id, principal.subject, body.note)
    except LookupError as exc:
        raise _lookup(exc) from exc


@anomalies_router.delete("/{anomaly_id}/acknowledge", summary="Withdraw an acknowledgement")
async def unacknowledge(anomaly_id: str, _: Operator, container: ContainerDep) -> dict[str, Any]:
    try:
        return await container.anomalies.unacknowledge(anomaly_id)
    except LookupError as exc:
        raise _lookup(exc) from exc


@anomalies_router.get("/{anomaly_id}/analysis", summary="Root-cause correlation and detection confidence")
async def analysis(anomaly_id: str, principal: Reader, container: ContainerDep) -> dict[str, Any]:
    allowed = visible_devices(principal, container)
    if allowed is not None:  # employees: only anomalies of their own devices (else: unknown)
        owned = False
        for device_id in allowed:
            twin = container.twin.get(device_id) if container.twin.has_device(device_id) else None
            if twin is not None and any(a.anomaly_id == anomaly_id for a in twin.anomalies.active.values()):
                owned = True
                break
            stored = await container.event_repo.list_anomalies(device_id, None, None, None, 1000)
            if any(a.anomaly_id == anomaly_id for a in stored):
                owned = True
                break
        if not owned:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown anomaly")
    try:
        return await container.anomalies.analyze(anomaly_id)
    except LookupError as exc:
        raise _lookup(exc) from exc


def _lookup(exc: LookupError) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@analytics_router.get("/thermal")
async def thermal(
    container: ContainerDep,
    device_id: ScopedDevice,
    minutes: int = Query(default=30, ge=1, le=60 * 24 * 400),
    end: datetime | None = Query(default=None, description="Window end (ISO 8601); default now"),
) -> dict[str, Any]:
    try:
        return await container.analytics.thermal(device_id, minutes, end)
    except LookupError as exc:
        raise _lookup(exc) from exc


@analytics_router.get("/performance")
async def performance(
    container: ContainerDep,
    device_id: ScopedDevice,
    minutes: int = Query(default=30, ge=1, le=60 * 24 * 400),
    end: datetime | None = Query(default=None, description="Window end (ISO 8601); default now"),
) -> dict[str, Any]:
    try:
        return await container.analytics.performance(device_id, minutes, end)
    except LookupError as exc:
        raise _lookup(exc) from exc


@analytics_router.get(
    "/predictions", summary="Trend-based predictions with confidence and supporting metrics"
)
async def predictions(container: ContainerDep, device_id: ScopedDevice) -> dict[str, Any]:
    try:
        return await container.analytics.predictions(device_id)
    except LookupError as exc:
        raise _lookup(exc) from exc


@simulation_router.get("/scenarios")
async def scenarios(container: ContainerDep, _: Staff) -> list[dict[str, Any]]:
    return container.simulation.scenarios()


@simulation_router.post(
    "/run",
    response_model=SimulationOut,
    summary="Run a what-if scenario from the current live state (never touches the laptop)",
)
async def run_simulation(request: SimulationRequest, container: ContainerDep, _: Staff) -> Any:
    try:
        return container.simulation.run(
            request.scenario,
            request.duration_minutes * 60,
            request.overrides(),
            ambient_c=request.ambient_c,
            thermal_profile=request.thermal_profile,
            adapter_w=request.adapter_w,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
