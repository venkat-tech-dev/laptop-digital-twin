"""Device-level authorization (enforced server-side on every device-scoped API and WebSocket topic).

Phase 9: a principal sees only devices of its current organisation, narrowed by its device-group scope and,
for employees, by device assignment. ``visible_devices`` always returns an explicit set (never "all"), so a
route that forgets to filter fails closed. A device the caller may not see is answered exactly like an
unknown device (404), and a probe for another organisation's device is recorded as a security event.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Query, status

from app.api.deps import ContainerDep, Reader
from app.core.security import Principal
from app.services.tenancy import TenantContext

NOT_FOUND = "Unknown device"


def context_of(principal: Principal) -> TenantContext:
    return TenantContext(
        principal.org_id,
        principal.subject,
        principal.org_role,
        principal.permissions,
        principal.group_scope,
        principal.platform_admin,
    )


def visible_devices(principal: Principal, container: ContainerDep) -> set[str]:
    """The devices this principal may access in its current organisation (explicit set)."""
    return container.tenancy.visible(context_of(principal))


def check_device(principal: Principal, container: ContainerDep, device_id: str) -> None:
    if device_id not in visible_devices(principal, container):
        owner = container.tenancy.org_of(device_id)
        if owner is not None and owner != principal.org_id:
            container.tenancy.note_cross_tenant(principal.subject, "device", device_id[:64], principal.org_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)


async def scoped_device(
    principal: Reader,
    container: ContainerDep,
    device_id: Annotated[str | None, Query(max_length=64)] = None,
) -> str | None:
    """Resolve the ``device_id`` query parameter for the caller. Without a parameter: the platform's primary
    device if visible, else the first visible device; with one: only a visible device."""
    allowed = visible_devices(principal, container)
    if device_id is None:
        if not allowed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No device is available to your account")
        primary = container.twin.primary_device_id
        return primary if primary in allowed else sorted(allowed)[0]
    check_device(principal, container, device_id)
    return device_id


ScopedDevice = Annotated[str | None, Depends(scoped_device)]


def require_staff(principal: Reader) -> Principal:
    """Viewer or above: fleet-wide and configuration views are not available to employees."""
    if not principal.has_role("viewer"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not available for employee accounts")
    return principal


Staff = Annotated[Principal, Depends(require_staff)]
