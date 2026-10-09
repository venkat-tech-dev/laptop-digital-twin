# ruff: noqa: E501  (inline test payloads)
"""Shared Phase 9 fixtures: a platform with two organisations (acme, globex), their owners and enrolled devices,
all created through the public APIs (enrollment tokens, device tokens, telemetry)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import INVENTORY, sample, settings

PASSWORD = "Correct-Horse-9-Battery"
os.environ.setdefault("DATA_ENCRYPTION_KEY", Fernet.generate_key().decode())


def accounts_app(**kw: Any) -> TestClient:
    return TestClient(create_app(settings(**{"AUTH_MODE": "accounts", "JWT_SECRET": "x" * 40, **kw})))


def bearer(token: str, org: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}"}
    if org:
        h["X-Organization-Id"] = org
    return h


def login(c: TestClient, username: str, org: str | None = None, otp: str | None = None) -> dict[str, str]:
    body: dict[str, Any] = {"username": username, "password": PASSWORD}
    if org:
        body["organization_id"] = org
    if otp:
        body["otp"] = otp
    r = c.post("/api/v1/auth/login", json=body)
    assert r.status_code == 200, r.text
    return bearer(r.json()["access_token"])


@dataclass
class Device:
    device_id: str
    headers: dict[str, str]
    seq: int = 0


def enroll(c: TestClient, owner: dict[str, str], device_id: str, cpu: float = 30.0) -> Device:
    tok = c.post("/api/v1/org/enrollment-tokens", json={"ttl_hours": 1, "label": device_id}, headers=owner)
    assert tok.status_code == 201, tok.text
    r = c.post(
        "/api/v1/agent/enroll",
        json={"enrollment_token": tok.json()["token"], "device_id": device_id, "agent_version": "1.6.0"},
    )
    assert r.status_code == 200, r.text
    d = Device(device_id, {"Authorization": f"Bearer {r.json()['device_token']}", "X-Device-Id": device_id})
    env = {
        "device_id": device_id,
        "agent_version": "1.6.0",
        "discovered_at": datetime.now(UTC).isoformat(),
        "inventory": INVENTORY,
    }
    assert c.post("/api/v1/ingest/inventory", json=env, headers=d.headers).status_code == 202
    hb = {
        "device_id": device_id,
        "agent_version": "1.6.0",
        "sent_at": datetime.now(UTC).isoformat(),
        "run_mode": "console",
    }
    assert c.post("/api/v1/agent/heartbeat", json=hb, headers=d.headers).status_code == 200
    telemetry(c, d, cpu)
    return d


def telemetry(c: TestClient, d: Device, cpu: float, ts: datetime | None = None, expect: int = 202) -> Any:
    d.seq += 1
    ts = ts or datetime.now(UTC)
    body = {
        "device_id": d.device_id,
        "agent_version": "1.6.0",
        "sequence": d.seq,
        "sent_at": ts.isoformat(),
        "samples": [
            sample("cpu.usage_percent", cpu, ts=ts),
            sample("memory.usage_percent", 55.0, component="memory", ts=ts),
        ],
    }
    r = c.post("/api/v1/ingest/telemetry", json=body, headers=d.headers)
    assert r.status_code == expect, r.text
    return r


@dataclass
class World:
    c: TestClient
    root: dict[str, str]  # platform super-admin (default org owner)
    alice: dict[str, str]  # owner of acme
    bob: dict[str, str]  # owner of globex
    dev_a: Device
    dev_b: Device


def build_world(c: TestClient) -> World:
    r = c.post("/api/v1/auth/setup", json={"username": "root", "password": PASSWORD})
    assert r.status_code == 200, r.text
    root = bearer(r.json()["access_token"])
    for org, owner in (("acme", "alice"), ("globex", "bob")):
        assert (
            c.post(
                "/api/v1/platform/organizations", json={"org_id": org, "name": org.title()}, headers=root
            ).status_code
            == 201
        )
        m = c.post(
            "/api/v1/org/members",
            json={"username": owner, "password": PASSWORD, "role": "org_owner"},
            headers={**root, "X-Organization-Id": org},
        )
        assert m.status_code == 201, m.text
    alice, bob = login(c, "alice"), login(c, "bob")
    dev_a = enroll(c, alice, "dev-acme-0001")
    dev_b = enroll(c, bob, "dev-globex-0001")
    return World(c, root, alice, bob, dev_a, dev_b)


def past(seconds: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)
