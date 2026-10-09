# ruff: noqa: E501  (inline test payloads)
"""Phase 9 - cross-tenant isolation: Tenant A (acme / alice) must never observe, modify, infer, subscribe to or
operate on Tenant B (globex / bob) data. Every GET route of the API is swept with B's identifiers; writes,
WebSockets, notifications, background jobs, exports and caches are checked explicitly."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.unit.tenancy_helpers import World, accounts_app, build_world
from tests.unit.test_alerting_api import anomaly


def settle(c: TestClient) -> None:
    ct = c.app.state.container  # type: ignore[attr-defined]
    for _ in range(200):
        c.portal.call(ct.alerts.drain)  # type: ignore[union-attr]
        if ct.remediation is not None:
            c.portal.call(ct.remediation.drain)  # type: ignore[union-attr]
        d = ct.diagnosis
        if not (d._queue.qsize() or d._active or d._pending):
            c.portal.call(ct.audit.flush)  # type: ignore[union-attr]
            return
        c.portal.call(asyncio.sleep, 0.05)  # type: ignore[union-attr,arg-type]


@pytest.fixture(scope="module")
def world() -> Iterator[tuple[World, dict[str, str]]]:
    with accounts_app() as c:
        w = build_world(c)
        ct = c.app.state.container  # type: ignore[attr-defined]
        # Tenant B data of every kind: alert -> diagnosis, remediation, policy, group, unit, token, audit
        c.portal.call(ct.bus.publish_all, [anomaly(device=w.dev_b.device_id, aid="an-globex")])  # type: ignore[union-attr]
        settle(c)
        alert_b = c.get("/api/v1/alerts", headers=w.bob).json()["items"][0]["alert_id"]
        rem_b = c.post(
            "/api/v1/remediations",
            json={"device_id": w.dev_b.device_id, "action_type": "REFRESH_TELEMETRY"},
            headers=w.bob,
        )
        assert rem_b.status_code == 201, rem_b.text
        diag_b = c.post(f"/api/v1/alerts/{alert_b}/diagnose", headers=w.bob)
        assert diag_b.status_code == 202, diag_b.text
        settle(c)
        diags = c.get(f"/api/v1/devices/{w.dev_b.device_id}/diagnoses", headers=w.bob).json()["items"]
        group_b = c.post("/api/v1/org/groups", json={"name": "Globex Finance"}, headers=w.bob).json()[
            "group_id"
        ]
        unit_b = c.post(
            "/api/v1/org/units", json={"kind": "department", "name": "Globex R&D"}, headers=w.bob
        ).json()["unit_id"]
        pol = c.post(
            "/api/v1/org/policies",
            json={"kind": "diagnosis", "scope_type": "organization", "body": {"auto_min_severity": "MEDIUM"}},
            headers=w.bob,
        ).json()
        token_b = c.post("/api/v1/org/enrollment-tokens", json={"ttl_hours": 1}, headers=w.bob).json()[
            "token_id"
        ]
        ids = {
            "device_id": w.dev_b.device_id,
            "alert_id": alert_b,
            "remediation_id": rem_b.json()["id"],
            "diagnosis_id": diags[0]["diagnosis_id"] if diags else "none",
            "group_id": group_b,
            "unit_id": unit_b,
            "policy_id": pol["policy_id"],
            "token_id": token_b,
            "username": "bob",
            "anomaly_id": "an-globex",
            "execution_id": rem_b.json()["execution_id"],
            "org_id": "globex",
        }
        yield w, ids


SECRETS = (
    "device_id",
    "alert_id",
    "remediation_id",
    "diagnosis_id",
    "group_id",
    "unit_id",
    "policy_id",
    "token_id",
    "execution_id",
)
SKIP = re.compile(
    r"^/(api/v1/(agent|ingest)/|scim/|health|metrics|docs|redoc|openapi|models/|api/v1/platform/)"
)


def leaks(text: str, ids: dict[str, str]) -> list[str]:
    found = [k for k in SECRETS if ids[k] and ids[k] != "none" and ids[k] in text]
    if re.search(r'"(username|actor_id|subject|requested_by|created_by|user_id)"\s*:\s*"bob"', text):
        found.append("username")
    if re.search(r'"(org_id|organization_id|tenant_id)"\s*:\s*"globex"', text):
        found.append("org_id")
    return found


def test_every_get_route_is_tenant_scoped(world: tuple[World, dict[str, str]]) -> None:
    w, ids = world
    c = w.c
    checked, problems = 0, []
    params = {
        **ids,
        "user_id": "bob",
        "workspace_id": ids["group_id"],
        "target_id": "cpu",
        "kind": "agent",
        "job_id": ids["execution_id"],
        "notification_id": "n-x",
        "provider_id": "p-x",
        "series_id": "x",
        "prediction_id": "p-x",
        "component_id": "cpu",
        "metric": "cpu.usage_percent",
        "action_id": "REFRESH_TELEMETRY",
    }
    for route_path, ops in c.app.openapi()["paths"].items():  # type: ignore[attr-defined]
        if "get" not in ops or SKIP.match(route_path):
            continue
        path = route_path
        for name in re.findall(r"{(\w+)}", path):
            path = path.replace("{" + name + "}", params.get(name, ids["device_id"]))
        for query in ("", f"?device_id={ids['device_id']}", f"?alert_id={ids['alert_id']}"):
            r = c.get(path + query, headers=w.alice)
            checked += 1
            if r.status_code < 400:
                bad = leaks(r.text, ids)
                if bad:
                    problems.append(f"{route_path}{query} -> {r.status_code} leaks {bad}")
    assert checked > 150
    assert problems == [], "\n".join(problems)


def test_organization_header_cannot_be_spoofed(world: tuple[World, dict[str, str]]) -> None:
    w, ids = world
    r = w.c.get(
        f"/api/v1/devices/{ids['device_id']}/twin", headers={**w.alice, "X-Organization-Id": "globex"}
    )
    assert r.status_code == 403 and r.json()["code"] == "ORGANIZATION_ACCESS_DENIED"
    assert (
        w.c.get("/api/v1/org/devices", headers={**w.alice, "X-Organization-Id": "globex"}).status_code == 403
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v1/alerts/{alert_id}/acknowledge", {"note": "x"}),
        ("POST", "/api/v1/alerts/{alert_id}/resolve", {"note": "x"}),
        ("POST", "/api/v1/alerts/{alert_id}/diagnose", None),
        ("POST", "/api/v1/remediations/{remediation_id}/approve", None),
        ("POST", "/api/v1/remediations/{remediation_id}/cancel", None),
        ("POST", "/api/v1/remediations/{remediation_id}/dry-run", None),
        ("POST", "/api/v1/remediations", {"device_id": "{device_id}", "action_type": "REFRESH_TELEMETRY"}),
        ("POST", "/api/v1/diagnoses/{diagnosis_id}/feedback", {"verdict": "CORRECT"}),
        ("POST", "/api/v1/org/devices/{device_id}/lifecycle", {"to": "DISABLED"}),
        ("POST", "/api/v1/org/groups/{group_id}/members", {"add": []}),
        ("PATCH", "/api/v1/org/groups/{group_id}", {"name": "pwned"}),
        ("POST", "/api/v1/org/units/{unit_id}/archive", None),
        ("POST", "/api/v1/org/policies/{policy_id}/publish", {"version": 1}),
        ("POST", "/api/v1/org/policies/{policy_id}/archive", None),
        ("POST", "/api/v1/org/enrollment-tokens/{token_id}/revoke", None),
        ("PUT", "/api/v1/org/members/{username}", {"role": "employee"}),
        ("POST", "/api/v1/org/members/{username}/revoke-sessions", None),
        (
            "POST",
            "/api/v1/org/devices/{device_id}/data-deletion",
            {"confirm_device_id": "{device_id}", "reason": "attack"},
        ),
    ],
)
def test_writes_on_other_tenant_resources_fail(
    world: tuple[World, dict[str, str]], method: str, path: str, body: Any
) -> None:
    w, ids = world
    url = path.format(**ids)
    if body is not None:
        body = {k: (v.format(**ids) if isinstance(v, str) else v) for k, v in body.items()}
    r = w.c.request(method, url, json=body, headers=w.alice)
    assert r.status_code in (401, 403, 404, 422), (url, r.status_code, r.text)
    assert r.status_code != 200
    assert leaks(r.text, ids) == []


def test_adding_other_tenant_device_to_own_group_fails(world: tuple[World, dict[str, str]]) -> None:
    w, ids = world
    g = w.c.post("/api/v1/org/groups", json={"name": "Acme Laptops"}, headers=w.alice).json()["group_id"]
    r = w.c.post(f"/api/v1/org/groups/{g}/members", json={"add": [ids["device_id"]]}, headers=w.alice)
    assert r.status_code == 404


def test_policy_scope_on_other_tenant_device_fails(world: tuple[World, dict[str, str]]) -> None:
    w, ids = world
    r = w.c.post(
        "/api/v1/org/policies",
        json={
            "kind": "diagnosis",
            "scope_type": "device",
            "scope_id": ids["device_id"],
            "body": {"enabled": False},
        },
        headers=w.alice,
    )
    assert r.status_code == 404


def test_websocket_never_carries_other_tenant_data(world: tuple[World, dict[str, str]]) -> None:
    w, ids = world
    c = w.c
    token = w.alice["Authorization"][7:]
    with c.websocket_connect(f"/ws/twin?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["event"] == "connection_status" and hello["primary_device_id"] == w.dev_a.device_id
        ws.send_json({"type": "subscribe", "topics": [f"device:{ids['device_id']}", "fleet"]})
        msgs = []
        for _ in range(30):
            m = ws.receive_json()
            msgs.append(m)
            if m["event"] == "subscribed":
                break
        sub = msgs[-1]
        assert {"topic": f"device:{ids['device_id']}", "reason": "unknown_device"} in sub["rejected"]
        ct = c.app.state.container  # type: ignore[attr-defined]
        # B's events while A listens: nothing about B may arrive (fleet included)
        c.portal.call(ct.bus.publish_all, [anomaly(device=ids["device_id"], aid="an-globex-2")])  # type: ignore[union-attr]
        settle(c)
        ws.send_json({"type": "ping"})
        seen = []
        for _ in range(60):
            m = ws.receive_json()
            seen.append(m)
            if m["event"] == "pong":
                break
        # the rejection only echoes the topic the client itself sent; nothing else may mention B
        text = str([m for m in msgs if m["event"] != "subscribed"] + seen)
        assert ids["device_id"] not in text and "globex" not in text


def test_websocket_org_spoof_is_refused(world: tuple[World, dict[str, str]]) -> None:
    w, _ = world
    from starlette.websockets import WebSocketDisconnect

    token = w.alice["Authorization"][7:]
    with (
        pytest.raises(WebSocketDisconnect),
        w.c.websocket_connect(f"/ws/twin?token={token}&org=globex") as ws,
    ):
        ws.receive_json()


def test_notifications_reach_only_the_device_organization(world: tuple[World, dict[str, str]]) -> None:
    w, ids = world
    bob = w.c.get("/api/v1/notifications", headers=w.bob).json()
    alice = w.c.get("/api/v1/notifications", headers=w.alice).json()
    root = w.c.get("/api/v1/notifications", headers=w.root).json()
    assert any(n["device_id"] == ids["device_id"] for n in bob["items"])
    assert not any(n["device_id"] == ids["device_id"] for n in alice["items"])
    assert not any(
        n["device_id"] == ids["device_id"] for n in root["items"]
    )  # platform admin is not a globex member
    ct = w.c.app.state.container  # type: ignore[attr-defined]
    recipients = {u for u, _ in ct.tenancy.recipients(ids["device_id"])}
    assert recipients == {"bob"}


def test_background_records_carry_the_owning_tenant(world: tuple[World, dict[str, str]]) -> None:
    w, ids = world
    ct = w.c.app.state.container  # type: ignore[attr-defined]
    rem = ct.remediation.items[ids["remediation_id"]]
    assert rem.tenant_id == "globex"
    if ids["diagnosis_id"] != "none":
        d = w.c.portal.call(ct.diagnosis.repo.get, ids["diagnosis_id"])  # type: ignore[union-attr]
        assert d.tenant_id == "globex"


def test_audit_search_and_export_are_tenant_scoped(world: tuple[World, dict[str, str]]) -> None:
    w, ids = world
    a = w.c.get("/api/v1/org/audit?limit=500", headers=w.alice).json()["items"]
    assert a and all(e["org_id"] == "acme" for e in a)
    csv = w.c.get("/api/v1/org/audit/export?fmt=csv", headers=w.alice)
    assert csv.status_code == 200 and "globex" not in csv.text and "bob" not in csv.text
    b = w.c.get("/api/v1/org/audit?limit=500", headers=w.bob).json()["items"]
    assert any(e["action"] == "device.enrolled" and e["resource_id"] == ids["device_id"] for e in b)


def test_policy_cache_is_per_tenant(world: tuple[World, dict[str, str]]) -> None:
    w, ids = world
    ct = w.c.app.state.container  # type: ignore[attr-defined]
    p = w.c.post(
        "/api/v1/org/policies",
        json={"kind": "agent", "scope_type": "organization", "body": {"telemetry_interval_ms": 2000}},
        headers=w.alice,
    ).json()
    assert (
        w.c.post(
            f"/api/v1/org/policies/{p['policy_id']}/publish", json={"version": p["version"]}, headers=w.alice
        ).status_code
        == 200
    )
    assert ct.policies.value("agent", "telemetry_interval_ms", device_id=w.dev_a.device_id) == 2000
    assert (
        ct.policies.value("agent", "telemetry_interval_ms", device_id=ids["device_id"]) == 5000
    )  # B untouched
    eff = w.c.get(f"/api/v1/org/policies/effective?kind=agent&device_id={ids['device_id']}", headers=w.alice)
    assert eff.status_code == 404


def test_platform_routes_require_platform_admin(world: tuple[World, dict[str, str]]) -> None:
    w, _ = world
    assert w.c.get("/api/v1/platform/organizations", headers=w.alice).status_code == 403
    assert w.c.get("/api/v1/platform/organizations", headers=w.root).status_code == 200
    # legacy platform-wide administration (alert policy, users) is not available to another organization's owner
    assert w.c.get("/api/v1/users", headers=w.alice).status_code == 403
    assert (
        w.c.put("/api/v1/remediation-policy", json={"four_eyes_min_risk": "LOW"}, headers=w.alice).status_code
        == 403
    )
    assert (
        w.c.post(
            "/api/v1/remediation-policy/kill-switch",
            json={"scope": "global", "enabled": True},
            headers=w.alice,
        ).status_code
        == 403
    )
    ks = w.c.post(
        "/api/v1/remediation-policy/kill-switch",
        json={"scope": "tenant", "target": "acme", "enabled": True},
        headers=w.alice,
    )
    assert ks.status_code == 200  # an organization may stop its own remediation
    w.c.post(
        "/api/v1/remediation-policy/kill-switch",
        json={"scope": "tenant", "target": "acme", "enabled": False},
        headers=w.alice,
    )
    assert (
        w.c.post(
            "/api/v1/remediation-policy/kill-switch",
            json={"scope": "tenant", "target": "globex", "enabled": True},
            headers=w.alice,
        ).status_code
        == 403
    )


def test_public_provider_listing_does_not_enumerate_tenants(world: tuple[World, dict[str, str]]) -> None:
    w, _ = world
    assert w.c.get("/api/v1/auth/providers").status_code == 422
    assert w.c.get("/api/v1/auth/providers?organization=acme").json() == {"items": []}


def test_aggregate_statistics_never_include_other_organizations(world: tuple[World, dict[str, str]]) -> None:
    """Phase 10 regression: aggregates (no device ids in the body) must still be tenant-scoped."""
    from datetime import UTC, datetime, timedelta

    from app.domain.prediction.engine import Prediction

    w, _ = world
    c = w.c
    svc = c.app.state.container.forecasts  # type: ignore[attr-defined]
    now = datetime.now(UTC)
    p = Prediction(
        "pred-globex-1", w.dev_b.device_id, "k", "memory", "threshold", "memory.usage_percent", "%", "up",
        "EXPIRED", "HIGH", 80.0, 95.0, 96.0, now, 3600.0, now, now, now, 90.0, 99.0, 0.8, "HIGH", "theil_sen",
        "v1", "f1", now - timedelta(hours=2), now, "statement", {}, now, now, now, closed_at=now,
    )  # fmt: skip
    c.portal.call(svc.repo.upsert, p)  # type: ignore[union-attr]
    acme = c.get("/api/v1/prediction-accuracy", headers=w.alice)
    globex = c.get("/api/v1/prediction-accuracy", headers=w.bob)
    if acme.status_code == 200:
        assert acme.json()["overall"]["closed"] == 0  # globex's outcome is not in acme's statistics
    assert globex.status_code != 200 or globex.json()["overall"]["closed"] == 1
    assert c.get("/api/v1/diagnosis-config/status", headers=w.alice).status_code == 403  # platform only
    assert c.get("/api/v1/diagnosis-config/status", headers=w.root).status_code == 200
