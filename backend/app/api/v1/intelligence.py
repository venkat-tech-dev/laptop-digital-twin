"""Phase-4 anomaly intelligence APIs (read side, operator feedback, admin policy).

Authorization is enforced here, server-side: every device-scoped route checks the caller may see the
device (employees: assigned devices only; anything else is answered like an unknown id), and an
anomaly id resolves to its device before anything is returned.

Deliberately absent: endpoints that train models, run detectors on demand, upload models or execute
anything. Training is scheduled by the backend; the browser can only read results, give feedback
and (admins) change validated numeric policy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.access import check_device
from app.api.deps import Admin, ContainerDep, Operator, Reader
from app.repositories.base import AnomalyFilter
from app.services.intelligence import IntelligenceService

device_router = APIRouter(prefix="/devices", tags=["anomaly intelligence"])
anomaly_router = APIRouter(prefix="/anomalies", tags=["anomaly intelligence"])
config_router = APIRouter(prefix="/anomaly-config", tags=["anomaly intelligence"])

Level = Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
Kind = Literal["threshold_anomaly", "behavioral_anomaly", "volatility_anomaly", "multivariate_anomaly"]


def _device(container: Any, principal: Any, device_id: str) -> None:
    check_device(principal, container, device_id)
    if not container.twin.has_device(device_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown device")


def _intel(container: Any) -> IntelligenceService:
    if container.intelligence is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Anomaly intelligence is disabled")
    intel: IntelligenceService = container.intelligence
    return intel


@device_router.get("/{device_id}/anomalies", summary="Anomaly history with filters (newest first)")
async def device_anomalies(
    device_id: str,
    container: ContainerDep,
    principal: Reader,
    status_: Literal["active", "resolved"] | None = Query(default=None, alias="status"),
    level: list[Level] = Query(default=[]),
    type_: list[Kind] = Query(default=[], alias="type"),
    since: datetime | None = None,
    until: datetime | None = None,
    min_confidence: float | None = Query(default=None, ge=0, le=1),
    signal_id: str | None = Query(default=None, max_length=32, pattern=r"^[a-z_]+$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    _device(container, principal, device_id)
    filters = AnomalyFilter(
        status=status_,
        levels=tuple(level),
        types=tuple(type_),
        since=since,
        until=until,
        min_confidence=min_confidence,
        signal_id=signal_id,
    )
    return await container.anomalies.history(device_id, filters, limit, offset)


@device_router.get("/{device_id}/anomalies/active", summary="Active anomalies (all types)")
async def device_active_anomalies(
    device_id: str, container: ContainerDep, principal: Reader
) -> dict[str, Any]:
    _device(container, principal, device_id)
    return {"device_id": device_id, "items": await container.anomalies.active(device_id)}


@device_router.get("/{device_id}/anomaly-summary", summary="Counts, highest severity, detection mode")
async def device_anomaly_summary(
    device_id: str, container: ContainerDep, principal: Reader
) -> dict[str, Any]:
    _device(container, principal, device_id)
    return _intel(container).summary(device_id)


@device_router.get("/{device_id}/baseline", summary="Learned baselines, their status and model versions")
async def device_baseline(device_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    _device(container, principal, device_id)
    return await _intel(container).baseline(device_id)


@anomaly_router.get("/{anomaly_id}", summary="One anomaly with its full evidence")
async def get_anomaly(anomaly_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    out = await container.anomalies.get(anomaly_id[:64])
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown anomaly")
    try:
        check_device(principal, container, out["device_id"])
    except HTTPException as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown anomaly") from exc
    return out


class FeedbackIn(BaseModel):
    verdict: Literal["true_positive", "false_positive", "unsure"]
    note: str | None = Field(default=None, max_length=500)


@anomaly_router.post("/{anomaly_id}/feedback", summary="Operator verdict on a detection (quality loop)")
async def anomaly_feedback(
    anomaly_id: str, body: FeedbackIn, container: ContainerDep, principal: Operator
) -> dict[str, Any]:
    anomaly = await container.anomalies.find(anomaly_id[:64])
    if anomaly is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown anomaly")
    fb = await _intel(container).feedback(anomaly, body.verdict, principal.subject, body.note)
    return {"anomaly_id": anomaly.anomaly_id, "feedback": fb}


class AnomalyConfigIn(BaseModel):
    """Only these keys are editable (validated ranges are enforced again in the service)."""

    model_config = {"extra": "forbid"}

    z_trigger: float | None = None
    z_recover: float | None = None
    z_trigger_cold: float | None = None
    persistence_s: float | None = None
    recovery_s: float | None = None
    cooldown_s: float | None = None
    shift_z: float | None = None
    volatility_ratio: float | None = None
    correlation_window_s: float | None = None
    expire_after_s: float | None = None
    contamination_min_confidence: float | None = None
    iforest_threshold_quantile: float | None = None
    iforest_enabled: bool | None = None
    process_context: bool | None = None
    enabled_detectors: list[Literal["robust_z", "quantile", "ewma_shift", "iforest"]] | None = None


@config_router.get("", summary="Anomaly detection policy (admin)")
async def get_anomaly_config(container: ContainerDep, _: Admin) -> dict[str, Any]:
    return {**_intel(container).config(), "status": _intel(container).status()}


@config_router.put("", summary="Change anomaly detection policy (admin, audited)")
async def put_anomaly_config(body: AnomalyConfigIn, container: ContainerDep, admin: Admin) -> dict[str, Any]:
    try:
        return await _intel(container).set_config(body.model_dump(exclude_none=True), admin.subject)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
