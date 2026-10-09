"""Phase-5 forecasting APIs (read side + admin policy).

Authorization is server-side: device-scoped routes check the caller may see the device (employees:
assigned devices only; anything else answers like an unknown id), and a prediction id resolves to its
device before it is returned. There is deliberately no endpoint to train, run or upload a model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.api.access import Staff, check_device, visible_devices
from app.api.deps import Admin, ContainerDep, Reader
from app.repositories.predictions import CLOSED_STATUSES, PredictionFilter, calibration
from app.services.forecasting import ForecastService

device_router = APIRouter(prefix="/devices", tags=["predictions"])
prediction_router = APIRouter(prefix="/predictions", tags=["predictions"])
accuracy_router = APIRouter(prefix="/prediction-accuracy", tags=["predictions"])
config_router = APIRouter(prefix="/prediction-config", tags=["predictions"])

Status = Literal["ACTIVE", "UPDATED", "LOW_CONFIDENCE", "INVALIDATED", "EXPIRED", "CONFIRMED", "CANCELLED"]
TargetId = Literal["disk", "memory", "battery", "temperature", "cpu"]


def _svc(container: Any) -> ForecastService:
    if container.forecasts is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Forecasting is disabled")
    svc: ForecastService = container.forecasts
    return svc


def _device(container: Any, principal: Any, device_id: str) -> None:
    check_device(principal, container, device_id)
    if not container.twin.has_device(device_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown device")


@device_router.get(
    "/{device_id}/predictions", summary="Current forecast per metric (status, range, confidence)"
)
async def device_predictions(device_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    _device(container, principal, device_id)
    return _svc(container).current(device_id)


@device_router.get(
    "/{device_id}/predictions/history", summary="Prediction records with filters (newest first)"
)
async def device_prediction_history(
    device_id: str,
    container: ContainerDep,
    principal: Reader,
    active: bool | None = None,
    status_: list[Status] = Query(default=[], alias="status"),
    target: list[TargetId] = Query(default=[]),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    _device(container, principal, device_id)
    svc = _svc(container)
    items = await svc.repo.search(
        device_id, PredictionFilter(active, tuple(status_), tuple(target), since, until), limit, offset
    )
    live = {p.prediction_id: p for p in svc.tracker.active(device_id)}
    return {
        "device_id": device_id,
        "items": [(live.get(p.prediction_id) or p).to_dict() for p in items],
        "limit": limit,
        "offset": offset,
    }


@device_router.get("/{device_id}/predictions/{target_id}/forecast", summary="Actual history + forecast path")
async def device_forecast_curve(
    device_id: str, target_id: TargetId, container: ContainerDep, principal: Reader
) -> dict[str, Any]:
    _device(container, principal, device_id)
    out = _svc(container).detail_curve(device_id, target_id)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No forecast for this metric yet")
    return {"device_id": device_id, "target_id": target_id, **out}


@prediction_router.get("/{prediction_id}", summary="One prediction with its evidence and lifecycle")
async def get_prediction(prediction_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    svc = _svc(container)
    p = svc.find_active(prediction_id[:64]) or await svc.repo.get(prediction_id[:64])
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown prediction")
    try:
        check_device(principal, container, p.device_id)
    except HTTPException as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown prediction") from exc
    return p.to_dict()


@accuracy_router.get("", summary="Calibration of closed predictions (hit rate, timing error, lead time)")
async def prediction_accuracy(
    container: ContainerDep,
    principal: Staff,
    device_id: str | None = Query(default=None, max_length=64),
    since: datetime | None = None,
) -> dict[str, Any]:
    allowed = visible_devices(principal, container)
    if device_id is not None and allowed is not None and device_id not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown device")
    items = await _svc(container).repo.search(
        device_id, PredictionFilter(statuses=CLOSED_STATUSES, since=since), 5000
    )
    # Phase 10 fix: without a device_id the search spans every device; aggregates must still cover only
    # the caller's visible devices (otherwise other organizations' outcomes leak into the statistics)
    items = [p for p in items if p.device_id in allowed]
    return {"device_id": device_id, **calibration(items)}


class PredictionConfigIn(BaseModel):
    model_config = {"extra": "forbid"}

    policy: dict[str, float] | None = None
    targets: dict[TargetId, dict[str, Any]] | None = None


@config_router.get("", summary="Forecasting policy and per-metric settings (admin)")
async def get_prediction_config(container: ContainerDep, _: Admin) -> dict[str, Any]:
    svc = _svc(container)
    return {**svc.config(), "status": svc.status()}


@config_router.put("", summary="Change forecasting policy / thresholds (admin, validated, versioned)")
async def put_prediction_config(
    body: PredictionConfigIn, container: ContainerDep, admin: Admin
) -> dict[str, Any]:
    try:
        return await _svc(container).set_config(body.model_dump(exclude_none=True), admin.subject)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
