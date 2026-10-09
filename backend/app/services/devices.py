"""Agent device registration, per-device tokens and ingest de-duplication.

* ``register``: an agent presenting the enrollment key (``AGENT_INGEST_KEY``) receives a random
  per-device token. Only its SHA-256 hash is stored. Registering again rotates the token.
* ``verify``: constant-time comparison against the stored hash; revoked devices are refused.
  Verified hashes are cached in memory for ``cache_ttl_s`` so the ingest path needs no database
  round trip; while the database is unreachable a cached credential keeps working (stale-while-error)
  and an uncached one raises :class:`CredentialStoreUnavailableError` (-> 503, the agent retries).
  Revocation clears the cache on this replica immediately (other replicas: within the TTL).
* ``BatchDeduper``: remembers recently accepted ``batch_id`` values so a batch retried after a lost
  response is acknowledged as ``duplicate`` instead of being applied twice.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from app.repositories.admin import AdminRepository, DeviceCredential

LAST_USED_WRITE_INTERVAL_S = 300.0


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class CredentialStoreUnavailableError(Exception):
    """The credential store (database) cannot be reached and the token is not cached."""


class DeviceAuthService:
    def __init__(self, repo: AdminRepository, cache_ttl_s: float = 300.0) -> None:
        self._repo = repo
        self._last_write: dict[str, float] = {}
        self._cache: dict[str, tuple[str, float]] = {}  # device_id -> (token hash, verified at)
        self._ttl = cache_ttl_s
        self._expiry: dict[str, datetime | None] = {}

    async def register(self, device_id: str, ttl_days: int = 90) -> str:
        """Issue (or rotate) the device's credential; the previous token stops working immediately."""
        token, _ = await self.issue(device_id, ttl_days)
        return token

    async def issue(self, device_id: str, ttl_days: int = 90) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        digest = _hash(token)
        now = datetime.now(UTC)
        expires = now + timedelta(days=ttl_days)
        try:
            await self._repo.save_device_credential(
                DeviceCredential(device_id, digest, now, expires_at=expires)
            )
        except Exception as exc:
            raise CredentialStoreUnavailableError(str(exc)[:200]) from exc
        self._cache[device_id] = (digest, time.monotonic())
        self._expiry[device_id] = expires
        return token, expires

    def expires_at(self, device_id: str) -> datetime | None:
        return self._expiry.get(device_id)

    async def verify(self, device_id: str, token: str) -> bool:
        digest = _hash(token)
        now = time.monotonic()
        cached = self._cache.get(device_id)
        if cached is not None and now - cached[1] < self._ttl and hmac.compare_digest(cached[0], digest):
            exp = self._expiry.get(device_id)
            if exp is None or exp > datetime.now(UTC):
                return True
        try:
            cred = await self._repo.get_device_credential(device_id)
        except Exception as exc:
            if cached is not None and hmac.compare_digest(cached[0], digest):
                return True  # database down: last verified credential still valid (stale-while-error)
            raise CredentialStoreUnavailableError(str(exc)[:200]) from exc
        expired = cred is not None and cred.expires_at is not None and cred.expires_at <= datetime.now(UTC)
        if cred is None or cred.revoked or expired or not hmac.compare_digest(cred.token_hash, digest):
            self._cache.pop(device_id, None)
            return False
        self._expiry[device_id] = cred.expires_at
        self._cache[device_id] = (digest, now)
        if now - self._last_write.get(device_id, 0.0) > LAST_USED_WRITE_INTERVAL_S:
            self._last_write[device_id] = now
            cred.last_used_at = datetime.now(UTC)
            with contextlib.suppress(Exception):  # bookkeeping only: never fail the request on it
                await self._repo.save_device_credential(cred)
        return True

    async def revoke(self, device_id: str) -> bool:
        self._cache.pop(device_id, None)
        cred = await self._repo.get_device_credential(device_id)
        if cred is None:
            return False
        cred.revoked = True
        await self._repo.save_device_credential(cred)
        return True

    async def list(self) -> list[DeviceCredential]:
        return await self._repo.list_device_credentials()


class BatchDeduper:
    def __init__(self, capacity: int = 200_000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._capacity = capacity

    def seen(self, batch_id: str | None) -> bool:
        return batch_id is not None and batch_id in self._seen

    def remember(self, batch_id: str | None) -> None:
        if batch_id is None:
            return
        self._seen[batch_id] = None
        self._seen.move_to_end(batch_id)
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)

    def forget(self, batch_id: str) -> None:
        """Phase 10: a batch whose rows were lost before being written must be accepted again on resend."""
        self._seen.pop(batch_id, None)
