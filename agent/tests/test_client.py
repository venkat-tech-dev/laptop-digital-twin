"""Secure API client against an in-process fake backend (httpx.MockTransport) + DPAPI credentials."""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import httpx
import pytest

from app.security.credentials import CredentialStore, DeviceCredential
from app.transport.client import AgentApiClient, ApiError, ConfigurationError, validate_backend_url

KEY = "enroll-key"


def test_https_required_except_loopback() -> None:
    assert validate_backend_url("https://twin.example.com/", True) == "https://twin.example.com"
    assert validate_backend_url("http://127.0.0.1:8000", True) == "http://127.0.0.1:8000"
    assert validate_backend_url("http://localhost:8000", True)
    with pytest.raises(ConfigurationError):
        validate_backend_url("http://twin.example.com", True)
    with pytest.raises(ConfigurationError):
        validate_backend_url("http://127.0.0.1:8000", False)
    with pytest.raises(ConfigurationError):
        validate_backend_url("ftp://x", True)


class FakeBackend:
    def __init__(self, *, registration: bool = True, bulk: bool = True, gzip_ok: bool = True) -> None:
        self.registration = registration
        self.bulk = bulk
        self.gzip_ok = gzip_ok
        self.encodings: list[str] = []
        self.throttle = False
        self.heartbeats: list[dict[str, object]] = []
        self.tokens: dict[str, str] = {}
        self.revoked = False
        self.received: list[dict[str, object]] = []
        self.auth_seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/agent/register":
            if not self.registration:
                return httpx.Response(404)
            if request.headers.get("X-Agent-Key") != KEY:
                return httpx.Response(401)
            device = json.loads(request.content)["device_id"]
            token = f"tok-{len(self.tokens) + 1}"
            self.tokens[device] = token
            self.revoked = False
            return httpx.Response(200, json={"device_id": device, "device_token": token})
        auth = request.headers.get("Authorization", "")
        self.auth_seen.append("token" if auth else "key")
        if auth:
            device = request.headers.get("X-Device-Id", "")
            if self.revoked or self.tokens.get(device) != auth.removeprefix("Bearer "):
                return httpx.Response(401)
        elif request.headers.get("X-Agent-Key") != KEY:
            return httpx.Response(401)
        if path == "/api/v1/agent/heartbeat":
            self.heartbeats.append(json.loads(request.content))
            return httpx.Response(200, json={"presence": "ONLINE", "server_time": "2026-10-07T00:00:00Z"})
        if path == "/api/v1/ingest/telemetry/bulk":
            if not self.bulk:
                return httpx.Response(404)
            if self.throttle:
                return httpx.Response(429, headers={"Retry-After": "42"}, json={"detail": "slow down"})
            encoding = request.headers.get("Content-Encoding", "identity")
            self.encodings.append(encoding)
            if encoding == "gzip" and not self.gzip_ok:
                return httpx.Response(415)
            body = gzip.decompress(request.content) if encoding == "gzip" else request.content
            batches = json.loads(body)["batches"]
            self.received += batches
            return httpx.Response(
                200,
                json={
                    "accepted": len(batches),
                    "duplicates": 0,
                    "rejected": 0,
                    "last_sequence": 7,
                    "results": [{"batch_id": b["batch_id"], "status": "accepted"} for b in batches],
                },
            )
        if path == "/api/v1/ingest/telemetry":
            self.received.append(json.loads(request.content))
            return httpx.Response(202, json={"accepted": 1})
        if path == "/api/v1/ingest/inventory":
            return httpx.Response(202, json={"accepted": 1})
        return httpx.Response(500)


class MemoryCreds(CredentialStore):
    def __init__(self) -> None:
        self.saved: DeviceCredential | None = None

    def load(self, device_id: str, backend_url: str) -> DeviceCredential | None:
        return self.saved if self.saved and self.saved.device_id == device_id else None

    def save(self, cred: DeviceCredential) -> None:
        self.saved = cred

    def clear(self) -> None:
        self.saved = None


def client(backend: FakeBackend, creds: CredentialStore | None = None) -> AgentApiClient:
    c = AgentApiClient(
        "http://127.0.0.1:8000",
        KEY,
        creds or MemoryCreds(),
        agent_version="t",
        transport=httpx.MockTransport(backend),
    )
    c.bind_device("dev-1")
    return c


def batch(i: int) -> bytes:
    return json.dumps({"batch_id": f"b{i}", "device_id": "dev-1"}).encode()


async def test_registers_then_uses_device_token() -> None:
    be, creds = FakeBackend(), MemoryCreds()
    c = client(be, creds)
    results = await c.publish_batches([batch(1), batch(2)])
    assert [r.status for r in results] == ["accepted", "accepted"]
    assert creds.saved is not None and creds.saved.token == "tok-1"
    assert be.auth_seen == ["token"] and c.auth_mode == "device-token"
    assert c.last_latency_ms is not None
    await c.close()


async def test_revoked_token_triggers_reregistration_once() -> None:
    be, creds = FakeBackend(), MemoryCreds()
    c = client(be, creds)
    await c.publish_batches([batch(1)])
    be.revoked = True
    await c.publish_batches([batch(2)])  # 401 -> re-register -> retry succeeds
    assert creds.saved is not None and creds.saved.token == "tok-2"
    assert [b["batch_id"] for b in be.received] == ["b1", "b2"]
    await c.close()


async def test_old_backend_without_registration_or_bulk_falls_back() -> None:
    be = FakeBackend(registration=False, bulk=False)
    c = client(be)
    results = await c.publish_batches([batch(1), batch(2)])
    assert [r.batch_id for r in results] == ["b1", "b2"] and set(be.auth_seen) == {"key"}
    assert c.auth_mode == "enrollment-key"
    await c.close()


async def test_network_errors_are_retryable_api_errors() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    c = AgentApiClient(
        "http://127.0.0.1:9", KEY, None, agent_version="t", transport=httpx.MockTransport(down)
    )
    c.bind_device("dev-1")
    with pytest.raises(ApiError) as err:
        await c.publish_batches([batch(1)])
    assert err.value.retryable and err.value.status is None
    await c.close()


def test_missing_enrollment_key_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        AgentApiClient("https://x.example.com", "", None, agent_version="t")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_credential_roundtrip(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.bin")
    store.save(DeviceCredential("dev-1", "secret-token", "https://twin"))
    raw = (tmp_path / "credentials.bin").read_bytes()
    assert b"secret-token" not in raw  # encrypted at rest
    loaded = store.load("dev-1", "https://twin")
    assert loaded is not None and loaded.token == "secret-token"
    assert store.load("other-device", "https://twin") is None
    assert store.load("dev-1", "https://other-backend") is None
    (tmp_path / "credentials.bin").write_bytes(b"corrupted")
    assert store.load("dev-1", "https://twin") is None  # unreadable -> re-register, no crash


async def test_concurrent_first_requests_register_exactly_once() -> None:
    import asyncio

    be = FakeBackend()
    c = client(be)
    await asyncio.gather(*(c.publish_batches([batch(i)]) for i in range(5)))
    assert len(be.tokens) == 1 and be.tokens["dev-1"] == "tok-1"
    assert len(be.received) == 5 and set(be.auth_seen) == {"token"}
    await c.close()


def big(i: int) -> bytes:
    return json.dumps({"batch_id": f"g{i}", "device_id": "dev-1", "samples": ["x" * 40] * 100}).encode()


async def test_bulk_is_gzipped_and_summary_consumed() -> None:
    be = FakeBackend()
    c = client(be)
    await c.publish_batches([big(1), big(2)])
    assert be.encodings == ["gzip"] and [b["batch_id"] for b in be.received] == ["g1", "g2"]
    assert c.last_summary is not None and c.last_summary.last_sequence == 7
    assert c.bytes_sent_total < c.bytes_uncompressed_total / 5  # repetitive JSON compresses well
    await c.close()


async def test_backend_without_gzip_gets_plain_bodies() -> None:
    be = FakeBackend(gzip_ok=False)
    c = client(be)
    await c.publish_batches([big(1)])
    await c.publish_batches([big(2)])
    assert be.encodings == ["gzip", "identity", "identity"]  # one probe, then plain
    assert [b["batch_id"] for b in be.received] == ["g1", "g2"]
    await c.close()


async def test_429_retry_after_is_surfaced() -> None:
    be = FakeBackend()
    be.throttle = True
    c = client(be)
    with pytest.raises(ApiError) as err:
        await c.publish_batches([batch(1)])
    assert err.value.status == 429 and err.value.retryable and err.value.retry_after_s == 42.0
    await c.close()


async def test_heartbeat_sends_versioned_identity() -> None:
    be = FakeBackend()
    c = client(be)
    out = await c.heartbeat({"agent_version": "t", "sent_at": "2026-10-07T00:00:00Z", "queue_depth": 3})
    assert out["presence"] == "ONLINE"
    assert be.heartbeats[0]["device_id"] == "dev-1" and be.heartbeats[0]["schema_version"] == "1.2"
    await c.close()
