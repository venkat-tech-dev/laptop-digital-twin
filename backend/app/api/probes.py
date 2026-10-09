from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.deps import ContainerDep
from app.core.metrics import REGISTRY
from app.schemas.misc import ReadinessOut

router = APIRouter(tags=["observability"])


@router.get("/health/live", summary="Liveness: the process is running")
async def live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready", response_model=ReadinessOut, summary="Readiness: dependencies reachable")
async def ready(container: ContainerDep, response: Response) -> ReadinessOut:
    checks: dict[str, dict[str, Any]] = {}
    ok = True
    checks["instance"] = {"role": container.role}
    if container.role != "active":  # standby: another instance holds the leader lock
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessOut(status=container.role, checks=checks)
    if container.db is not None:
        try:
            checks["database"] = {
                "status": "ok",
                "latency_ms": round(await container.db.ping_bounded(2.0), 2),
                "timescaledb": container.db.timescale,
            }
        except Exception as exc:
            ok = False
            checks["database"] = {"status": "error", "error": type(exc).__name__}
    else:
        checks["database"] = {
            "status": "disabled",
            "note": "DATABASE_URL not set: history kept in memory only",
        }
    if container.redis is not None:
        try:
            checks["redis"] = {"status": "ok", "latency_ms": round(await container.redis.ping(), 2)}
        except Exception as exc:
            # Redis is an accelerator, not a hard dependency: degrade, don't fail readiness.
            checks["redis"] = {"status": "degraded", "error": type(exc).__name__}
    else:
        checks["redis"] = {"status": "disabled"}
    p = container.persister
    checks["persistence_queue"] = {
        "status": "ok" if p.depth < 0.8 * p.capacity else "backlogged",
        "depth": p.depth,
        "capacity": p.capacity,
        "oldest_queued_age_s": p.oldest_age_s(),
        "last_error": p.last_error,
    }
    # Phase 10: supervised background loops; a crash-looping critical loop (persister, receipts, audit,
    # alerting, remediation, liveness) means accepted data or governance work is not being processed
    background = container.supervisor.status()
    checks["background"] = background
    if background["status"] == "failing":
        ok = False
    twin = container.twin.get()
    checks["telemetry_agent"] = {"status": twin.device.status.value if twin else "NO_DEVICE"}
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessOut(status="ready" if ok else "not_ready", checks=checks)


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
