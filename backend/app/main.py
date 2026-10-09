"""FastAPI application factory for the Laptop Digital Twin backend."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import probes, ws
from app.api.v1 import (
    admin,
    alerting,
    analysis,
    devices,
    diagnosis,
    fleet_ops,
    identity,
    ingest,
    intelligence,
    ops,
    org,
    predictions,
    remediation,
    scim,
    system,
    telemetry,
    twin,
)
from app.core.config import Settings, get_settings
from app.core.container import VERSION, build_container
from app.core.ingest_body import IngestBodyMiddleware
from app.core.logging import configure_logging, request_id_var
from app.core.middleware import RateLimitMiddleware, RequestContextMiddleware, StandbyGateMiddleware
from app.core.tracing import setup_tracing
from app.infrastructure.database.engine import normalize_url
from app.infrastructure.database.leader import LeaderLock

log = structlog.get_logger("app")
_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    422: "INVALID_REQUEST",
    429: "RATE_LIMITED",
    501: "NOT_IMPLEMENTED",
    503: "UNAVAILABLE",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    container = build_container(settings)

    async def activate() -> None:
        await container.start()
        container.role = "active"
        log.info(
            "backend_started",
            env=settings.app_env.value,
            persistence=container.persistence_mode,
            redis=settings.redis_enabled,
            auth=settings.auth_mode.value,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Phase 10: with a database, exactly one instance is active (advisory lock); others are standby
        shutdown = asyncio.Event()
        lock: LeaderLock | None = None
        background: list[asyncio.Task[None]] = []

        async def step_down() -> None:
            container.role = "lost"
            log.critical("shutting_down_after_leadership_loss")
            os.kill(os.getpid(), signal.SIGTERM)  # graceful stop; the supervisor restarts us as a candidate

        async def lead() -> None:
            assert lock is not None
            if not await lock.wait_until_leader(shutdown):
                return
            await activate()
            await lock.hold(shutdown, step_down)

        if container.db is not None and settings.leader_election:
            lock = LeaderLock(normalize_url(settings.database_url))
            container.role = "standby"
            try:
                if await lock.try_acquire():
                    await activate()
                    background.append(asyncio.create_task(lock.hold(shutdown, step_down), name="leader_hold"))
                else:
                    log.warning("starting_as_standby")
                    background.append(asyncio.create_task(lead(), name="leader_wait"))
            except Exception as exc:  # database unreachable: wait as standby, never risk two leaders
                log.error("leader_election_unavailable_waiting", error=str(exc)[:200])
                background.append(asyncio.create_task(lead(), name="leader_wait"))
        else:
            await activate()
        yield
        shutdown.set()
        for t in background:
            t.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        if container.role in ("active", "lost") and container.started:
            await container.stop()
        if lock is not None:
            await lock.release()
        log.info("backend_stopped", role=container.role)

    app = FastAPI(
        title="Laptop Digital Twin API",
        version=VERSION,
        description="Real-time digital twin of a physical Windows laptop. "
        "LIVE data is real hardware telemetry; "
        "simulation endpoints return clearly labelled generated data.",
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env.value != "production" else None,
        redoc_url=None,
    )
    app.state.container = container

    app.add_middleware(
        IngestBodyMiddleware,
        max_body_bytes=settings.ingest_max_body_bytes,
        max_decompressed_bytes=settings.ingest_max_decompressed_bytes,
    )
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(StandbyGateMiddleware, container=container)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "X-API-Key", "Content-Type", "X-Request-ID", "X-Organization-Id"],
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Phase 9: machine-readable code + message + request id next to the (unchanged) "detail"
        detail = exc.detail
        if isinstance(detail, dict):
            code, message = str(detail.get("code") or exc.status_code), str(detail.get("message") or detail)
        else:
            code, message = _CODES.get(exc.status_code, str(exc.status_code)), str(detail)
        body = {
            "detail": detail,
            "code": code,
            "message": message,
            "request_id": request_id_var.get() or None,
        }
        return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Do not echo submitted values (may contain telemetry); report locations and messages only.
        errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(DBAPIError)
    async def database_unavailable(_: Request, exc: DBAPIError) -> JSONResponse:
        # Connectivity problems are transient: 503 + Retry-After makes clients (agents) retry.
        if exc.connection_invalidated or isinstance(exc, (OperationalError, InterfaceError)):
            log.warning("database_unavailable", error_type=type(exc.orig).__name__)
            return JSONResponse(
                {"detail": "Database temporarily unavailable"}, status_code=503, headers={"Retry-After": "10"}
            )
        log.exception("database_error", error_type=type(exc).__name__)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", error_type=type(exc).__name__)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    api = "/api/v1"
    app.include_router(ingest.router, prefix=api)
    app.include_router(ingest.register_router, prefix=api)
    app.include_router(devices.router, prefix=api)
    app.include_router(devices.pipeline_router, prefix=api)
    app.include_router(devices.fleet_router, prefix=api)
    app.include_router(twin.router, prefix=api)
    app.include_router(telemetry.router, prefix=api)
    app.include_router(analysis.anomalies_router, prefix=api)
    app.include_router(intelligence.device_router, prefix=api)
    app.include_router(intelligence.anomaly_router, prefix=api)
    app.include_router(intelligence.config_router, prefix=api)
    app.include_router(predictions.device_router, prefix=api)
    app.include_router(predictions.prediction_router, prefix=api)
    app.include_router(predictions.accuracy_router, prefix=api)
    app.include_router(predictions.config_router, prefix=api)
    app.include_router(alerting.alert_router, prefix=api)
    app.include_router(alerting.notification_router, prefix=api)
    app.include_router(alerting.prefs_router, prefix=api)
    app.include_router(alerting.admin_router, prefix=api)
    app.include_router(alerting.agent_router, prefix=api)
    app.include_router(diagnosis.device_router, prefix=api)
    app.include_router(diagnosis.diagnosis_router, prefix=api)
    app.include_router(diagnosis.trigger_router, prefix=api)
    app.include_router(diagnosis.job_router, prefix=api)
    app.include_router(diagnosis.config_router, prefix=api)
    app.include_router(remediation.router, prefix=api)
    app.include_router(remediation.catalog_router, prefix=api)
    app.include_router(remediation.policy_router, prefix=api)
    app.include_router(remediation.admin_router, prefix=api)
    app.include_router(remediation.agent_router, prefix=api)
    app.include_router(ops.router, prefix=api)
    app.include_router(fleet_ops.router, prefix=api)
    app.include_router(analysis.analytics_router, prefix=api)
    app.include_router(analysis.simulation_router, prefix=api)
    app.include_router(system.router, prefix=api)
    app.include_router(system.auth_router, prefix=api)
    app.include_router(identity.router, prefix=api)
    app.include_router(org.router, prefix=api)
    app.include_router(org.platform_router, prefix=api)
    app.include_router(scim.router)
    app.include_router(admin.accounts_router, prefix=api)
    app.include_router(admin.users_router, prefix=api)
    app.include_router(admin.workspaces_router, prefix=api)
    app.include_router(admin.settings_router, prefix=api)
    app.include_router(admin.agent_router, prefix=api)
    app.include_router(admin.assets_router, prefix=api)
    app.include_router(admin.diagnostics_router, prefix=api)
    app.include_router(admin.endpoint_router, prefix=api)
    app.include_router(ws.router)
    app.include_router(probes.router)
    if settings.models_dir.is_dir():
        app.mount("/models", StaticFiles(directory=settings.models_dir), name="models")
    setup_tracing(app, settings)
    return app


def run() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=s.api_host,
        port=s.api_port,
        log_level=s.log_level.lower(),
        access_log=False,
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )


if __name__ == "__main__":
    run()
