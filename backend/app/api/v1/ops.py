"""Operations endpoints (Phase 10): SLOs and their current SLI values. Platform-wide (platform scope)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import Admin, ContainerDep, require_platform_scope
from app.services import slo

router = APIRouter(prefix="/ops", tags=["operations"])


@router.get("/slo", summary="Service-level objectives with current SLI values (since process start)")
async def slos(principal: Admin, container: ContainerDep) -> dict[str, Any]:
    require_platform_scope(principal)
    return slo.evaluate(container)
