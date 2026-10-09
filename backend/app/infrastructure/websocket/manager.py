"""WebSocket connection manager with topic subscriptions.

Each client gets a bounded send queue drained by its own task, so one slow browser tab can never
block telemetry fan-out: if its queue overflows the connection is closed (the client reconnects,
re-subscribes and resynchronises from a fresh snapshot).

Routing (messages carry ``device_id``):
  * ``device:<id>``     everything about that device (telemetry, state, anomalies, presence...)
  * ``workspace:<id>``  expanded to the workspace's devices at subscribe time
  * ``fleet``           low-volume events for every device (presence, status, anomalies, health);
                        high-volume ``telemetry_update`` / ``component_state_changed`` are excluded
  * no subscription     legacy behaviour: the primary device only
Messages without ``device_id`` are delivered only when they carry no tenant data (``GLOBAL_EVENTS``);
the heartbeat is sent per client with that client's organisation-scoped primary device (Phase 9).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from starlette.websockets import WebSocket, WebSocketState

from app.core.metrics import WS_CONNECTIONS, WS_DROPPED, WS_MESSAGES, WS_SUBSCRIPTIONS

log = structlog.get_logger("websocket")

HIGH_VOLUME_EVENTS = frozenset({"telemetry_update", "component_state_changed", "twin.state.patch"})
#: device-less messages that may go to every client (they contain no organisation data)
GLOBAL_EVENTS = frozenset({"server_notice"})


@dataclass(eq=False)
class Client:
    websocket: WebSocket
    queue: asyncio.Queue[tuple[float, str]]
    client_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    last_received: float = field(default_factory=time.monotonic)
    sender: asyncio.Task[None] | None = None
    closed: bool = False
    subject: str = "anonymous"
    topics: set[str] = field(default_factory=set)
    devices: set[str] | None = None  # None: legacy client (no subscription) -> primary device only
    fleet: bool = False
    allowed: set[str] | None = None  # authorization: the devices this client may see (Phase 9: always a set)
    org_id: str | None = None  # organisation of the connection (tenant-aware routing and quotas)
    primary: str | None = None  # this client's organisation-scoped primary device (legacy clients)

    def wants(
        self, device_id: str | None, event: str, primary: str | None, recipient: str | None = None
    ) -> bool:
        if recipient is not None:  # a personal message (notification.*): only that user's sessions
            return recipient == self.subject and (
                not device_id or self.allowed is None or device_id in self.allowed
            )
        if device_id is None:
            return event in GLOBAL_EVENTS  # fail closed: device-less messages carry no tenant data
        if self.allowed is not None and device_id not in self.allowed:
            return False
        if self.devices is None:
            return device_id == (self.primary or primary) or (self.fleet and event not in HIGH_VOLUME_EVENTS)
        if device_id in self.devices:
            return True
        return self.fleet and event not in HIGH_VOLUME_EVENTS


class ConnectionManager:
    def __init__(
        self,
        send_queue_max: int = 256,
        primary: Callable[[], str | None] | None = None,
        on_sent: Callable[[float], None] | None = None,
    ) -> None:
        self._clients: set[Client] = set()
        self._queue_max = send_queue_max
        self._primary = primary or (lambda: None)
        self._on_sent = on_sent
        self.sent_total = 0
        self.dropped_clients_total = 0

    @property
    def count(self) -> int:
        return len(self._clients)

    def set_hooks(
        self, primary: Callable[[], str | None] | None = None, on_sent: Callable[[float], None] | None = None
    ) -> None:
        if primary is not None:
            self._primary = primary
        if on_sent is not None:
            self._on_sent = on_sent

    async def register(self, websocket: WebSocket, subject: str = "anonymous") -> Client:
        client = Client(websocket, asyncio.Queue(maxsize=self._queue_max), subject=subject)
        client.sender = asyncio.create_task(self._sender(client), name=f"ws-sender-{client.client_id}")
        self._clients.add(client)
        WS_CONNECTIONS.set(len(self._clients))
        log.info("ws_connected", client_id=client.client_id, clients=len(self._clients))
        return client

    async def unregister(self, client: Client, reason: str = "disconnect") -> None:
        if client.closed:
            return
        client.closed = True
        self._clients.discard(client)
        WS_CONNECTIONS.set(len(self._clients))
        self._update_subscription_gauge()
        if client.sender is not None:
            client.sender.cancel()
        if client.websocket.application_state is WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await client.websocket.close(code=1001 if reason == "shutdown" else 1000)
        log.info("ws_disconnected", client_id=client.client_id, reason=reason, clients=len(self._clients))

    # ----------------------------------------------------------- subscriptions
    def subscribe(self, client: Client, topic: str, devices: set[str]) -> None:
        client.topics.add(topic)
        if topic == "fleet":
            client.fleet = True
        else:
            client.devices = (client.devices or set()) | devices
        self._update_subscription_gauge()

    def unsubscribe(self, client: Client, topic: str, resolve: Callable[[str], set[str]]) -> None:
        client.topics.discard(topic)
        client.fleet = "fleet" in client.topics
        remaining: set[str] = set()
        for t in client.topics:
            if t != "fleet":
                remaining |= resolve(t)
        client.devices = remaining if client.topics - {"fleet"} else (set() if client.fleet else None)
        self._update_subscription_gauge()

    def _update_subscription_gauge(self) -> None:
        WS_SUBSCRIPTIONS.set(sum(len(c.topics) for c in self._clients))

    def interested_devices(self) -> set[str]:
        """Devices whose high-volume messages some local client actually receives."""
        primary = self._primary()
        out: set[str] = set()
        for c in self._clients:
            if c.devices is None:
                if primary and (c.allowed is None or primary in c.allowed):
                    out.add(primary)
            else:
                out |= c.devices
        return out

    def wants_device(self, device_id: str) -> bool:
        primary = self._primary()
        for c in self._clients:
            if c.allowed is not None and device_id not in c.allowed:
                continue
            if (c.devices is None and device_id == (c.primary or primary)) or (
                c.devices is not None and device_id in c.devices
            ):
                return True
        return False

    def subscription_stats(self) -> dict[str, Any]:
        topics: dict[str, int] = {}
        legacy = 0
        for c in self._clients:
            if not c.topics:
                legacy += 1
            for t in c.topics:
                kind = t.split(":", 1)[0]
                topics[kind] = topics.get(kind, 0) + 1
        return {
            "clients": len(self._clients),
            "legacy_clients": legacy,
            "subscriptions_by_kind": topics,
            "queued_messages": sum(c.queue.qsize() for c in self._clients),
            "sent_total": self.sent_total,
            "slow_consumers_dropped_total": self.dropped_clients_total,
        }

    # ------------------------------------------------------------------- send
    def sessions_of(self, subject: str) -> int:
        """Open sessions of one user (browser-notification delivery needs at least one)."""
        return sum(1 for c in self._clients if c.subject == subject and not c.closed)

    def clients(self) -> list[Client]:
        return list(self._clients)

    def org_connections(self, org_id: str) -> int:
        return sum(1 for c in self._clients if c.org_id == org_id)

    def send(self, client: Client, message: dict[str, Any]) -> bool:
        return self._enqueue(client, json.dumps(message, default=str))

    def broadcast(self, message: dict[str, Any], data: str | None = None) -> None:
        """Route ``message`` to the clients whose subscriptions match it (serialised at most once;
        pass ``data`` when the caller already has the JSON text)."""
        if not self._clients:
            return
        device_id = message.get("device_id")
        event = str(message.get("event", ""))
        recipient = message.get("recipient")
        primary = self._primary()
        for client in list(self._clients):
            if not client.wants(device_id, event, primary, recipient):
                continue
            if data is None:
                data = json.dumps(message, default=str)
            self._enqueue(client, data)

    def _enqueue(self, client: Client, data: str) -> bool:
        try:
            client.queue.put_nowait((time.perf_counter(), data))
            WS_MESSAGES.inc()
            return True
        except asyncio.QueueFull:
            WS_DROPPED.inc()
            self.dropped_clients_total += 1
            log.warning("ws_slow_consumer_dropped", client_id=client.client_id)
            asyncio.get_running_loop().create_task(self.unregister(client, "slow_consumer"))
            return False

    async def _sender(self, client: Client) -> None:
        try:
            while True:
                queued_at, data = await client.queue.get()
                await client.websocket.send_text(data)
                self.sent_total += 1
                if self._on_sent is not None:
                    self._on_sent((time.perf_counter() - queued_at) * 1000)
        except asyncio.CancelledError:
            raise
        except Exception:  # socket closed underneath us
            await self.unregister(client, "send_failed")

    async def close_idle(self, idle_timeout_s: float) -> None:
        now = time.monotonic()
        for client in list(self._clients):
            if now - client.last_received > idle_timeout_s:
                await self.unregister(client, "idle_timeout")

    async def close_all(self) -> None:
        for client in list(self._clients):
            await self.unregister(client, "shutdown")
