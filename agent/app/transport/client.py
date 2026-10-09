"""Secure API client for the backend.

* HTTPS only. Plain HTTP is accepted solely for a loopback backend (local development /
  single-machine install) and only while ``AGENT_ALLOW_INSECURE_LOCALHOST`` is true.
* TLS certificates are always validated (system store, or ``AGENT_CA_BUNDLE`` for corporate PKI).
* Device registration: the enrollment key is exchanged once for a per-device token
  (``POST /api/v1/agent/register``). The token is stored with DPAPI and sent as a Bearer token; a 401
  triggers one re-registration (token refresh/rotation). Backends without registration support fall
  back to the enrollment key header.
* Organization enrollment (Phase 9): with ``AGENT_ENROLLMENT_TOKEN`` the device enrolls once through
  ``POST /api/v1/agent/enroll`` (single-use, expiring token that also selects the organization) instead
  of the shared key. Credentials expire; the heartbeat reports ``credential_expires_at`` and the client
  rotates the token (``POST /api/v1/agent/credentials/rotate``) before it lapses.
* Errors are typed (:class:`ApiError`) with a ``retryable`` flag the sync manager relies on; a 429
  or 503 ``Retry-After`` is honoured.
* Bulk uploads are gzip-compressed (measured on a real batch: 63 KB JSON -> 6.3 KB). A backend that
  answers 415 gets uncompressed bodies from then on.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import ipaddress
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

from app.contracts import SCHEMA_VERSION, InventoryEnvelope
from app.security.credentials import CredentialStore, DeviceCredential

log = structlog.get_logger("agent.api")

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
GZIP_MIN_BYTES = 1024
MAX_RETRY_AFTER_S = 3600.0


class ConfigurationError(Exception):
    pass


class ApiError(Exception):
    def __init__(
        self,
        detail: str,
        status: int | None = None,
        retryable: bool = True,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.retryable = retryable
        self.retry_after_s = retry_after_s


@dataclass(frozen=True, slots=True)
class BatchResult:
    batch_id: str
    status: str  # accepted | duplicate | rejected
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class BulkSummary:
    accepted: int
    duplicates: int
    rejected: int
    last_sequence: int | None
    durable_confirmation: bool = False  # Phase 10 backend: keep accepted batches until confirmed durable


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, min(MAX_RETRY_AFTER_S, float(value)))
    except ValueError:
        return None  # HTTP-date form: fall back to our own backoff


def validate_backend_url(url: str, allow_insecure_localhost: bool) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ConfigurationError(f"AGENT_BACKEND_URL must be an https:// URL (got {url!r})")
    if parsed.scheme == "http":
        host = parsed.hostname
        loopback = host == "localhost"
        with contextlib.suppress(ValueError):
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        if not (loopback and allow_insecure_localhost):
            raise ConfigurationError("Plain HTTP is only allowed for a loopback backend; use https://")
    return url.rstrip("/")


class AgentApiClient:
    def __init__(
        self,
        base_url: str,
        enrollment_key: str,
        credentials: CredentialStore | None,
        *,
        agent_version: str,
        timeout_s: float = 10.0,
        ca_bundle: str | None = None,
        allow_insecure_localhost: bool = True,
        use_device_tokens: bool = True,
        enrollment_token: str = "",
        rotate_before: timedelta = timedelta(days=7),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = validate_backend_url(base_url, allow_insecure_localhost)
        if not enrollment_key and not enrollment_token:
            raise ConfigurationError("Neither AGENT_ENROLLMENT_TOKEN nor AGENT_INGEST_KEY is set")
        if enrollment_token and credentials is None:
            raise ConfigurationError("AGENT_ENROLLMENT_TOKEN requires a credential store")
        self._key = enrollment_key
        self._enrollment_token = enrollment_token
        self._rotate_before = rotate_before
        self.credential_expires_at: datetime | None = None
        self._creds = credentials
        self._version = agent_version
        self._use_tokens = (use_device_tokens or bool(enrollment_token)) and credentials is not None
        self._device_id: str | None = None
        self._token: str | None = None
        self._legacy = not self._use_tokens
        self.last_latency_ms: float | None = None
        self.last_summary: BulkSummary | None = None
        self.bytes_sent_total = 0
        self.bytes_uncompressed_total = 0
        self._gzip = True
        self._register_lock = asyncio.Lock()
        self._verify: str | bool = ca_bundle or True
        self._transport = transport
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)),
            verify=self._verify,
            headers={"User-Agent": f"ldt-agent/{agent_version}"},
            transport=transport,
        )

    # ------------------------------------------------------------------ auth
    @property
    def auth_mode(self) -> str:
        return "device-token" if self._token else "enrollment-key"

    def bind_device(self, device_id: str) -> None:
        self._device_id = device_id
        if self._use_tokens and self._creds is not None:
            cred = self._creds.load(device_id, self.base_url)
            self._token = cred.token if cred else None

    def _auth_headers(self) -> dict[str, str]:
        if self._token and self._device_id:
            return {"Authorization": f"Bearer {self._token}", "X-Device-Id": self._device_id}
        return {"X-Agent-Key": self._key} if self._key else {}

    async def ensure_registered(self) -> None:
        if self._legacy or self._token or self._device_id is None:
            return
        async with self._register_lock:  # concurrent first requests must register exactly once
            if self._legacy or self._token:
                return
            await self._register()

    async def _register(self) -> None:
        assert self._device_id is not None
        if self._enrollment_token:
            await self._enroll()
            return
        try:
            resp = await self._client.post(
                "/api/v1/agent/register",
                json={"device_id": self._device_id, "agent_version": self._version},
                headers={"X-Agent-Key": self._key},
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"Registration failed: {type(exc).__name__}", retryable=True) from exc
        if resp.status_code == 404:
            log.info("backend_without_device_registration_using_enrollment_key")
            self._legacy = True
            return
        self._raise_for(resp, "Registration")
        self._adopt(resp.json())
        log.info("device_registered", device_id=self._device_id)

    async def _enroll(self) -> None:
        """Exchange the single-use organization enrollment token for a per-device credential."""
        assert self._device_id is not None
        try:
            resp = await self._client.post(
                "/api/v1/agent/enroll",
                json={
                    "enrollment_token": self._enrollment_token,
                    "device_id": self._device_id,
                    "agent_version": self._version,
                },
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"Enrollment failed: {type(exc).__name__}", retryable=True) from exc
        if resp.status_code in (401, 403, 409):
            # used / expired / revoked token, device owned by another organization, quota: an
            # administrator must issue a new token. Data stays queued (retryable) meanwhile.
            code = _error_code(resp)
            log.error("enrollment_rejected", status=resp.status_code, code=code)
            raise ApiError(
                f"Enrollment rejected ({resp.status_code} {code}); issue a new AGENT_ENROLLMENT_TOKEN",
                resp.status_code,
                retryable=True,
                retry_after_s=300.0,
            )
        self._raise_for(resp, "Enrollment")
        data = resp.json()
        self._adopt(data)
        self._enrollment_token = ""  # single-use: never sent again by this process
        log.info("device_enrolled", device_id=self._device_id, organization=data.get("organization_id"))

    def _adopt(self, data: dict[str, Any]) -> None:
        assert self._device_id is not None
        token = str(data["device_token"])
        self._token = token
        self.credential_expires_at = _parse_time(data.get("credential_expires_at"))
        if self._creds is not None:
            try:
                self._creds.save(DeviceCredential(self._device_id, token, self.base_url))
            except Exception as exc:  # cannot persist: keep the token in memory for this run
                log.warning("credential_save_failed", error=str(exc)[:200])

    async def maybe_rotate(self, heartbeat: dict[str, Any], now: datetime | None = None) -> bool:
        """Rotate the device credential when the backend reports it expires within ``rotate_before``."""
        expires = _parse_time(heartbeat.get("credential_expires_at"))
        if expires is not None:
            self.credential_expires_at = expires
        if expires is None or not self._token or self._legacy:
            return False
        if expires - (now or datetime.now(UTC)) > self._rotate_before:
            return False
        async with self._register_lock:
            current = self._token
            try:
                resp = await self._client.post(
                    "/api/v1/agent/credentials/rotate", headers=self._auth_headers()
                )
            except httpx.HTTPError as exc:
                log.debug("credential_rotation_failed", error=type(exc).__name__)
                return False
            if resp.status_code != 200 or self._token != current:
                log.warning("credential_rotation_refused", status=resp.status_code, code=_error_code(resp))
                return False
            self._adopt(resp.json())
        log.info("device_credential_rotated", expires_at=str(self.credential_expires_at))
        return True

    # ------------------------------------------------------------------ requests
    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        await self.ensure_registered()
        for attempt in (1, 2):
            started = time.perf_counter()
            headers = {**self._auth_headers(), **kwargs.pop("extra_headers", {})}
            if "content" in kwargs:
                headers["Content-Type"] = "application/json"
                self.bytes_sent_total += len(kwargs["content"])
            try:
                resp = await self._client.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise ApiError(f"{type(exc).__name__}: {exc}"[:300], retryable=True) from exc
            self.last_latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
            if resp.status_code == 401 and self._token and attempt == 1:
                rejected = headers.get("Authorization")
                async with self._register_lock:
                    # Another request may already have rotated the token: only re-register once.
                    if self._token and f"Bearer {self._token}" == rejected:
                        log.warning("device_token_rejected_reregistering")
                        self._token = None
                        if self._creds is not None:
                            self._creds.clear()
                        await self._register()
                kwargs["extra_headers"] = {k: v for k, v in headers.items() if k == "Content-Encoding"}
                continue
            return resp
        return resp

    @staticmethod
    def _raise_for(resp: httpx.Response, what: str) -> None:
        if resp.status_code < 400:
            return
        retryable = resp.status_code in RETRYABLE_STATUS
        if resp.status_code in (401, 403):
            detail = f"{what}: backend rejected credentials ({resp.status_code}); check AGENT_INGEST_KEY"
            raise ApiError(detail, resp.status_code, retryable=True)  # config fix expected; keep data
        raise ApiError(
            f"{what}: HTTP {resp.status_code} {resp.text[:200]}",
            resp.status_code,
            retryable,
            retry_after_s=_retry_after(resp),
        )

    async def publish_inventory(self, envelope: InventoryEnvelope) -> None:
        resp = await self._send("POST", "/api/v1/ingest/inventory", content=envelope.model_dump_json())
        self._raise_for(resp, "Inventory")

    async def publish_batches(self, payloads: list[bytes]) -> list[BatchResult]:
        """Upload pre-serialised batches in one gzip request (one-by-one on very old backends)."""
        body = b'{"batches":[' + b",".join(payloads) + b"]}"
        self.bytes_uncompressed_total += len(body)
        resp = await self._post_compressed("/api/v1/ingest/telemetry/bulk", body)
        if resp.status_code == 404:
            return [await self._publish_single(p) for p in payloads]
        if resp.status_code == 409:
            raise ApiError("Unknown device: inventory required", 409, retryable=True)
        self._raise_for(resp, "Bulk telemetry")
        data = resp.json()
        if "accepted" in data:
            self.last_summary = BulkSummary(
                int(data.get("accepted", 0)),
                int(data.get("duplicates", 0)),
                int(data.get("rejected", 0)),
                data.get("last_sequence"),
                bool(data.get("durable_confirmation", False)),
            )
        return [BatchResult(r["batch_id"], r["status"], r.get("detail")) for r in data["results"]]

    async def _post_compressed(self, path: str, body: bytes) -> httpx.Response:
        if self._gzip and len(body) >= GZIP_MIN_BYTES:
            packed = gzip.compress(body, compresslevel=6)
            resp = await self._send("POST", path, content=packed, extra_headers={"Content-Encoding": "gzip"})
            if resp.status_code != 415:
                return resp
            log.info("backend_rejects_gzip_sending_uncompressed")
            self._gzip = False
        return await self._send("POST", path, content=body)

    async def heartbeat(self, info: dict[str, Any]) -> dict[str, Any]:
        """Liveness ping: keeps presence ONLINE even while uploads back off; returns server time."""
        body = json.dumps(
            {"schema_version": SCHEMA_VERSION, "device_id": self._device_id, **info}, default=str
        )
        resp = await self._send("POST", "/api/v1/agent/heartbeat", content=body.encode())
        if resp.status_code == 404:
            return {}  # older backend: presence then follows telemetry only
        self._raise_for(resp, "Heartbeat")
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def _publish_single(self, payload: bytes) -> BatchResult:
        batch_id = str(json.loads(payload).get("batch_id", ""))
        resp = await self._send("POST", "/api/v1/ingest/telemetry", content=payload)
        if resp.status_code == 409:
            raise ApiError("Unknown device: inventory required", 409, retryable=True)
        if resp.status_code == 422:
            return BatchResult(batch_id, "rejected", resp.text[:200])
        self._raise_for(resp, "Telemetry")
        return BatchResult(batch_id, "accepted")

    async def fetch_config(self) -> dict[str, Any] | None:
        try:
            resp = await self._send("GET", "/api/v1/agent/config", params={"device_id": self._device_id})
        except ApiError:
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) and data.get("configured") else None

    async def fetch_notifications(self) -> list[dict[str, Any]]:
        """Windows notifications the backend queued for this device (Phase 6, device token only)."""
        try:
            resp = await self._send(
                "GET", "/api/v1/agent/notifications", params={"device_id": self._device_id}
            )
        except ApiError:
            return []
        if resp.status_code != 200:
            return []
        items = resp.json().get("items") if isinstance(resp.json(), dict) else None
        return [i for i in items or [] if isinstance(i, dict) and i.get("notification_id")][:10]

    async def ack_notification(self, notification_id: str) -> None:
        try:
            await self._send("POST", f"/api/v1/agent/notifications/{notification_id}/ack")
        except ApiError:
            return

    # ------------------------------------------------------------------ Phase 8: remediation
    async def fetch_action_key(self) -> dict[str, Any] | None:
        try:
            resp = await self._send("GET", "/api/v1/agent/actions/key")
        except ApiError:
            return None
        return resp.json() if resp.status_code == 200 and isinstance(resp.json(), dict) else None

    async def fetch_actions(self) -> list[dict[str, Any]]:
        try:
            resp = await self._send("GET", "/api/v1/agent/actions", params={"device_id": self._device_id})
        except ApiError:
            return []
        if resp.status_code != 200:
            return []
        items = resp.json().get("items") if isinstance(resp.json(), dict) else None
        return [i for i in items or [] if isinstance(i, dict)][:5]

    async def report_action(self, execution_id: str, phase: str, detail: str, data: dict[str, Any]) -> bool:
        body = {
            "phase": phase,
            "detail": detail[:500],
            "data": {k: v for k, v in data.items() if isinstance(v, (str, int, float, bool)) or v is None},
        }
        try:
            resp = await self._send("POST", f"/api/v1/agent/actions/{execution_id}/report", json=body)
        except ApiError:
            return False
        return resp.status_code < 500

    async def reset_connections(self) -> None:
        """RECONNECT_AGENT: drop pooled connections; the next request opens new ones."""
        old = self._client
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=old.timeout,
            verify=self._verify,
            headers={"User-Agent": f"ldt-agent/{self._version}"},
            transport=self._transport,
        )
        await old.aclose()

    async def close(self) -> None:
        await self._client.aclose()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _error_code(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return "UNKNOWN"
    code = body.get("code") if isinstance(body, dict) else None
    return str(code)[:40] if code else "UNKNOWN"
