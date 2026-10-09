"""Opt-in outbound sync to another Laptop Digital Twin backend ("hub").

Local-first: nothing leaves this machine unless an administrator sets a target URL, enables sync and
provides ``SYNC_TARGET_KEY`` (the hub's agent ingest key) in the environment. The hub receives the same
validated batches the local agent sends; process lists are stripped unless explicitly included.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.schemas.ingest import InventoryEnvelopeIn, TelemetryBatchIn
from app.services.admin import AdminService

log = structlog.get_logger("sync")


class SyncService:
    def __init__(self, admin: AdminService, key: str, max_queue: int = 600) -> None:
        self._admin = admin
        self._key = key
        self._queue: deque[TelemetryBatchIn] = deque(maxlen=max_queue)
        self._inventory: dict[str, InventoryEnvelopeIn] = {}
        self._pending_inventory: set[str] = set()
        self._tokens: dict[str, str] = {}  # per-device tokens issued by the hub (memory only)
        self._config: dict[str, Any] = {"enabled": False}
        self.sent_batches = 0
        self.dropped_batches = 0
        self.last_success_at: datetime | None = None
        self.last_attempt_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def active(self) -> bool:
        return bool(self._config.get("enabled") and self._config.get("target_url") and self._key)

    def offer_batch(self, batch: TelemetryBatchIn) -> None:
        if not self.active:
            return
        if len(self._queue) == self._queue.maxlen:
            self.dropped_batches += 1
        self._queue.append(batch)

    def offer_inventory(self, envelope: InventoryEnvelopeIn) -> None:
        self._inventory[envelope.device_id] = envelope
        self._pending_inventory.add(envelope.device_id)

    def status(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "queued_batches": len(self._queue),
            "sent_batches": self.sent_batches,
            "dropped_batches": self.dropped_batches,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "last_error": self.last_error,
        }

    async def refresh_config(self) -> None:
        was = self.active
        self._config = await self._admin.sync_config()
        if self.active and not was:
            self._pending_inventory = set(self._inventory)  # (re)send identity when sync starts
        if not self.active:
            self._queue.clear()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        last_config = 0.0
        loop = asyncio.get_running_loop()
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            while not stop.is_set():
                if loop.time() - last_config > 10.0:
                    last_config = loop.time()
                    try:
                        await self.refresh_config()
                    except Exception as exc:  # repository unavailable: keep last config
                        log.debug("sync_config_refresh_failed", error=str(exc)[:200])
                delay = 1.0
                if self.active and (self._queue or self._pending_inventory):
                    try:
                        await self._push(client)
                        backoff = 1.0
                    except Exception as exc:
                        self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                        log.warning("sync_failed", error=self.last_error)
                        backoff = min(60.0, backoff * 2)
                        delay = backoff
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)

    async def _push(self, client: httpx.AsyncClient) -> None:
        base = str(self._config["target_url"])
        self.last_attempt_at = datetime.now(UTC)
        for device_id in list(self._pending_inventory):
            env = self._inventory.get(device_id)
            if env is not None:
                resp = await client.post(
                    f"{base}/api/v1/ingest/inventory",
                    content=env.model_dump_json(),
                    headers=await self._headers(client, base, device_id),
                )
                self._check_auth(resp, device_id)
                _raise(resp)
            self._pending_inventory.discard(device_id)
        include_processes = bool(self._config.get("include_processes"))
        for _ in range(50):
            if not self._queue:
                break
            batch = self._queue[0]
            payload = batch if include_processes else batch.model_copy(update={"processes": None})
            resp = await client.post(
                f"{base}/api/v1/ingest/telemetry",
                content=payload.model_dump_json(),
                headers=await self._headers(client, base, batch.device_id),
            )
            self._check_auth(resp, batch.device_id)
            if resp.status_code == 409:  # hub does not know the device yet
                self._pending_inventory.add(batch.device_id)
                return
            _raise(resp)
            self._queue.popleft()
            self.sent_batches += 1
        self.last_success_at = datetime.now(UTC)
        self.last_error = None

    async def _headers(self, client: httpx.AsyncClient, base: str, device_id: str) -> dict[str, str]:
        """The hub only accepts telemetry with a per-device token: register each forwarded device once
        with SYNC_TARGET_KEY (the hub's enrollment key) and keep the token in memory."""
        common = {"Content-Type": "application/json", "User-Agent": "ldt-sync/1.1"}
        token = self._tokens.get(device_id)
        if token is None:
            resp = await client.post(
                f"{base}/api/v1/agent/register",
                json={"device_id": device_id, "agent_version": "hub-sync"},
                headers={**common, "X-Agent-Key": self._key},
            )
            if resp.status_code == 404:  # older hub without registration: fall back to the shared key
                return {**common, "X-Agent-Key": self._key}
            _raise(resp)
            token = self._tokens[device_id] = str(resp.json()["device_token"])
        return {**common, "Authorization": f"Bearer {token}", "X-Device-Id": device_id}

    def _check_auth(self, resp: httpx.Response, device_id: str) -> None:
        if resp.status_code == 401:
            self._tokens.pop(device_id, None)  # rotated or revoked on the hub: re-register next cycle


def _raise(resp: httpx.Response) -> None:
    if resp.status_code in (401, 403):
        raise PermissionError(f"Hub rejected SYNC_TARGET_KEY ({resp.status_code})")
    if resp.status_code >= 400:
        raise RuntimeError(f"Hub returned {resp.status_code}: {resp.text[:160]}")
