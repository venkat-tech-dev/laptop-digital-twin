"""Redis integration.

* Hot twin state:          ``ldt:twin:{device_id}``          (JSON, TTL) - survives backend restarts
* Event fan-out (pub/sub):  ``ldt:events``                     - ``<origin>|<json>``; each replica
                                                                  delivers its own events locally and
                                                                  only decodes other replicas' events
* Short-term buffer:        ``ldt:recent:{device_id}`` stream  - compact numeric samples for chart backfill

PostgreSQL stays the system of record; Redis data is disposable.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis
import structlog

from app.core.metrics import REDIS_LATENCY

log = structlog.get_logger("redis")
EVENTS_CHANNEL = "ldt:events"


class RedisGateway:
    def __init__(self, url: str, recent_maxlen: int = 2000) -> None:
        self._redis = aioredis.from_url(
            url,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
            health_check_interval=15,
        )
        self._recent_maxlen = recent_maxlen
        self._listener: asyncio.Task[None] | None = None
        self.connected = False
        self.origin = uuid.uuid4().hex[:12]  # this replica
        # Pub/sub channels are global across Redis logical databases: namespace the channel by the
        # db index so deployments sharing one Redis server (e.g. a test instance on db 1) stay apart.
        db = self._redis.connection_pool.connection_kwargs.get("db", 0)
        self.channel = EVENTS_CHANNEL if not db else f"{EVENTS_CHANNEL}:db{db}"

    async def ping(self) -> float:
        started = time.perf_counter()
        await self._redis.ping()
        elapsed = time.perf_counter() - started
        REDIS_LATENCY.labels("ping").observe(elapsed)
        self.connected = True
        return elapsed * 1000.0

    async def save_twin(self, device_id: str, state: dict[str, Any], ttl_s: int = 3600) -> None:
        started = time.perf_counter()
        await self._redis.set(f"ldt:twin:{device_id}", json.dumps(state, default=str), ex=ttl_s)
        REDIS_LATENCY.labels("save_twin").observe(time.perf_counter() - started)

    async def save_twin_doc(self, device_id: str, raw: str, ttl_s: int = 7 * 86400) -> None:
        started = time.perf_counter()
        await self._redis.set(f"ldt:twindoc:{device_id}", raw, ex=ttl_s)
        REDIS_LATENCY.labels("save_twin_doc").observe(time.perf_counter() - started)

    async def load_twin_doc(self, device_id: str) -> str | None:
        raw = await self._redis.get(f"ldt:twindoc:{device_id}")
        return str(raw) if raw else None

    async def set_interest(self, devices: set[str], ttl_s: int = 15) -> None:
        """Advertise which devices this replica's WebSocket clients watch (interest-based fan-out)."""
        await self._redis.set(f"{self.channel}:interest:{self.origin}", json.dumps(sorted(devices)), ex=ttl_s)

    async def remote_interest(self) -> set[str]:
        out: set[str] = set()
        async for key in self._redis.scan_iter(match=f"{self.channel}:interest:*", count=100):
            if str(key).endswith(self.origin):
                continue
            raw = await self._redis.get(key)
            if raw:
                out.update(json.loads(raw))
        return out

    async def load_twin(self, device_id: str) -> dict[str, Any] | None:
        raw = await self._redis.get(f"ldt:twin:{device_id}")
        return json.loads(raw) if raw else None

    async def last_device_id(self) -> str | None:
        value = await self._redis.get("ldt:last_device")
        return str(value) if value else None

    async def set_last_device(self, device_id: str) -> None:
        await self._redis.set("ldt:last_device", device_id)

    async def append_recent(self, device_id: str, ts_ms: int, values: dict[str, float]) -> None:
        started = time.perf_counter()
        await self._redis.xadd(
            f"ldt:recent:{device_id}",
            {"ts": ts_ms, "v": json.dumps(values)},
            maxlen=self._recent_maxlen,
            approximate=True,
        )
        REDIS_LATENCY.labels("append_recent").observe(time.perf_counter() - started)

    async def read_recent(self, device_id: str, since_ms: int) -> list[tuple[int, dict[str, float]]]:
        entries: list[tuple[str, dict[str, str]]] = await self._redis.xrange(
            f"ldt:recent:{device_id}", min=f"{since_ms}-0", max="+"
        )  # type: ignore[assignment]
        out: list[tuple[int, dict[str, float]]] = []
        for _, fields in entries or []:
            out.append((int(fields["ts"]), json.loads(fields["v"])))
        return out

    async def publish_event(self, message: dict[str, Any]) -> None:
        await self.publish_raw(json.dumps(message, default=str))

    async def publish_raw(self, data: str) -> None:
        """Publish an already serialised event, tagged with this replica's origin id."""
        started = time.perf_counter()
        await self._redis.publish(self.channel, f"{self.origin}|{data}")
        REDIS_LATENCY.labels("publish").observe(time.perf_counter() - started)

    def start_listener(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        async def _listen() -> None:
            backoff = 1.0
            while True:
                try:
                    pubsub = self._redis.pubsub()
                    await pubsub.subscribe(self.channel)
                    backoff = 1.0
                    self.connected = True  # subscribed again after an outage: cross-replica publishing on
                    async for msg in pubsub.listen():
                        if msg.get("type") != "message":
                            continue
                        data = str(msg["data"])
                        if not data.startswith("{"):  # "<origin>|<json>"
                            origin, _, data = data.partition("|")
                            if origin == self.origin:
                                continue  # delivered locally already; skip the decode
                        try:
                            await handler(json.loads(data))
                        except Exception as exc:  # one bad message must not tear down the subscription
                            log.warning("redis_message_skipped", error=str(exc)[:200])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # reconnect with backoff
                    self.connected = False
                    log.warning("redis_subscription_lost", error=str(exc), retry_in_s=backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

        self._listener = asyncio.create_task(_listen(), name="redis-listener")

    async def close(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
        await self._redis.aclose()
