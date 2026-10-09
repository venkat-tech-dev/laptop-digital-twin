"""Phase-7 diagnosis APIs.

Authorization is server-side: every diagnosis, job and trigger resolves to its device first and the
caller must be allowed to see that device (otherwise 404, like an unknown id). Requesting a diagnosis
needs the operator role, or an employee account the device is assigned to. Requests are asynchronous
(202 + job); nothing here runs a model inline, accepts a prompt, or changes the endpoint.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.access import Staff, check_device
from app.api.deps import ContainerDep, Reader, require_platform_scope
from app.domain.diagnosis.models import DiagnosisStatus, DiagnosisType
from app.repositories.diagnoses import DiagnosisFilter
from app.services.diagnosis import FEEDBACK_VERDICTS, DiagnosisService, QueueFullError

device_router = APIRouter(prefix="/devices", tags=["diagnosis"])
diagnosis_router = APIRouter(prefix="/diagnoses", tags=["diagnosis"])
trigger_router = APIRouter(tags=["diagnosis"])
job_router = APIRouter(prefix="/diagnosis-jobs", tags=["diagnosis"])
config_router = APIRouter(prefix="/diagnosis-config", tags=["diagnosis"])

Verdict = Literal["HELPFUL", "NOT_HELPFUL", "CORRECT", "INCORRECT", "PARTIALLY_CORRECT"]
assert set(Verdict.__args__) == set(FEEDBACK_VERDICTS)  # type: ignore[attr-defined]


def _svc(container: Any) -> DiagnosisService:
    if container.diagnosis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Diagnosis is disabled")
    svc: DiagnosisService = container.diagnosis
    return svc


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown {what}")


def _visible(container: Any, principal: Any, device_id: str, what: str) -> None:
    try:
        check_device(principal, container, device_id)
    except HTTPException as exc:
        raise _not_found(what) from exc


def _may_request(principal: Any) -> None:
    """Operators and above for any visible device; employees for their assigned device (checked by
    ``_visible``); viewers are read-only."""
    if principal.role == "viewer":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required to request a diagnosis")


class DiagnoseRequest(BaseModel):
    force: bool = False  # bypass the fingerprint cache (operators only)


class FeedbackIn(BaseModel):
    verdict: Verdict
    actual_cause: str | None = Field(default=None, max_length=500)
    note: str | None = Field(default=None, max_length=1000)


# ------------------------------------------------------------------------------ read
@device_router.get("/{device_id}/diagnoses", summary="Diagnoses of a device (newest first)")
async def device_diagnoses(
    device_id: str,
    container: ContainerDep,
    principal: Reader,
    current: bool = True,
    status_: list[DiagnosisStatus] = Query(default=[], alias="status"),
    type_: list[DiagnosisType] = Query(default=[], alias="type"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    _visible(container, principal, device_id, "device")
    if not container.twin.has_device(device_id):
        raise _not_found("device")
    svc = _svc(container)
    f = DiagnosisFilter(current_only=current, statuses=tuple(s.value for s in status_),
                        types=tuple(t.value for t in type_))  # fmt: skip
    rows = await svc.repo.search(device_id, f, limit, offset)
    return {"device_id": device_id, "items": [d.to_dict(full=False) for d in rows], "limit": limit,
            "offset": offset}  # fmt: skip


@diagnosis_router.get("/{diagnosis_id}", summary="Diagnosis detail: evidence, hypotheses, versions, feedback")
async def diagnosis_detail(diagnosis_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    svc = _svc(container)
    d = await svc.repo.get(diagnosis_id[:64])
    if d is None:
        raise _not_found("diagnosis")
    _visible(container, principal, d.device_id, "diagnosis")
    out = await svc.detail(d.diagnosis_id)
    assert out is not None
    return out


@diagnosis_router.get("", summary="Diagnoses for one alert / anomaly / prediction")
async def diagnoses_for_trigger(
    container: ContainerDep,
    principal: Reader,
    alert_id: str | None = Query(default=None, max_length=64),
    anomaly_id: str | None = Query(default=None, max_length=64),
    prediction_id: str | None = Query(default=None, max_length=64),
    current: bool = True,
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    svc = _svc(container)
    kind, tid = next(
        (
            (k, v)
            for k, v in (("alert", alert_id), ("anomaly", anomaly_id), ("prediction", prediction_id))
            if v
        ),
        (None, None),
    )
    if kind is None or tid is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "alert_id, anomaly_id or prediction_id required"
        )
    device_id = await svc.trigger_device(kind, tid)
    if device_id is None:
        rows = await svc.repo.search(
            None, DiagnosisFilter(alert_id=alert_id, anomaly_id=anomaly_id, prediction_id=prediction_id), 1
        )
        device_id = rows[0].device_id if rows else None
    if device_id is None:
        raise _not_found(kind)
    _visible(container, principal, device_id, kind)
    f = DiagnosisFilter(
        current_only=current, alert_id=alert_id, anomaly_id=anomaly_id, prediction_id=prediction_id
    )
    rows = await svc.repo.search(device_id, f, limit)
    return {"device_id": device_id, "items": [d.to_dict(full=False) for d in rows]}


# ------------------------------------------------------------------------------ requests
async def _request(
    container: Any, principal: Any, kind: str, trigger_id: str, body: DiagnoseRequest | None
) -> dict[str, Any]:
    svc = _svc(container)
    device_id = await svc.trigger_device(kind, trigger_id[:64])
    if device_id is None:
        raise _not_found(kind)
    _visible(container, principal, device_id, kind)
    _may_request(principal)
    force = bool(body and body.force)
    if force and not principal.has_role("operator"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required to bypass the cache")
    try:
        job = svc.request(kind, trigger_id[:64], device_id, principal.subject, force=force)
    except QueueFullError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, str(exc), headers={"Retry-After": "30"}
        ) from exc
    return {"job": job.public(), "status_url": f"/api/v1/diagnosis-jobs/{job.job_id}"}


@trigger_router.post(
    "/alerts/{alert_id}/diagnose", status_code=202, summary="Request a diagnosis of an alert"
)
async def diagnose_alert(
    alert_id: str, container: ContainerDep, principal: Reader, body: DiagnoseRequest | None = None
) -> dict[str, Any]:
    return await _request(container, principal, "alert", alert_id, body)


@trigger_router.post(
    "/anomalies/{anomaly_id}/diagnose", status_code=202, summary="Request a diagnosis of an anomaly"
)
async def diagnose_anomaly(
    anomaly_id: str, container: ContainerDep, principal: Reader, body: DiagnoseRequest | None = None
) -> dict[str, Any]:
    return await _request(container, principal, "anomaly", anomaly_id, body)


@trigger_router.post(
    "/predictions/{prediction_id}/diagnose", status_code=202, summary="Request a diagnosis of a prediction"
)
async def diagnose_prediction(
    prediction_id: str, container: ContainerDep, principal: Reader, body: DiagnoseRequest | None = None
) -> dict[str, Any]:
    return await _request(container, principal, "prediction", prediction_id, body)


@job_router.get("/{job_id}", summary="Status of a diagnosis job")
async def job_status(job_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    job = _svc(container).jobs.get(job_id[:64])
    if job is None:
        raise _not_found("job")
    _visible(container, principal, job.device_id, "job")
    return job.public()


@job_router.post("/{job_id}/cancel", summary="Cancel a queued or running diagnosis job")
async def job_cancel(job_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    svc = _svc(container)
    job = svc.jobs.get(job_id[:64])
    if job is None:
        raise _not_found("job")
    _visible(container, principal, job.device_id, "job")
    _may_request(principal)
    return {"cancelled": svc.cancel(job.job_id), "job": job.public()}


# ------------------------------------------------------------------------------ feedback
@diagnosis_router.post("/{diagnosis_id}/feedback", status_code=201, summary="Feedback on a diagnosis")
async def diagnosis_feedback(
    diagnosis_id: str, body: FeedbackIn, container: ContainerDep, principal: Reader
) -> dict[str, Any]:
    svc = _svc(container)
    d = await svc.repo.get(diagnosis_id[:64])
    if d is None:
        raise _not_found("diagnosis")
    _visible(container, principal, d.device_id, "diagnosis")
    return await svc.add_feedback(d, body.verdict, body.actual_cause, body.note, principal.subject)


# ------------------------------------------------------------------------------ status
@config_router.get("/status", summary="Diagnosis engine status: mode, model health, queue, statistics")
async def diagnosis_status(container: ContainerDep, principal: Staff) -> dict[str, Any]:
    require_platform_scope(principal)  # Phase 10: engine-wide counters and feedback span every organization
    return await _svc(container).status()
