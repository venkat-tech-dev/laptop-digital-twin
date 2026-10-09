"""Phase-8 remediation APIs.

Authorization is server-side on every route: the caller's permissions (Phase 9 permission model), the device
visibility of the caller (employees: assigned devices only; others answer 404 like an unknown id), the
risk ceiling per role and the four-eyes rule. The agent endpoints accept only a per-device token for the
same device; an enrollment key can never pull or report actions.

There is no endpoint that accepts a command, script, path, URL or registry operation: a request names a
catalog action and its typed parameters, nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.access import check_device, visible_devices
from app.api.deps import AgentIdentity, ContainerDep, Reader, require_agent, require_platform_scope
from app.domain.remediation.catalog import CATALOG
from app.domain.remediation.models import Mode, Remediation
from app.repositories.remediation import RemediationFilter
from app.services.remediation import RemediationError, RemediationService

router = APIRouter(prefix="/remediations", tags=["remediation"])
catalog_router = APIRouter(prefix="/action-catalog", tags=["remediation"])
policy_router = APIRouter(prefix="/remediation-policy", tags=["remediation"])
admin_router = APIRouter(prefix="/remediation-admin", tags=["remediation"])
agent_router = APIRouter(prefix="/agent/actions", tags=["ingest (agent only)"])

AgentDep = Annotated[AgentIdentity, Depends(require_agent)]
StatusLit = Literal[
    "PROPOSED",
    "PENDING_APPROVAL",
    "APPROVED",
    "REJECTED",
    "QUEUED",
    "VALIDATING",
    "EXECUTING",
    "VERIFYING",
    "SUCCEEDED",
    "PARTIALLY_SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
    "ROLLED_BACK",
    "ROLLBACK_FAILED",
]


def _svc(container: Any) -> RemediationService:
    if container.remediation is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Remediation is disabled")
    svc: RemediationService = container.remediation
    return svc


def _need(principal: Any, permission: str) -> None:
    if not principal.can(permission):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Permission {permission} required")


def _http(exc: RemediationError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "message": str(exc)})


async def _get(container: Any, principal: Any, remediation_id: str) -> Remediation:
    _need(principal, "remediation.view")
    svc = _svc(container)
    r = svc.items.get(remediation_id[:64]) or await svc.repo.get(remediation_id[:64])
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown remediation")
    try:
        check_device(principal, container, r.device_id)
    except HTTPException as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown remediation") from exc
    return r


def _view(r: Remediation, svc: RemediationService, principal: Any, full: bool = True) -> dict[str, Any]:
    out = r.to_dict(full=full)
    action = CATALOG.get(r.action_type)
    if action is not None:
        out["action"] = {
            "risk_level": action.risk.value,
            "impact": action.impact,
            "rollback": action.rollback,
            "reversible": action.public()["reversible"],
            "changes_state": action.changes_state,
            "estimated_duration_s": action.estimated_duration_s,
            "timeouts_s": action.public()["timeouts_s"],
        }
        out["allowed"] = {
            "approve": r.status.value == "PENDING_APPROVAL"
            and principal.can("remediation.approve")
            and not (svc.policy_for(r.device_id).four_eyes(action) and r.requested_by == principal.subject),
            "reject": r.status.value == "PENDING_APPROVAL" and principal.can("remediation.approve"),
            "cancel": r.is_open
            and r.status.value not in ("EXECUTING", "VERIFYING")
            and principal.can("remediation.cancel"),
        }
    if full:
        env = (out.get("execution") or {}).get("envelope")
        if env:  # the signature itself is not needed by people; keep the digest
            out["execution"] = {
                **out["execution"],
                "envelope": {k: v for k, v in env.items() if k != "signature"},
            }
    return out


# --------------------------------------------------------------------------------- read
@router.get("", summary="Remediations (newest first) with filters")
async def list_remediations(
    container: ContainerDep,
    principal: Reader,
    device_id: str | None = Query(default=None, max_length=64),
    status_: list[StatusLit] = Query(default=[], alias="status"),
    action_type: str | None = Query(default=None, max_length=64),
    risk: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] | None = None,
    requested_by: str | None = Query(default=None, max_length=128),
    diagnosis_id: str | None = Query(default=None, max_length=64),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    _need(principal, "remediation.view")
    svc = _svc(container)
    allowed = visible_devices(principal, container)
    if device_id is not None and allowed is not None and device_id not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown device")
    f = RemediationFilter(
        device_id,
        frozenset(allowed) if allowed is not None else None,
        tuple(status_),
        action_type,
        risk,
        requested_by,
        diagnosis_id,
        since,
        until,
    )
    rows = await svc.repo.search(f, limit, offset)
    rows = [svc.items.get(r.remediation_id, r) for r in rows]  # live state wins
    return {
        "items": [_view(r, svc, principal, full=False) for r in rows],
        "limit": limit,
        "offset": offset,
        "permissions": sorted(p for p in principal.permissions if p.startswith("remediation.")),
    }


@router.get("/{remediation_id}", summary="One remediation: preview, approval, execution, verification, audit")
async def get_remediation(remediation_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    r = await _get(container, principal, remediation_id)
    return _view(r, _svc(container), principal)


@router.get(
    "/{remediation_id}/execution", summary="Execution record (envelope digest, dispatch, agent report)"
)
async def get_execution(remediation_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    r = await _get(container, principal, remediation_id)
    v = _view(r, _svc(container), principal)
    return {
        "remediation_id": r.remediation_id,
        "execution_id": r.execution_id,
        "status": r.status.value,
        "started_at": v["started_at"],
        "execution": v["execution"],
    }


@router.get("/{remediation_id}/verification", summary="Verification checks and outcome")
async def get_verification(remediation_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    r = await _get(container, principal, remediation_id)
    return {
        "remediation_id": r.remediation_id,
        "status": r.status.value,
        "rules": r.verification_rules,
        "verification": r.verification,
        "result": r.result,
    }


# ----------------------------------------------------------------------------- requests
class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(max_length=64)
    action_type: str = Field(max_length=64, pattern=r"^[A-Z_]{3,64}$")
    parameters: dict[str, str] = Field(default_factory=dict, max_length=8)
    diagnosis_id: str | None = Field(default=None, max_length=64)
    mode: Mode = Mode.IMMEDIATE
    scheduled_at: datetime | None = None
    dry_run: bool = False


@router.post("", status_code=201, summary="Request a catalog action for a device (goes through approval)")
async def request_remediation(body: RequestIn, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    svc = _svc(container)
    _need(principal, "remediation.execute" if body.dry_run else "remediation.request")
    try:
        check_device(principal, container, body.device_id)
    except HTTPException as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown device") from exc
    diagnosis = None
    if body.diagnosis_id and container.diagnosis is not None:
        diagnosis = await container.diagnosis.detail(body.diagnosis_id)
        if diagnosis is None or diagnosis.get("device_id") != body.device_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown diagnosis")
    try:
        r = await svc.propose(
            body.device_id,
            body.action_type,
            body.parameters,
            principal.subject,
            role=principal.role,
            diagnosis=diagnosis,
            mode=body.mode,
            scheduled_at=body.scheduled_at,
            dry_run=body.dry_run,
        )
    except RemediationError as exc:
        raise _http(exc) from exc
    return _view(r, svc, principal)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=300)


@router.post(
    "/{remediation_id}/approve", summary="Approve Action (permission, risk ceiling and four-eyes enforced)"
)
async def approve(
    remediation_id: str, container: ContainerDep, principal: Reader, body: DecisionIn | None = None
) -> dict[str, Any]:
    r = await _get(container, principal, remediation_id)
    svc = _svc(container)
    try:
        r = await svc.approve(
            r,
            principal.subject,
            principal.role,
            body.note if body else None,
            permissions=principal.permissions,
            org_role="platform" if principal.platform_admin else principal.org_role,
        )
    except RemediationError as exc:
        raise _http(exc) from exc
    return _view(r, svc, principal)


@router.post("/{remediation_id}/reject", summary="Reject Action")
async def reject(
    remediation_id: str, container: ContainerDep, principal: Reader, body: DecisionIn | None = None
) -> dict[str, Any]:
    r = await _get(container, principal, remediation_id)
    _need(principal, "remediation.approve")
    svc = _svc(container)
    try:
        r = await svc.reject(r, principal.subject, principal.role, body.note if body else None)
    except RemediationError as exc:
        raise _http(exc) from exc
    return _view(r, svc, principal)


@router.post("/{remediation_id}/cancel", summary="Cancel Execution (only before the device has received it)")
async def cancel(
    remediation_id: str, container: ContainerDep, principal: Reader, body: DecisionIn | None = None
) -> dict[str, Any]:
    r = await _get(container, principal, remediation_id)
    _need(principal, "remediation.cancel")
    svc = _svc(container)
    try:
        r = await svc.cancel(r, principal.subject, body.note if body else None)
    except RemediationError as exc:
        raise _http(exc) from exc
    return _view(r, svc, principal)


@router.post("/{remediation_id}/dry-run", summary="Dry run: what would happen now (changes nothing)")
async def dry_run(remediation_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    r = await _get(container, principal, remediation_id)
    return _svc(container).dry_run_plan(r)


# ------------------------------------------------------------------------------ catalog
@catalog_router.get(
    "", summary="Every action that remediation can perform (and the disabled ones, with reasons)"
)
async def list_catalog(container: ContainerDep, principal: Reader) -> dict[str, Any]:
    _need(principal, "remediation.view")
    svc = _svc(container)
    return {
        "actions": [a.public() for a in CATALOG.values()],
        "applications": [a.public() for a in svc.policy.applications().values()],
    }


@catalog_router.get("/{action_id}", summary="One catalog action")
async def get_catalog(action_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    _need(principal, "remediation.view")
    a = CATALOG.get(action_id[:64])
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown action")
    return a.public()


# ------------------------------------------------------------------------------- policy
@policy_router.get("", summary="Remediation policy (approval modes, four-eyes, kill switches, windows)")
async def get_policy(container: ContainerDep, principal: Reader) -> dict[str, Any]:
    _need(principal, "remediation.view")
    return _svc(container).policy.public()


@policy_router.put("", summary="Change the remediation policy (administrators; validated, versioned)")
async def put_policy(changes: dict[str, Any], container: ContainerDep, principal: Reader) -> dict[str, Any]:
    _need(principal, "remediation.manage_policy")
    require_platform_scope(principal)  # the Phase 8 policy is platform-wide; organisations use /org/policies
    if {"extra_applications", "disabled_applications"} & set(changes):
        _need(principal, "remediation.manage_actions")
    changes = {k: v for k, v in changes.items() if k not in ("version", "updated_at", "updated_by")}
    try:
        return (await _svc(container).set_policy(changes, principal.subject)).public()
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)[:300]) from exc


class KillSwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["global", "tenant", "device_group", "action", "device"]
    target: str | None = Field(default=None, max_length=128)
    enabled: bool


@policy_router.post("/kill-switch", summary="Turn a kill switch on or off (no new execution may begin)")
async def kill_switch(body: KillSwitchIn, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    _need(principal, "remediation.manage_policy")
    # an organisation may switch only its own tenant kill switch; everything else is platform-wide
    if not (body.scope == "tenant" and body.target == principal.org_id):
        require_platform_scope(principal)
    svc = _svc(container)
    ks = svc.policy.public()["kill_switches"]
    if body.scope == "global":
        ks["global"] = body.enabled
    else:
        if not body.target:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "target required for this scope")
        key = {
            "tenant": "tenants",
            "device_group": "device_groups",
            "action": "actions",
            "device": "devices",
        }[body.scope]
        items = set(ks[key])
        (items.add if body.enabled else items.discard)(body.target)
        ks[key] = sorted(items)
    policy = await svc.set_policy({"kill_switches": ks}, principal.subject)
    result: dict[str, Any] = policy.public()["kill_switches"]
    return result


# -------------------------------------------------------------------------------- admin
@admin_router.get("/status", summary="Remediation engine status: signing, kill switches, circuits, queue")
async def remediation_status(container: ContainerDep, principal: Reader) -> dict[str, Any]:
    _need(principal, "remediation.view")
    if principal.role == "employee":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not available for employee accounts")
    out = _svc(container).status()
    allowed = visible_devices(principal, container)  # never another organisation's devices or counts
    out["open_circuits"] = [c for c in out["open_circuits"] if c["device_id"] in allowed]
    svc = _svc(container)
    by_status: dict[str, int] = {}
    for r in svc.items.values():
        if r.device_id in allowed:
            by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
    out["by_status"] = by_status
    out["in_flight"] = sum(by_status.get(k, 0) for k in ("VALIDATING", "EXECUTING", "VERIFYING"))
    out.pop("stats", None)
    return out


@admin_router.get("/audit/verify", summary="Verify the tamper-evident audit chain")
async def verify_audit(container: ContainerDep, principal: Reader) -> dict[str, Any]:
    _need(principal, "remediation.manage_policy")
    require_platform_scope(principal)  # the remediation chain spans every organisation
    return await _svc(container).repo.verify_audit()


# -------------------------------------------------------------------------------- agent
def _device_agent(agent: AgentIdentity, device_id: str) -> None:
    if agent.device_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "A per-device token is required for remediation")
    if agent.device_id != device_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token is not valid for this device")


@agent_router.get("/key", summary="Public key that signs action envelopes (the agent pins it)")
async def agent_key(container: ContainerDep, agent: AgentDep) -> dict[str, Any]:
    if agent.device_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "A per-device token is required for remediation")
    svc = _svc(container)
    if svc.signer is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Remediation signing is not configured")
    return {"public_key": svc.signer.public_key_b64, "key_id": svc.signer.key_id, "algorithm": "Ed25519"}


@agent_router.get("", summary="Signed action envelopes waiting for this device")
async def agent_pull(
    container: ContainerDep, agent: AgentDep, device_id: str = Query(max_length=64)
) -> dict[str, Any]:
    _device_agent(agent, device_id)
    return {"items": _svc(container).agent_pull(device_id, datetime.now(UTC))}


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: Literal["accepted", "rejected", "completed", "failed"]
    detail: str | None = Field(default=None, max_length=500)
    data: dict[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=16)


@agent_router.post("/{execution_id}/report", summary="The agent reports validation, execution and results")
async def agent_report(
    execution_id: str, body: ReportIn, container: ContainerDep, agent: AgentDep
) -> dict[str, Any]:
    if agent.device_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "A per-device token is required for remediation")
    try:
        r = await _svc(container).agent_report(
            agent.device_id, execution_id[:64], body.phase, {"detail": body.detail, "data": body.data}
        )
    except RemediationError as exc:
        raise _http(exc) from exc
    return {"execution_id": r.execution_id, "status": r.status.value}
