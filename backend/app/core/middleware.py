"""HTTP middleware: correlation IDs, latency metrics, rate limiting, security headers."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.logging import request_id_var
from app.core.metrics import HTTP_LATENCY

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Agent endpoints have their own per-device token bucket (see api/v1/ingest.py).
_UNLIMITED = ("/api/v1/ingest/", "/api/v1/agent/", "/health/", "/metrics")


def _templates(routes: Any, prefix: str = "") -> list[str]:
    """Path templates of all routes, including routers included with a prefix (cheap: no schema build)."""
    out: list[str] = []
    for r in routes:
        ctx = getattr(r, "include_context", None)
        inner = getattr(r, "original_router", None)
        if ctx is not None and inner is not None:
            out += _templates(inner.routes, prefix + (getattr(ctx, "prefix", "") or ""))
        elif isinstance(getattr(r, "path", None), str):
            out.append(prefix + r.path)
    return out


def _route_template(request: Request) -> str:
    """Path template for metrics labels (routes of included routers match on a child scope the middleware
    never sees). Templates come from the app's OpenAPI paths, compiled once; unknown paths stay
    ``unmatched`` so label cardinality is bounded."""
    app = request.app
    patterns = getattr(app.state, "_route_patterns", None)
    if patterns is None:
        from starlette.routing import compile_path

        templates = _templates(app.routes)
        if not templates:  # framework internals changed: fall back to the (slower) OpenAPI schema
            try:
                templates = list(app.openapi().get("paths", {}))
            except Exception:
                templates = []
        patterns = [(compile_path(t)[0], t) for t in sorted(templates, key=lambda t: (t.count("{"), -len(t)))]
        app.state._route_patterns = patterns
    path = request.url.path
    for rx, template in patterns:
        if rx.match(path):
            return str(template)
    return "unmatched"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        finally:
            path = _route_template(request)  # full template incl. prefixes: /api/v1/devices/{device_id}
            HTTP_LATENCY.labels(request.method, path, str(status_code)).observe(time.perf_counter() - started)
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        container = getattr(request.app.state, "container", None)
        if container is not None and container.settings.hsts_enabled:  # only behind TLS
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path.startswith(_UNLIMITED) or request.method == "OPTIONS":
            return await call_next(request)
        container = request.app.state.container
        client = request.client.host if request.client else "unknown"
        allowed, retry_after = container.limiter.allow(client)
        if not allowed:
            return JSONResponse(
                {"detail": "Rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(max(1, int(retry_after)))},
            )
        return await call_next(request)


class StandbyGateMiddleware:
    """Phase 10: a standby instance (another one holds the leader lock) answers 503 except for probes.

    Pure ASGI so WebSockets are covered too (closed with 1013 "try again later").
    """

    OPEN_PATHS = ("/health/", "/metrics")

    def __init__(self, app: Any, container: Any) -> None:
        self.app = app
        self.container = container

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope["type"] in ("http", "websocket")
            and self.container.role != "active"
            and not str(scope.get("path", "")).startswith(self.OPEN_PATHS)
        ):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
                return
            msg = "This instance is on standby; another instance is active"
            body = json.dumps(
                {"detail": {"code": "STANDBY", "message": msg}, "code": "STANDBY", "message": msg}
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"application/json"), (b"retry-after", b"5")],
                }
            )
            await send({"type": "http.response.body", "body": body.encode()})
            return
        await self.app(scope, receive, send)
