"""Agent ingest API: registration, heartbeat, inventory, single batch and bulk upload.

Every agent request: authenticate (per-device token) -> per-device rate limit (429 + Retry-After)
-> body size / gzip limits (``IngestBodyMiddleware``) -> validate -> dedupe -> apply -> acknowledge.

Acknowledgement contract (bulk):
    {"accepted": n, "duplicates": n, "rejected": n, "last_sequence": n, "server_received_at": ts,
     "results": [{"batch_id": ..., "status": "accepted|duplicate|rejected", "detail": ...}]}
The agent deletes accepted and duplicate batches, dead-letters rejected ones (non-retryable: invalid
schema, unsupported schema version, timestamps far in the future) and retries everything else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Agent, ContainerDep, require_enrollment_key
from app.core.metrics import INGEST_BATCHES, INGEST_RATE_LIMITED
from app.schemas.ingest import (
    BulkAck,
    HeartbeatIn,
    HeartbeatOut,
    IngestAck,
    InventoryEnvelopeIn,
    RegisterIn,
    RegisterOut,
    TelemetryBatchIn,
)
from app.services.devices import CredentialStoreUnavailableError
from app.services.digital_twin import UnknownDeviceError
from app.services.ingest_pipeline import OverloadedError

router = APIRouter(prefix="/ingest", tags=["ingest (agent only)"])
register_router = APIRouter(prefix="/agent", tags=["ingest (agent only)"])
MAX_BULK = 500
UNKNOWN_DEVICE = "Unknown device: send hardware inventory first"


def _forbid_other_device(agent: Any, device_id: str) -> None:
    if not agent.allows(device_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Device token does not belong to this device")


def _err(status_code: int, code: str, message: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message}, headers=headers)


def _device_gate(container: Any, device_id: str, agent_version: str | None = None, batches: int = 1) -> None:
    """Phase 9: lifecycle (disabled / revoked / retired devices are refused), organisation status, blocked
    versions and the organisation's telemetry quota (429 + Retry-After: the agent keeps the batch queued)."""
    t = container.tenancy
    refused = t.ingest_allowed(device_id)
    if refused is not None:
        raise _err(403, refused, "This device is not allowed to send data (lifecycle / organization status)")
    org = t.org_of(device_id)
    if org is None:
        return
    if agent_version:
        agent = container.policies.effective("agent", org, device_id)[0]
        if agent["reject_blocked"] and agent_version in agent["blocked_versions"]:
            raise _err(403, "AGENT_VERSION_BLOCKED", f"Agent {agent_version} is blocked; upgrade the agent")
    ok, retry = t.check_rate(org, "telemetry_batches_per_min", cost=batches)
    if not ok:
        INGEST_RATE_LIMITED.inc()
        raise _err(
            429,
            "TENANT_TELEMETRY_QUOTA",
            "Organization telemetry quota exceeded; retry later",
            {"Retry-After": str(max(1, int(retry)))},
        )


def _overloaded(exc: OverloadedError) -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        exc.reason,
        headers={"Retry-After": str(max(1, round(exc.retry_after_s)))},
    )


def _rate_limit(container: Any, agent: Any, request: Request) -> None:
    key = agent.device_id or (request.client.host if request.client else "unknown")
    allowed, retry_after = container.agent_limiter.allow(key)
    if not allowed:
        INGEST_RATE_LIMITED.inc()
        container.ingest.counters.rate_limited += 1
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Agent rate limit exceeded",
            headers={"Retry-After": str(max(1, round(retry_after)))},
        )


@register_router.post(
    "/register",
    response_model=RegisterOut,
    dependencies=[Depends(require_enrollment_key)],
    summary="Exchange the enrollment key for a per-device token (re-registering rotates it)",
)
async def register(body: RegisterIn, container: ContainerDep, request: Request) -> RegisterOut:
    """Legacy enrollment (shared key): only into the ``default`` organisation, never re-activating a device an
    administrator disabled / revoked / retired, and never taking a device enrolled in another organisation."""
    from app.domain.tenancy.models import Lifecycle

    t = container.tenancy
    if not container.settings.allow_legacy_enrollment:
        raise _err(403, "LEGACY_ENROLLMENT_DISABLED", "Enroll with an organization enrollment token")
    rec = t.registry.get(body.device_id)
    if rec is not None and rec.org_id != "default":
        raise _err(409, "DEVICE_OWNED_ELSEWHERE", "This device is enrolled in an organization; use its token")
    if rec is not None and rec.lifecycle not in (Lifecycle.ACTIVE, Lifecycle.QUARANTINED):
        container.audit.record(
            "default",
            f"agent:{body.device_id}",
            "agent",
            "device.enrollment_failed",
            "device",
            resource_type="device",
            resource_id=body.device_id,
            result="DENIED",
            severity="WARNING",
            reason=f"legacy re-registration of a {rec.lifecycle.value} device",
            source="agent",
            ip=request.client.host if request.client else None,
        )
        raise _err(
            403, f"DEVICE_{rec.lifecycle.value}", "This device was disabled or revoked by an administrator"
        )
    from app.services.tenancy import TenancyError

    try:
        await t.ensure_device(body.device_id, "default", "legacy", "legacy-enrollment-key")
    except TenancyError as exc:
        raise _err(exc.status, exc.code, str(exc)) from exc
    ttl = container.policies.value("enrollment", "credential_ttl_days", org_id="default")
    try:
        token = await container.device_auth.register(body.device_id, ttl_days=int(ttl))
    except CredentialStoreUnavailableError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Credential store unavailable", headers={"Retry-After": "10"}
        ) from exc
    container.presence.known(body.device_id)
    container.audit.record(
        "default",
        f"agent:{body.device_id}",
        "agent",
        "device.registered",
        "device",
        resource_type="device",
        resource_id=body.device_id,
        source="agent",
        metadata={"method": "legacy_enrollment_key"},
    )
    return RegisterOut(device_id=body.device_id, device_token=token)


class EnrollIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enrollment_token: str = Field(min_length=10, max_length=128)
    device_id: str = Field(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    agent_version: str = Field(max_length=32)


@register_router.post(
    "/enroll", summary="Enroll with an organization enrollment token (single-use, expiring)"
)
async def enroll(body: EnrollIn, container: ContainerDep, request: Request) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    if not container.enroll_limiter.allow(ip)[0]:
        raise _err(429, "RATE_LIMITED", "Too many enrollment attempts", {"Retry-After": "60"})
    from app.services.tenancy import TenancyError

    try:
        rec, _tok = await container.tenancy.enroll(body.enrollment_token, body.device_id, ip)
    except TenancyError as exc:
        raise _err(exc.status, exc.code, str(exc)) from exc
    ttl = container.policies.value("enrollment", "credential_ttl_days", org_id=rec.org_id)
    try:
        token, expires = await container.device_auth.issue(body.device_id, int(ttl))
    except CredentialStoreUnavailableError as exc:
        raise _err(
            503, "CREDENTIAL_STORE_UNAVAILABLE", "Credential store unavailable", {"Retry-After": "10"}
        ) from exc
    container.presence.known(body.device_id)
    return {
        "device_id": body.device_id,
        "device_token": token,
        "organization_id": rec.org_id,
        "credential_expires_at": expires.isoformat(),
    }


@register_router.post(
    "/credentials/rotate", summary="Rotate this device's credential (device token required)"
)
async def rotate(container: ContainerDep, agent: Agent) -> dict[str, Any]:
    if agent.device_id is None:
        raise _err(403, "DEVICE_TOKEN_REQUIRED", "Rotation requires the current per-device token")
    _device_gate(container, agent.device_id, batches=0)
    org = container.tenancy.org_of(agent.device_id) or "default"
    ttl = container.policies.value("enrollment", "credential_ttl_days", org_id=org)
    token, expires = await container.device_auth.issue(agent.device_id, int(ttl))
    container.audit.record(
        org,
        f"agent:{agent.device_id}",
        "agent",
        "device.credential_rotated",
        "device",
        resource_type="device",
        resource_id=agent.device_id,
        source="agent",
    )
    return {"device_id": agent.device_id, "device_token": token, "credential_expires_at": expires.isoformat()}


@register_router.post(
    "/heartbeat",
    response_model=HeartbeatOut,
    summary="Agent liveness: drives ONLINE / STALE / OFFLINE presence; returns server time for drift",
)
async def heartbeat(
    body: HeartbeatIn, request: Request, container: ContainerDep, agent: Agent
) -> HeartbeatOut:
    _forbid_other_device(agent, body.device_id)
    _rate_limit(container, agent, request)
    _device_gate(container, body.device_id, body.agent_version, batches=0)
    now = datetime.now(UTC)
    info = body.model_dump(mode="json", exclude={"device_id", "schema_version"})
    info["received_at"] = now.isoformat()
    presence, change = container.presence.heartbeat(body.device_id, info, now)
    if change is not None:
        await container.bus.publish_all([change])
    status_of = (
        await container.ingest.classify(body.device_id, list(body.unconfirmed_batch_ids))
        if body.unconfirmed_batch_ids
        else None
    )
    return HeartbeatOut(
        server_time=now,
        presence=presence.presence.value,
        last_sequence_received=container.ingest.sequences.last_sequence(body.device_id),
        clock_offset_s=round((now - body.sent_at).total_seconds(), 3),
        credential_expires_at=container.device_auth.expires_at(body.device_id) if agent.device_id else None,
        durable_batch_ids=status_of["durable"] if status_of else None,
        pending_batch_ids=status_of["pending"] if status_of else None,
        unknown_batch_ids=status_of["unknown"] if status_of else None,
    )


@router.post("/inventory", response_model=IngestAck, status_code=status.HTTP_202_ACCEPTED)
async def ingest_inventory(
    envelope: InventoryEnvelopeIn, request: Request, container: ContainerDep, agent: Agent
) -> IngestAck:
    _forbid_other_device(agent, envelope.device_id)
    _rate_limit(container, agent, request)
    _device_gate(container, envelope.device_id, envelope.agent_version, batches=0)
    if container.tenancy.org_of(envelope.device_id) is None:  # enrollment-key ingest (legacy): default org
        await container.tenancy.ensure_device(envelope.device_id, "default", "legacy", "legacy-inventory")
    await container.telemetry.ingest_inventory(envelope, container.device_repo)
    container.sync.offer_inventory(envelope)
    container.presence.batch_received(envelope.device_id)
    return IngestAck(
        accepted=1, device_id=envelope.device_id, sequence=0, server_received_at=datetime.now(UTC)
    )


@router.post("/telemetry", response_model=IngestAck, status_code=status.HTTP_202_ACCEPTED)
async def ingest_telemetry(
    batch: TelemetryBatchIn, request: Request, container: ContainerDep, agent: Agent
) -> IngestAck:
    _forbid_other_device(agent, batch.device_id)
    _rate_limit(container, agent, request)
    _device_gate(container, batch.device_id, batch.agent_version)
    received_at = datetime.now(UTC)
    error = container.ingest.timestamp_error(batch, received_at)
    if error is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, error)
    try:
        container.ingest.admit_replay(replay_only=batch.replay)
    except OverloadedError as exc:
        raise _overloaded(exc) from exc
    try:
        result, accepted = await container.ingest.apply(batch, received_at)
    except UnknownDeviceError as exc:
        INGEST_BATCHES.labels("unknown_device").inc()
        # The agent reacts to 409 by re-sending its hardware inventory, then replays the batch.
        raise HTTPException(status.HTTP_409_CONFLICT, UNKNOWN_DEVICE) from exc
    return IngestAck(
        accepted=accepted,
        device_id=batch.device_id,
        sequence=batch.sequence,
        duplicate=result == "duplicate",
        server_received_at=received_at,
    )


@router.post(
    "/telemetry/bulk",
    response_model=BulkAck,
    status_code=status.HTTP_200_OK,
    summary="Upload up to 500 queued batches (gzip welcome); each is validated and acknowledged",
)
async def ingest_bulk(body: dict[str, Any], request: Request, container: ContainerDep, agent: Agent) -> Any:
    _rate_limit(container, agent, request)
    raw = body.get("batches")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_BULK:
        raise HTTPException(422, f"'batches' must be a list of 1..{MAX_BULK} telemetry batches")
    devices = {str(b.get("device_id")) for b in raw if isinstance(b, dict)}
    for d in devices:
        if agent.allows(d):
            _device_gate(
                container,
                d,
                None,
                batches=sum(1 for b in raw if isinstance(b, dict) and b.get("device_id") == d),
            )
    replay_only = all(isinstance(b, dict) and b.get("replay") is True for b in raw)
    try:
        container.ingest.admit_replay(replay_only=replay_only)
    except OverloadedError as exc:
        raise _overloaded(exc) from exc
    try:
        ack = await container.ingest.ingest_bulk(agent, raw, datetime.now(UTC))
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Device token does not belong to this device") from exc
    except UnknownDeviceError as exc:
        INGEST_BATCHES.labels("unknown_device").inc()
        raise HTTPException(status.HTTP_409_CONFLICT, UNKNOWN_DEVICE) from exc
    return JSONResponse(ack.model_dump(mode="json"))
