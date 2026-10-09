"""``/ws/twin`` - near-real-time digital twin stream.

Protocol (JSON text frames):
  server -> client: connection_status, twin_snapshot, subscribed, unsubscribed, subscription_error,
                    telemetry_update, component_state_changed, health_changed, anomaly_detected,
                    anomaly_resolved, device_status_changed, device_presence_changed, system_event,
                    heartbeat, pong
  Phase 3 (digital twin): twin.snapshot, twin.state.patch, twin.status.changed, twin.event.created,
                         twin.summary (fleet), twin.sync.required
  client -> server:
    {"type": "twin.sync", "device_id": "<id>"}    twin.snapshot after a version gap / reconnect
    {"type": "subscribe",   "topics": ["device:<id>", "workspace:<id>", "fleet"]}
    {"type": "unsubscribe", "topics": [...]}
    {"type": "resync", "device_id": "<id>"?}     fresh snapshot(s) after a gap
    {"type": "ping", "latency": {"websocket_delivery_ms": [..], "end_to_end_latency_ms": [..]}}

Lifecycle: connect (token + origin checked) -> connection_status -> subscribe -> one twin_snapshot
per subscribed device -> live deltas. A client that never subscribes gets the primary device
(backwards compatible). After a reconnect the client re-subscribes and receives fresh snapshots.

Auth: ``?token=<JWT or API key>`` when AUTH_MODE != none (browsers cannot set WS headers).
Origin is checked against CORS_ORIGINS to block cross-site WebSocket hijacking.
Subscription authorization: the topic must exist and be visible to the caller. Employees only see
the devices assigned to them (also on ``fleet`` and ``workspace:`` topics); unauthorized devices are
rejected exactly like unknown ones. Staff roles (viewer and above) see every device.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.core.container import Container
from app.core.security import AuthError
from app.infrastructure.websocket.manager import Client
from app.infrastructure.websocket.protocol import PROTOCOL_VERSION, envelope

router = APIRouter()
MAX_MESSAGE_BYTES = 16_384
MAX_LATENCY_VALUES = 64


def _origin_allowed(container: Container, origin: str | None) -> bool:
    if origin is None:
        return True  # non-browser clients (CLI tools, tests)
    allowed = container.settings.cors_origins
    return "*" in allowed or origin in allowed


@router.websocket("/ws/twin")
async def twin_stream(websocket: WebSocket) -> None:
    container: Container = websocket.app.state.container
    if not _origin_allowed(container, websocket.headers.get("origin")):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Origin not allowed")
        return
    try:
        principal = container.auth.authenticate_token(websocket.query_params.get("token"))
    except AuthError:
        container.note_auth_failure(None, "websocket", "invalid token")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized")
        return
    from fastapi import HTTPException

    from app.api.access import visible_devices
    from app.api.deps import resolve_principal

    try:  # session, membership, organisation (?org=) - same rules as the REST API
        principal = await resolve_principal(
            container, principal, websocket.query_params.get("org"), "/ws/twin"
        )
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized")
        return
    limit, _ = container.tenancy.orgs[principal.org_id].quota("websocket_connections")
    if container.ws.org_connections(principal.org_id) >= limit:
        from app.core.metrics import QUOTA_REJECTIONS

        QUOTA_REJECTIONS.labels("websocket_connections").inc()
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER, reason="Connection quota reached")
        return
    await websocket.accept()
    client = await container.ws.register(websocket, principal.subject)
    client.org_id = principal.org_id
    client.principal = principal  # type: ignore[attr-defined]
    client.allowed = visible_devices(principal, container)  # tenant + scope + assignment (explicit set)
    if principal.role == "employee":  # device-level authorization for the whole connection
        client.devices = set(client.allowed)
    client.primary = _primary_for(container, client)
    container.ws.send(
        client,
        envelope(
            "connection_status",
            status="connected",
            client_id=client.client_id,
            protocol=PROTOCOL_VERSION,
            heartbeat_interval_s=container.settings.ws_heartbeat_s,
            auth=principal.method,
            server_time=datetime.now(UTC).isoformat(),
            primary_device_id=_primary_for(container, client),
            organization_id=client.org_id,
            topics_supported=["device:<id>", "workspace:<id>", "fleet"],
        ),
    )
    first = _primary_for(container, client)
    if first is not None:
        _send_snapshot(container, client, first)  # legacy clients: their organisation's primary device
    try:
        while True:
            raw = await websocket.receive_text()
            client.last_received = time.monotonic()
            if len(raw) > MAX_MESSAGE_BYTES:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind == "ping":
                _record_client_latency(container, msg.get("latency"))
                container.ws.send(client, envelope("pong", server_time=datetime.now(UTC).isoformat()))
            elif kind == "subscribe":
                await _subscribe(container, client, msg.get("topics"))
            elif kind == "unsubscribe":
                await _unsubscribe(container, client, msg.get("topics"))
            elif kind == "twin.sync":
                device_id = msg.get("device_id")
                if isinstance(device_id, str) and _may_see(client, device_id):
                    _send_twin_snapshot(container, client, device_id)
            elif kind == "resync":
                device_id = msg.get("device_id")
                if (
                    isinstance(device_id, str)
                    and _may_see(client, device_id)
                    and (client.devices is None or device_id in client.devices)
                ):
                    _send_snapshot(container, client, device_id)
                elif client.devices:
                    for d in sorted(client.devices):
                        _send_snapshot(container, client, d)
                else:
                    _send_snapshot(container, client, None)
    except WebSocketDisconnect:
        pass
    finally:
        await container.ws.unregister(client)


def _may_see(client: Client, device_id: str) -> bool:
    return client.allowed is not None and device_id in client.allowed


def _primary_for(container: Container, client: Client) -> str | None:
    """The platform's primary device if this client may see it, else its first visible device."""
    allowed = client.allowed or set()
    primary = container.twin.primary_device_id
    return primary if primary in allowed else (sorted(allowed)[0] if allowed else None)


async def _resolve(container: Container, client: Client, topic: str) -> tuple[set[str] | None, str | None]:
    """Devices behind a topic, or (None, reason) when it may not be subscribed."""
    kind, _, ident = topic.partition(":")
    if topic == "fleet":
        return set(), None
    if kind == "device" and ident:
        if container.twin.has_device(ident) and _may_see(client, ident):
            return {ident}, None
        return None, "unknown_device"  # unauthorized devices look exactly like unknown ones
    if kind == "workspace" and ident:
        known = [d.device_id for d in container.twin.devices()]
        try:
            spaces = await container.admin.workspaces(known)
        except Exception:
            return None, "workspaces_unavailable"
        for w in spaces:
            if w.workspace_id == ident:
                visible = {d for d in w.device_ids if _may_see(client, d)}
                return (visible, None) if visible else (None, "unknown_workspace")
        return None, "unknown_workspace"
    return None, "invalid_topic"


async def _subscribe(container: Container, client: Client, topics: Any) -> None:
    if not isinstance(topics, list):
        container.ws.send(client, envelope("subscription_error", reason="topics must be a list"))
        return
    principal = getattr(client, "principal", None)
    if principal is not None:  # refresh: devices enrolled / moved since the connection opened
        from app.api.access import visible_devices

        client.allowed = visible_devices(principal, container)
    accepted: list[str] = []
    rejected: list[dict[str, str]] = []
    new_devices: set[str] = set()
    limit = container.settings.ws_max_subscriptions
    for topic in topics[:limit]:
        if not isinstance(topic, str) or len(topic) > 128:
            rejected.append({"topic": str(topic)[:128], "reason": "invalid_topic"})
            continue
        if topic not in client.topics and len(client.topics) >= limit:
            rejected.append({"topic": topic, "reason": "too_many_subscriptions"})
            continue
        devices, reason = await _resolve(container, client, topic)
        if devices is None:
            rejected.append({"topic": topic, "reason": reason or "forbidden"})
            continue
        before = set(client.devices or ())
        container.ws.subscribe(client, topic, devices)
        new_devices |= devices - before
        accepted.append(topic)
    container.ws.send(
        client,
        envelope(
            "subscribed",
            topics=sorted(client.topics),
            accepted=accepted,
            rejected=rejected,
            devices=sorted(client.devices or ()),
        ),
    )
    for device_id in sorted(new_devices):
        _send_snapshot(container, client, device_id)
        _send_twin_snapshot(container, client, device_id)
    if "fleet" in accepted:
        container.ws.send(client, envelope("fleet_snapshot", devices=_fleet(container, client)))


async def _unsubscribe(container: Container, client: Client, topics: Any) -> None:
    if not isinstance(topics, list):
        return
    resolved: dict[str, set[str]] = {}
    for t in client.topics:
        devices, _ = await _resolve(container, client, t)
        resolved[t] = devices or set()
    for topic in topics:
        if isinstance(topic, str):
            container.ws.unsubscribe(client, topic, lambda t: resolved.get(t, set()))
    container.ws.send(client, envelope("unsubscribed", topics=sorted(client.topics)))


def _fleet(container: Container, client: Client) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    out = []
    for d in container.twin.devices():
        if not _may_see(client, d.device_id):
            continue
        p = container.presence.get(d.device_id)
        out.append(
            {
                "device_id": d.device_id,
                "model": d.model,
                "status": d.status.value,
                "presence": p.presence.value if p else "UNKNOWN",
                "last_contact_at": p.to_dict(now)["last_contact_at"] if p else None,
            }
        )
    return out


def _record_client_latency(container: Container, latency: Any) -> None:
    """Browsers report what they measured (delivery and end-to-end), so the backend can expose the
    whole path. Values are clamped and capped: this input is untrusted."""
    if not isinstance(latency, dict):
        return
    for stage in ("websocket_delivery_ms", "end_to_end_latency_ms"):
        values = latency.get(stage)
        if not isinstance(values, list):
            continue
        for v in values[:MAX_LATENCY_VALUES]:
            if isinstance(v, (int, float)) and 0 <= v <= 600_000:
                container.ingest.latency.observe(stage, float(v))


def _send_twin_snapshot(container: Container, client: Client, device_id: str) -> None:
    """Digital twin document (initial / recovery synchronisation; patches follow)."""
    snap = container.twin_state.engine.snapshot(device_id, flat=True)  # same keys as the patches
    container.ws.send(client, envelope("twin.snapshot", device_id=device_id, twin=snap))


def _send_snapshot(container: Container, client: Client, device_id: str | None) -> None:
    snapshot = container.twin.snapshot(device_id)
    if snapshot is not None:
        snapshot["presence"] = container.presence.presence_of(snapshot["device_id"])
    container.ws.send(
        client,
        envelope(
            "twin_snapshot",
            device_id=snapshot["device_id"] if snapshot else device_id,
            twin=snapshot,
        ),
    )
