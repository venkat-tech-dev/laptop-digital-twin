"""Phase 9 agent identity: single-use organization enrollment tokens and credential rotation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.transport.client import AgentApiClient, ApiError, ConfigurationError
from tests.test_client import MemoryCreds

ENROLL = "ldt_enr_" + "a" * 40
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


class OrgBackend:
    """Fake backend with enrollment tokens (single-use), expiring device tokens and rotation."""

    def __init__(self) -> None:
        self.unused = {ENROLL}
        self.valid: set[str] = set()
        self.expires = NOW + timedelta(days=90)
        self.paths: list[str] = []
        self.issued = 0

    def _issue(self, device: str) -> httpx.Response:
        self.issued += 1
        token = f"dev-tok-{self.issued}"
        self.valid = {token}  # issuing a new credential invalidates the previous one
        return httpx.Response(
            200,
            json={
                "device_id": device,
                "device_token": token,
                "organization_id": "acme",
                "credential_expires_at": self.expires.isoformat(),
            },
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        assert "X-Agent-Key" not in request.headers  # the shared key is never used with a token
        if path == "/api/v1/agent/enroll":
            body = json.loads(request.content)
            if body["enrollment_token"] not in self.unused:
                return httpx.Response(
                    401, json={"code": "ENROLLMENT_FAILED", "message": "enrollment token already used"}
                )
            self.unused.discard(body["enrollment_token"])
            return self._issue(body["device_id"])
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.valid:
            return httpx.Response(401, json={"code": "AUTHENTICATION_FAILED"})
        if path == "/api/v1/agent/credentials/rotate":
            return self._issue(request.headers["X-Device-Id"])
        if path == "/api/v1/agent/heartbeat":
            return httpx.Response(
                200, json={"presence": "ONLINE", "credential_expires_at": self.expires.isoformat()}
            )
        if path == "/api/v1/ingest/telemetry/bulk":
            batches = json.loads(
                request.content
                if request.headers.get("Content-Encoding") != "gzip"
                else __import__("gzip").decompress(request.content)
            )["batches"]
            return httpx.Response(
                200, json={"results": [{"batch_id": b["batch_id"], "status": "accepted"} for b in batches]}
            )
        return httpx.Response(404)


def make(be: OrgBackend, creds: MemoryCreds, **kw: object) -> AgentApiClient:
    c = AgentApiClient(
        "http://127.0.0.1:8000",
        "",
        creds,
        agent_version="1.7.0",
        enrollment_token=ENROLL,
        transport=httpx.MockTransport(be),
        **kw,
    )  # type: ignore[arg-type]
    c.bind_device("dev-1")
    return c


def batch(i: int) -> bytes:
    return json.dumps({"batch_id": f"b{i}", "device_id": "dev-1"}).encode()


async def test_enrolls_once_with_token_and_persists_credential() -> None:
    be, creds = OrgBackend(), MemoryCreds()
    c = make(be, creds)
    await c.publish_batches([batch(1)])
    await c.publish_batches([batch(2)])
    assert be.paths.count("/api/v1/agent/enroll") == 1
    assert creds.saved is not None and creds.saved.token == "dev-tok-1"
    assert c.credential_expires_at == be.expires and c.auth_mode == "device-token"
    await c.close()
    # restart: the stored credential is used; the spent enrollment token is not sent again
    c2 = make(be, creds)
    await c2.publish_batches([batch(3)])
    assert be.paths.count("/api/v1/agent/enroll") == 1
    await c2.close()


async def test_spent_or_invalid_token_is_a_clear_retryable_error() -> None:
    be = OrgBackend()
    be.unused.clear()
    c = make(be, MemoryCreds())
    with pytest.raises(ApiError) as err:
        await c.publish_batches([batch(1)])
    assert err.value.retryable and err.value.status == 401 and "AGENT_ENROLLMENT_TOKEN" in str(err.value)
    assert err.value.retry_after_s == 300.0
    await c.close()


async def test_rotates_before_expiry_only() -> None:
    be, creds = OrgBackend(), MemoryCreds()
    c = make(be, creds, rotate_before=timedelta(days=7))
    hb = await c.heartbeat({"agent_version": "1.7.0"})
    assert not await c.maybe_rotate(hb, now=NOW)  # 90 days left
    assert await c.maybe_rotate(hb, now=be.expires - timedelta(days=3))
    assert creds.saved is not None and creds.saved.token == "dev-tok-2"
    await c.publish_batches([batch(1)])  # the new credential works; the old one is gone server-side
    assert "dev-tok-1" not in be.valid
    await c.close()


async def test_rotation_failure_keeps_current_credential() -> None:
    be, creds = OrgBackend(), MemoryCreds()
    c = make(be, creds)
    await c.publish_batches([batch(1)])
    be.valid = set()  # e.g. device disabled / revoked meanwhile
    assert not await c.maybe_rotate({"credential_expires_at": NOW.isoformat()}, now=NOW)
    assert creds.saved is not None and creds.saved.token == "dev-tok-1"
    assert not await c.maybe_rotate({"credential_expires_at": "garbage"}, now=NOW)
    await c.close()


def test_configuration_requires_key_or_token() -> None:
    with pytest.raises(ConfigurationError):
        AgentApiClient("https://x.example.com", "", MemoryCreds(), agent_version="t")
    with pytest.raises(ConfigurationError):
        AgentApiClient("https://x.example.com", "", None, agent_version="t", enrollment_token=ENROLL)


def test_package_version_matches_the_reported_agent_version() -> None:
    """Phase 10: the version the backend governs (AGENT_VERSION) must be the packaged version."""
    import tomllib
    from pathlib import Path

    from app.runner import AGENT_VERSION

    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert project["project"]["version"] == AGENT_VERSION
