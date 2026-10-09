"""ASGI middleware for agent endpoints: request size limits and gzip request bodies.

Agents send ``Content-Encoding: gzip`` (measured on a real batch: 63 KB JSON -> 6.3 KB). This layer
reads the body with a hard cap on the *compressed* size (413), inflates it with a hard cap on the
*decompressed* size (413, blocks zip bombs) and hands the route a plain JSON body. Malformed gzip is
a 400. Everything else passes through untouched.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from app.core.metrics import INGEST_BYTES

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

AGENT_PREFIXES = ("/api/v1/ingest/", "/api/v1/agent/")
GATED_PREFIX = "/api/v1/ingest/telemetry"  # concurrency-gated (heartbeats/registration are cheap)


class BodyTooLargeError(Exception):
    pass


def inflate(data: bytes, limit: int) -> bytes:
    """Decompress gzip/zlib ``data``, refusing to produce more than ``limit`` bytes."""
    d = zlib.decompressobj(wbits=47)  # 32 + 15: auto-detect gzip or zlib header
    out = d.decompress(data, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise BodyTooLargeError
    if not d.eof:
        raise zlib.error("truncated compressed body")
    return out


class IngestBodyMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int, max_decompressed_bytes: int) -> None:
        self.app = app
        self.max_body = max_body_bytes
        self.max_inflated = max_decompressed_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(AGENT_PREFIXES):
            await self.app(scope, receive, send)
            return
        pipeline = None
        if str(scope["path"]).startswith(GATED_PREFIX):
            app_state = getattr(scope.get("app"), "state", None)
            pipeline = getattr(getattr(app_state, "container", None), "ingest", None)
        if pipeline is not None:
            overloaded = pipeline.try_enter()
            if overloaded is not None:
                await _error(
                    send, 503, overloaded.reason, retry_after=max(1, round(overloaded.retry_after_s))
                )
                return
            try:
                await self._handle(scope, receive, send)
            finally:
                pipeline.leave()
            return
        await self._handle(scope, receive, send)

    async def _handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        declared = headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_body:
            await _error(send, 413, f"Request body exceeds {self.max_body} bytes")
            return
        encoding = headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("identity", "gzip", "deflate"):
            await _error(send, 415, f"Unsupported Content-Encoding: {encoding}")
            return
        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body:
                await _error(send, 413, f"Request body exceeds {self.max_body} bytes")
                return
            chunks.append(chunk)
            more = bool(message.get("more_body"))
        body = b"".join(chunks)
        route = "bulk" if str(scope["path"]).endswith("/bulk") else "other"
        INGEST_BYTES.labels(route, "wire").inc(len(body))
        if encoding != "identity" and body:
            try:
                body = inflate(body, self.max_inflated)
            except BodyTooLargeError:
                await _error(send, 413, f"Decompressed body exceeds {self.max_inflated} bytes")
                return
            except zlib.error:
                await _error(send, 400, "Malformed compressed body")
                return
        INGEST_BYTES.labels(route, "decoded").inc(len(body))
        new_headers = [
            (k, v)
            for k, v in scope.get("headers", [])
            if k.lower() not in (b"content-encoding", b"content-length")
        ]
        new_headers.append((b"content-length", str(len(body)).encode()))
        scope = {**scope, "headers": new_headers}
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return await receive()  # disconnect notifications after the body
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


async def _error(send: Send, status: int, detail: str, retry_after: int | None = None) -> None:
    payload = json.dumps({"detail": detail}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    if retry_after is not None:
        headers.append((b"retry-after", str(retry_after).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})
