"""Accounts, workspaces, operator settings, sync, diagnostics and model-asset endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.api.access import ScopedDevice, Staff, visible_devices
from app.api.deps import Admin, ContainerDep, require_agent
from app.core.security import AuthError
from app.domain.admin.models import Role
from app.domain.tenancy.permissions import FROM_LEGACY
from app.services.admin import ConflictError
from app.services.diagnostics import build_bundle

accounts_router = APIRouter(prefix="/auth", tags=["auth"])
users_router = APIRouter(prefix="/users", tags=["users"])
workspaces_router = APIRouter(prefix="/workspaces", tags=["workspaces"])
settings_router = APIRouter(prefix="/settings", tags=["settings"])
agent_router = APIRouter(prefix="/agent", tags=["ingest (agent only)"], dependencies=[Depends(require_agent)])
assets_router = APIRouter(prefix="/models", tags=["3D model assets"])
diagnostics_router = APIRouter(prefix="/system", tags=["system"])


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthError):
        return HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(422, str(exc))


def _accounts_only(container: Any) -> None:
    if container.settings.auth_mode.value != "accounts":
        raise HTTPException(status.HTTP_409_CONFLICT, "User accounts are used only when AUTH_MODE=accounts")


# ----------------------------------------------------------------------------------- accounts
class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SetupRequest(Credentials):
    setup_token: str | None = Field(default=None, max_length=256)


# ----------------------------------------------------------------------------------- users
class NewUser(Credentials):
    role: Literal["admin", "operator", "viewer", "employee"] = "viewer"


class UserPatch(BaseModel):
    role: Literal["admin", "operator", "viewer", "employee"] | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, max_length=256)


@users_router.get("")
async def list_users(_: Admin, container: ContainerDep) -> list[dict[str, Any]]:
    return [u.public() for u in await container.admin.users()]


@users_router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(body: NewUser, principal: Admin, container: ContainerDep) -> dict[str, Any]:
    _accounts_only(container)
    try:
        user = await container.admin.create_user(body.username, body.password, Role(body.role))
    except (ConflictError, ValueError) as exc:
        raise _http(exc) from exc
    # Phase 9: legacy user management acts in the default organisation
    await container.tenancy.set_member(None, "default", user.username, FROM_LEGACY[body.role])
    container.audit.record(
        principal.org_id,
        principal.subject,
        "user",
        "user.created",
        "user",
        resource_type="user",
        resource_id=user.username,
        metadata={"role": FROM_LEGACY[body.role]},
    )
    return user.public()


@users_router.patch("/{user_id}")
async def update_user(
    user_id: str, body: UserPatch, principal: Admin, container: ContainerDep
) -> dict[str, Any]:
    try:
        user = await container.admin.update_user(
            user_id, body.model_dump(exclude_none=True), principal.subject
        )
    except (ConflictError, LookupError, ValueError) as exc:
        raise _http(exc) from exc
    member = container.tenancy.member("default", user.username)
    if body.role is not None or body.disabled is not None:
        await container.tenancy.set_member(
            None,
            "default",
            user.username,
            FROM_LEGACY[user.role.value]
            if body.role is not None
            else (member.role if member else FROM_LEGACY[user.role.value]),
            member.group_scope if member else [],
            "DISABLED" if user.disabled else "ACTIVE",
        )
    if user.disabled or body.password is not None:
        await container.identity.revoke_user(
            user.username, "disabled" if user.disabled else "password changed"
        )
    container.audit.record(
        principal.org_id,
        principal.subject,
        "user",
        "user.updated",
        "user",
        resource_type="user",
        resource_id=user.username,
        metadata={
            "role": body.role,
            "disabled": body.disabled,
            "password_changed": body.password is not None,
        },
    )
    return user.public()


@users_router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: str, principal: Admin, container: ContainerDep) -> Response:
    users = {u.user_id: u for u in await container.admin.users()}
    try:
        await container.admin.delete_user(user_id, principal.subject)
    except (ConflictError, LookupError) as exc:
        raise _http(exc) from exc
    gone = users.get(user_id)
    if gone is not None:
        await container.identity.revoke_user(gone.username, "account deleted")
        for m in list(container.tenancy.members.get(gone.username, {}).values()):
            await container.tenancy.set_member(
                None, m.org_id, gone.username, m.role, m.group_scope, "DISABLED"
            )
        container.audit.record(
            principal.org_id,
            principal.subject,
            "user",
            "user.deleted",
            "user",
            resource_type="user",
            resource_id=gone.username,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------------------- workspaces
class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=96)
    device_ids: list[str] | None = Field(default=None, max_length=500)


def _known_devices(container: Any) -> list[str]:
    return [d.device_id for d in container.twin.devices()]


def _workspace_view(ws: Any, container: Any) -> dict[str, Any]:
    devices = {d.device_id: d for d in container.twin.devices()}
    out: dict[str, Any] = ws.public()
    out["devices"] = [
        {
            "device_id": did,
            "name": " ".join(x for x in (devices[did].manufacturer, devices[did].model) if x)
            if did in devices
            else did,
            "status": devices[did].status.value if did in devices else "UNKNOWN",
        }
        for did in ws.device_ids
    ]
    return out


async def _refresh_departments(container: Any) -> None:
    """Departments (workspaces) feed the fleet overview and the twin identity."""
    container.assignments.set_departments(await container.admin.workspaces(_known_devices(container)))
    for device_id in _known_devices(container):
        await container.twin_state.on_applied(device_id)


@workspaces_router.get("")
async def list_workspaces(principal: Staff, container: ContainerDep) -> list[dict[str, Any]]:
    spaces = await container.admin.workspaces(_known_devices(container))
    container.assignments.set_departments(spaces)
    if principal.org_id != "default" and not principal.platform_admin:
        return []  # workspaces are the default organisation's departments; others use /org/units and groups
    visible = visible_devices(principal, container)
    out = []
    for w in spaces:
        view = _workspace_view(w, container)
        view["device_ids"] = [d for d in view.get("device_ids", []) if d in visible]
        view["devices"] = [d for d in view.get("devices", []) if d["device_id"] in visible]
        out.append(view)
    return out


@workspaces_router.post("", status_code=status.HTTP_201_CREATED)
async def create_workspace(body: WorkspaceIn, _: Admin, container: ContainerDep) -> dict[str, Any]:
    await container.admin.workspaces(_known_devices(container))
    try:
        ws = await container.admin.save_workspace(None, body.name, body.device_ids)
    except (ConflictError, LookupError, ValueError) as exc:
        raise _http(exc) from exc
    await _refresh_departments(container)
    return _workspace_view(ws, container)


@workspaces_router.patch("/{workspace_id}")
async def update_workspace(
    workspace_id: str, body: WorkspaceIn, _: Admin, container: ContainerDep
) -> dict[str, Any]:
    try:
        ws = await container.admin.save_workspace(workspace_id, body.name, body.device_ids)
    except (ConflictError, LookupError, ValueError) as exc:
        raise _http(exc) from exc
    await _refresh_departments(container)
    return _workspace_view(ws, container)


@workspaces_router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(workspace_id: str, _: Admin, container: ContainerDep) -> Response:
    try:
        await container.admin.delete_workspace(workspace_id)
    except (ConflictError, LookupError) as exc:
        raise _http(exc) from exc
    await _refresh_departments(container)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------------------- agent configuration
class AgentConfigIn(BaseModel):
    telemetry_interval_ms: int | None = Field(default=None, ge=250, le=60_000)
    process_interval_ms: int | None = Field(default=None, ge=1000, le=60_000)
    top_process_count: int | None = Field(default=None, ge=1, le=100)
    collect_process_details: bool | None = None


@agent_router.get("/config", summary="Operator configuration for the agent (polled by the agent)")
async def agent_config_for_agent(container: ContainerDep, device_id: str | None = None) -> dict[str, Any]:
    return await container.admin.agent_config()


def _applied(container: Any) -> dict[str, Any]:
    """What the agent reports it is actually running with (from its own telemetry)."""
    twin = container.twin.get()
    agent = twin.components.get("agent") if twin else None
    out: dict[str, Any] = {}
    if agent is not None:
        for key, reading in agent.telemetry.items():
            if (
                key
                in (
                    "agent.telemetry_interval_ms",
                    "agent.process_interval_ms",
                    "agent.collect_process_details",
                    "agent.config_version",
                )
                and reading.available
            ):
                out[key.removeprefix("agent.")] = reading.value
    return out


@settings_router.get("/agent", summary="Agent configuration (requested and applied)")
async def get_agent_settings(_: Staff, container: ContainerDep) -> dict[str, Any]:
    return {"requested": await container.admin.agent_config(), "applied": _applied(container)}


@settings_router.put("/agent", summary="Change agent configuration (applied by the agent within ~15 s)")
async def put_agent_settings(
    body: AgentConfigIn, principal: Admin, container: ContainerDep
) -> dict[str, Any]:
    try:
        requested = await container.admin.set_agent_config(
            body.model_dump(exclude_none=True), principal.subject
        )
    except ValueError as exc:
        raise _http(exc) from exc
    return {"requested": requested, "applied": _applied(container)}


# ----------------------------------------------------------------------------------- sync
class SyncIn(BaseModel):
    enabled: bool | None = None
    target_url: str | None = Field(default=None, max_length=512)
    include_processes: bool | None = None


@settings_router.get("/sync", summary="Outbound sync to another backend (off by default)")
async def get_sync(_: Staff, container: ContainerDep) -> dict[str, Any]:
    return {**(await container.admin.sync_config()), **container.sync.status()}


@settings_router.put("/sync")
async def put_sync(body: SyncIn, principal: Admin, container: ContainerDep) -> dict[str, Any]:
    try:
        await container.admin.set_sync_config(body.model_dump(exclude_unset=True), principal.subject)
        await container.sync.refresh_config()
    except ValueError as exc:
        raise _http(exc) from exc
    return {**(await container.admin.sync_config()), **container.sync.status()}


# ----------------------------------------------------------------------------------- diagnostics
@diagnostics_router.get("/diagnostics", summary="Download a diagnostics bundle (ZIP, secrets redacted)")
async def diagnostics(_: Staff, container: ContainerDep, anonymize: bool = Query(default=True)) -> Response:
    data, filename = await build_bundle(container, anonymize)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


# ----------------------------------------------------------------------------------- model assets
_MAX = {"mesh": 60 * 1024 * 1024, "photo": 12 * 1024 * 1024}


def _sniff(kind: str, data: bytes) -> str:
    if kind == "mesh":
        if data[:4] == b"glTF":
            return "glb"
        raise ValueError("Upload a binary glTF (.glb) file")
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise ValueError("Upload a JPEG, PNG or WebP image")


@assets_router.put("/{kind}", summary="Upload a 3D model (.glb) or photo of the detected laptop (raw body)")
async def upload_asset(
    kind: Literal["mesh", "photo"],
    request: Request,
    _: Admin,
    container: ContainerDep,
    exact: bool = False,
    attribution: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    twin = container.twin.get()
    if twin is None or not twin.device.manufacturer or not twin.device.model:
        raise HTTPException(status.HTTP_409_CONFLICT, "No identified device to attach the file to")
    declared = int(request.headers.get("content-length") or 0)
    if declared > _MAX[kind]:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File exceeds {_MAX[kind] // 2**20} MB"
        )
    data = await request.body()
    if not data or len(data) > _MAX[kind]:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Empty or oversized upload")
    try:
        ext = _sniff(kind, data)
        container.geometry.save_asset(
            twin.device.manufacturer, twin.device.model, kind, data, ext, exact=exact, attribution=attribution
        )
    except ValueError as exc:
        raise _http(exc) from exc
    except OSError as exc:
        raise HTTPException(
            status.HTTP_507_INSUFFICIENT_STORAGE, f"Model directory not writable: {exc}"
        ) from exc
    d = twin.device
    return container.geometry.resolve(d.manufacturer, d.model, d.inventory)


@assets_router.delete("/{kind}", summary="Remove the uploaded model or photo")
async def delete_asset(kind: Literal["mesh", "photo"], _: Admin, container: ContainerDep) -> dict[str, Any]:
    twin = container.twin.get()
    if twin is None or not twin.device.manufacturer or not twin.device.model:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No identified device")
    if not container.geometry.remove_asset(twin.device.manufacturer, twin.device.model, kind):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nothing uploaded")
    d = twin.device
    return container.geometry.resolve(d.manufacturer, d.model, d.inventory)


# ----------------------------------------------------------------------------------- endpoint state
endpoint_router = APIRouter(prefix="/endpoint", tags=["endpoint agent"])


@endpoint_router.get("", summary="Device health (posture), agent health and recent device events")
async def endpoint_state(
    device_id: ScopedDevice,
    container: ContainerDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    twin = container.twin.get(device_id)
    if twin is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No device has reported yet")
    events = list(twin.recent_events)[:limit]
    if not events:
        try:
            stored = await container.event_repo.list_system_events(twin.device.device_id, 500)
        except Exception:
            stored = []
        events = [
            {
                "event_id": str(e.data.get("event_id", "")),
                "type": e.event_type.removeprefix("device."),
                "severity": e.severity,
                "timestamp": e.time.isoformat(),
                "source": str(e.data.get("source", "")),
                "message": e.message,
                "data": {k: v for k, v in e.data.items() if k not in ("event_id", "source")},
            }
            for e in stored
            if e.event_type.startswith("device.")
        ][:limit]
    cred = await container.device_auth.list()
    mine = next((c for c in cred if c.device_id == twin.device.device_id), None)
    return {
        "device_id": twin.device.device_id,
        "device_health": twin.device_health,
        "agent_health": twin.agent_health,
        "events": events,
        "presence": (
            p.to_dict(datetime.now(UTC)) if (p := container.presence.get(twin.device.device_id)) else None
        ),
        "sequence": container.ingest.sequences.snapshot(twin.device.device_id).get(twin.device.device_id),
        "credential": None
        if mine is None
        else {
            "registered_at": mine.created_at.isoformat(),
            "last_used_at": mine.last_used_at.isoformat() if mine.last_used_at else None,
            "revoked": mine.revoked,
        },
    }


@endpoint_router.delete(
    "/credentials/{device_id}", summary="Revoke a device token (the agent must re-enroll)"
)
async def revoke_device(device_id: str, _: Admin, container: ContainerDep) -> dict[str, Any]:
    if not await container.device_auth.revoke(device_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device has no registered credential")
    return {"device_id": device_id, "revoked": True}
