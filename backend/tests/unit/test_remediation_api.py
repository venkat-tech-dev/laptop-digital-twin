"""Phase 8 - integration (telemetry -> anomaly -> alert -> diagnosis -> recommendation -> approval -> signed
envelope -> agent -> verification -> twin / timeline / notification / audit), failure and security tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.domain.remediation.envelope import Signer, verify
from app.domain.remediation.models import Remediation, Status
from app.main import create_app
from app.services.remediation import RemediationService
from tests.conftest import AGENT_KEY, DEVICE, INVENTORY, sample, settings
from tests.unit.test_alerting_api import PASSWORD, anomaly

KEY = {"X-Agent-Key": AGENT_KEY}
SIGNING_KEY = Signer.generate_b64()


def make_settings(**kw: Any) -> Any:
    return settings(REMEDIATION_SIGNING_KEY=SIGNING_KEY, **kw)


class Agent:
    """A simulated endpoint agent with a real per-device token."""

    def __init__(self, c: TestClient, device: str = DEVICE) -> None:
        self.c, self.device, self.seq = c, device, 0
        env = {
            "device_id": device,
            "agent_version": "1.6.0",
            "discovered_at": datetime.now(UTC).isoformat(),
            "inventory": INVENTORY,
        }
        assert c.post("/api/v1/ingest/inventory", json=env, headers=KEY).status_code in (200, 202)
        r = c.post(
            "/api/v1/agent/register", json={"device_id": device, "agent_version": "1.6.0"}, headers=KEY
        )
        assert r.status_code in (200, 201), r.text
        self.headers = {"Authorization": f"Bearer {r.json()['device_token']}", "X-Device-Id": device}
        self.heartbeat()

    def heartbeat(self, actions: list[str] | None = None, run_mode: str = "console") -> None:
        body = {
            "device_id": self.device,
            "agent_version": "1.6.0",
            "sent_at": datetime.now(UTC).isoformat(),
            "run_mode": run_mode,
            "remediation_actions": actions
            if actions is not None
            else [
                "REFRESH_TELEMETRY",
                "REQUEST_SYSTEM_RESCAN",
                "RECONNECT_AGENT",
                "RESTART_KNOWN_APPLICATION",
            ],
            "restartable_applications": ["slack.desktop"],
        }
        assert self.c.post("/api/v1/agent/heartbeat", json=body, headers=self.headers).status_code == 200

    def telemetry(
        self, cpu: float, ts: datetime | None = None, slack: bool = True, hot: bool = False
    ) -> None:
        ts = ts or datetime.now(UTC)
        self.seq += 1
        procs = None
        if slack:
            procs = {
                "timestamp": ts.isoformat(),
                "source": "t",
                "total_processes": 1,
                "processes": [
                    {
                        "pid": 10 + self.seq,
                        "name": "slack.exe",
                        "status": "running",
                        "cpu_percent": 70.0 if hot else 2.0,
                        "memory_rss_bytes": 300 * 2**20,
                        "memory_percent": None,
                        "num_threads": 4,
                        "io_read_bytes_per_sec": None,
                        "io_write_bytes_per_sec": None,
                    }
                ],
            }
        body = {
            "device_id": self.device,
            "agent_version": "1.6.0",
            "sequence": self.seq,
            "sent_at": ts.isoformat(),
            "samples": [
                sample("cpu.usage_percent", cpu, ts=ts),
                sample("memory.usage_percent", 50.0, component="memory", ts=ts),
            ],
            "processes": procs,
        }
        assert self.c.post("/api/v1/ingest/telemetry", json=body, headers=self.headers).status_code in (
            200,
            202,
        )

    def pull(self) -> list[dict[str, Any]]:
        r = self.c.get("/api/v1/agent/actions", params={"device_id": self.device}, headers=self.headers)
        assert r.status_code == 200, r.text
        return list(r.json()["items"])

    def report(self, execution_id: str, phase: str, detail: str = "", **data: Any) -> Any:
        return self.c.post(
            f"/api/v1/agent/actions/{execution_id}/report",
            json={"phase": phase, "detail": detail, "data": data},
            headers=self.headers,
        )


def svc_of(c: TestClient) -> RemediationService:
    s = c.app.state.container.remediation  # type: ignore[attr-defined]
    assert s is not None
    return s


def tick(c: TestClient, now: datetime | None = None) -> None:
    c.portal.call(svc_of(c).tick, now or datetime.now(UTC))  # type: ignore[union-attr]


def settle(c: TestClient) -> None:
    container = c.app.state.container  # type: ignore[attr-defined]
    for _ in range(200):
        c.portal.call(container.alerts.drain)  # type: ignore[union-attr]
        d = container.diagnosis
        c.portal.call(svc_of(c).drain)  # type: ignore[union-attr]
        if not (d._queue.qsize() or d._active or d._pending) and svc_of(c)._queue.empty():
            return
        c.portal.call(asyncio.sleep, 0.05)  # type: ignore[union-attr,arg-type]
    raise AssertionError("did not settle")


def hot_cpu(agent: Agent, minutes: int = 12) -> None:
    start = datetime.now(UTC) - timedelta(minutes=minutes)
    for i in range(minutes * 2):
        hot = i >= 8
        agent.telemetry(92.0 if hot else 15.0, start + timedelta(seconds=30 * i), hot=hot)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(make_settings())) as c:
        yield c


def request(c: TestClient, action: str = "REFRESH_TELEMETRY", **kw: Any) -> dict[str, Any]:
    r = c.post("/api/v1/remediations", json={"device_id": DEVICE, "action_type": action, **kw})
    assert r.status_code == 201, r.text
    return dict(r.json())


# ------------------------------------------------------------------ end to end
def test_diagnosis_to_verified_restart(client: TestClient) -> None:
    c = client
    agent = Agent(c)
    hot_cpu(agent)
    c.portal.call(c.app.state.container.bus.publish_all, [anomaly(aid="cpu-1")])  # type: ignore[attr-defined,union-attr]
    settle(c)
    items = c.get("/api/v1/remediations", params={"device_id": DEVICE}).json()["items"]
    rec = next(i for i in items if i["action_type"] == "RESTART_KNOWN_APPLICATION")
    assert rec["status"] == "PENDING_APPROVAL" and rec["parameters"] == {"application_id": "slack.desktop"}
    assert rec["requested_by"] == "system" and rec["recommendation_source"] == "rules"
    assert rec["diagnosis_confidence"] and rec["action_confidence"] and rec["expected_success_probability"]
    detail = c.get(f"/api/v1/remediations/{rec['id']}").json()
    assert detail["action"]["rollback"] == "NOT_AVAILABLE" and detail["action"]["reversible"] is False
    assert detail["evidence"] and "slack" in detail["reason"].lower()
    alerts = c.get("/api/v1/alerts", params={"device_id": DEVICE}).json()["items"]
    assert any(a["title"].startswith("Approval required") for a in alerts)  # people are told
    plan = c.post(f"/api/v1/remediations/{rec['id']}/dry-run").json()
    assert plan["dry_run"] and {p["check"] for p in plan["preconditions"]} >= {
        "device_online",
        "device_allows",
        "application_running",
    }
    assert (
        c.get(f"/api/v1/remediations/{rec['id']}").json()["status"] == "PENDING_APPROVAL"
    )  # dry run changed nothing

    r = c.post(f"/api/v1/remediations/{rec['id']}/approve", json={"note": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "QUEUED"
    tick(c)
    envs = agent.pull()
    assert len(envs) == 1 and envs[0]["action_id"] == "RESTART_KNOWN_APPLICATION"
    pub = c.get("/api/v1/agent/actions/key", headers=agent.headers).json()["public_key"]
    env = verify(pub, envs[0])  # the agent can verify what it got
    assert (
        env.device_id == DEVICE and env.approved_by and env.parameters == {"application_id": "slack.desktop"}
    )
    assert agent.report(env.execution_id, "accepted").json()["status"] == "EXECUTING"
    assert (
        agent.report(
            env.execution_id, "completed", "slack.exe closed and restarted", running_after=True
        ).json()["status"]
        == "VERIFYING"
    )
    time.sleep(0.05)  # next clock tick: telemetry must be newer than the completion
    agent.telemetry(20.0)  # fresh telemetry after completion; CPU has dropped; slack runs again
    tick(c)
    done = c.get(f"/api/v1/remediations/{rec['id']}").json()
    assert done["status"] == "SUCCEEDED", done["verification"]
    assert {ch["check"]: ch["state"] for ch in done["verification"]["checks"]} == {
        "agent_completed": "PASS",
        "fresh_telemetry": "PASS",
        "application_running": "PASS",
        "target_improved": "PASS",
    }
    assert done["verification"]["baseline"] > done["verification"]["after"]
    assert "signature" not in done["execution"]["envelope"]
    actions = [a["action"] for a in done["audit"]]
    assert actions[:2] == ["proposed", "approval_required"] and "verified" in actions
    flat = c.get(f"/api/v1/devices/{DEVICE}/twin", params={"format": "flat"}).json()["state"]
    assert (
        flat["remediation.status"] == "SUCCEEDED"
        and flat["remediation.latest"]["remediation_id"] == rec["id"]
    )
    tl = c.get(f"/api/v1/devices/{DEVICE}/timeline").json()["items"]
    assert any(e["type"] == "remediation.succeeded" for e in tl)
    assert c.get("/api/v1/remediation-admin/audit/verify").json()["ok"] is True
    # duplicate report after completion changes nothing (idempotent)
    assert agent.report(env.execution_id, "failed", "late").json()["status"] == "SUCCEEDED"


def test_low_risk_manual_request_and_verification(client: TestClient) -> None:
    c = client
    agent = Agent(c)
    agent.telemetry(30.0)
    rec = request(c)
    assert rec["status"] == "PENDING_APPROVAL" and rec["approval_expires_at"]
    c.post(f"/api/v1/remediations/{rec['id']}/approve")
    tick(c)
    env = agent.pull()[0]
    agent.report(env["execution_id"], "completed", "collected")
    tick(c)
    assert svc_of(c).items[rec["id"]].status == Status.VERIFYING  # done on the device != verified
    # verification needs telemetry strictly newer than the completion; Windows' clock ticks every ~15.6 ms,
    # so a batch sent in the same tick would carry the same timestamp and (correctly) not count
    time.sleep(0.05)
    agent.telemetry(31.0)
    tick(c)
    assert svc_of(c).items[rec["id"]].status == Status.SUCCEEDED


def test_auto_remediation_only_with_explicit_policy(client: TestClient) -> None:
    c = client
    Agent(c).telemetry(30.0)
    svc = svc_of(c)
    from app.domain.remediation.recommend import Candidate

    cand = Candidate("REFRESH_TELEMETRY", {}, "stale", diagnosis_confidence=0.9, action_confidence=0.5)
    r1 = c.portal.call(svc._propose_system, DEVICE, cand)  # type: ignore[union-attr]
    assert r1 is not None and r1.status == Status.PENDING_APPROVAL  # default: manual
    c.post(f"/api/v1/remediations/{r1.remediation_id}/cancel")
    pol = c.get("/api/v1/remediation-policy").json()
    pol["risk_modes"]["LOW"] = "AUTO_APPROVE_LOW_RISK"
    assert (
        c.put(
            "/api/v1/remediation-policy",
            json={"risk_modes": pol["risk_modes"], "auto_remediation_enabled": True},
        ).status_code
        == 200
    )
    r2 = c.portal.call(svc._propose_system, DEVICE, cand)  # type: ignore[union-attr]
    assert r2 is not None and r2.status == Status.QUEUED and r2.approved_by == "policy"
    low_conf = Candidate("REQUEST_SYSTEM_RESCAN", {}, "x", diagnosis_confidence=0.4)
    r3 = c.portal.call(svc._propose_system, DEVICE, low_conf)  # type: ignore[union-attr]
    assert r3 is not None and r3.status == Status.PENDING_APPROVAL


# ------------------------------------------------------------------ failure handling
def test_rejection_by_device_failure_and_circuit_breaker(client: TestClient) -> None:
    c = client
    agent = Agent(c)
    agent.telemetry(30.0)
    svc = svc_of(c)
    past = datetime.now(UTC) - timedelta(seconds=400)
    for i in range(2):  # two earlier failures (outside the 300 s cooldown, inside the 24 h window)
        r = Remediation(
            f"old{i}",
            "default",
            DEVICE,
            "REFRESH_TELEMETRY",
            1,
            "Refresh",
            "x",
            "LOW",
            True,
            "MANUAL_APPROVAL",
            {},
            [],
            {},
            "NOT_NEEDED",
            Status.FAILED,
            "system",
            past,
            past,
            "c",
            f"e{i}",
            failed_at=past,
            started_at=past,
        )
        svc._index(r)
    rec = request(c)
    c.post(f"/api/v1/remediations/{rec['id']}/approve")
    tick(c)
    env = agent.pull()[0]
    agent.report(
        env["execution_id"], "rejected", "REFRESH_TELEMETRY is not allowed on this device (local policy)"
    )
    r = c.get(f"/api/v1/remediations/{rec['id']}").json()
    assert r["status"] == "FAILED" and "refused" in r["failure_reason"]
    assert any(a["action"] == "circuit_opened" for a in r["audit"])
    bad = c.post("/api/v1/remediations", json={"device_id": DEVICE, "action_type": "REFRESH_TELEMETRY"})
    assert bad.status_code == 409 and bad.json()["detail"]["code"] == "circuit_open"
    alerts = c.get("/api/v1/alerts", params={"device_id": DEVICE}).json()["items"]
    assert any("stopped after repeated failures" in a["title"] for a in alerts)
    assert any(
        x["action"] == "REFRESH_TELEMETRY"
        for x in c.get("/api/v1/remediation-admin/status").json()["open_circuits"]
    )


def test_approval_expiry_offline_and_kill_switch(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    c = client
    agent = Agent(c)
    agent.telemetry(30.0)
    rec = request(c)
    tick(c, datetime.now(UTC) + timedelta(minutes=31))
    assert c.get(f"/api/v1/remediations/{rec['id']}").json()["status"] == "EXPIRED"
    # offline: approved actions wait instead of executing
    rec = request(c, "REQUEST_SYSTEM_RESCAN")
    c.post(f"/api/v1/remediations/{rec['id']}/approve")
    svc = svc_of(c)
    monkeypatch.setattr(svc._presence, "presence_of", lambda d: "OFFLINE")
    tick(c)
    r = svc.items[rec["id"]]
    assert r.status == Status.QUEUED and r.execution["waiting_on"] == "device_online" and agent.pull() == []
    monkeypatch.undo()
    # kill switch: nothing new may begin, approvals are refused
    assert (
        c.post(
            "/api/v1/remediation-policy/kill-switch", json={"scope": "global", "enabled": True}
        ).status_code
        == 200
    )
    tick(c)
    assert r.status == Status.QUEUED and r.execution["waiting_on"] == "kill_switch"
    other = request(c, "RECONNECT_AGENT")
    refused = c.post(f"/api/v1/remediations/{other['id']}/approve")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "kill_switch"
    c.post("/api/v1/remediation-policy/kill-switch", json={"scope": "global", "enabled": False})
    tick(c)
    assert r.status == Status.VALIDATING


def test_envelope_reissue_timeouts_and_cancel(client: TestClient) -> None:
    c = client
    agent = Agent(c)
    agent.telemetry(30.0)
    rec = request(c)
    c.post(f"/api/v1/remediations/{rec['id']}/approve")
    tick(c)
    svc = svc_of(c)
    r = svc.items[rec["id"]]
    first_nonce = r.execution["envelope"]["nonce"]
    tick(c, datetime.now(UTC) + timedelta(minutes=3))  # not picked up before it expired
    assert r.status == Status.QUEUED
    tick(c)
    assert r.status == Status.VALIDATING and r.execution["issues"] == 2
    assert (
        r.execution["envelope"]["execution_id"] == r.execution_id
        and r.execution["envelope"]["nonce"] != first_nonce
    )
    env = agent.pull()[0]
    agent.report(env["execution_id"], "accepted")
    cancel = c.post(f"/api/v1/remediations/{rec['id']}/cancel")
    assert cancel.status_code == 409  # running on the device: not interrupted
    tick(c, datetime.now(UTC) + timedelta(minutes=5))
    assert r.status == Status.FAILED and "execution timeout" in (r.failure_reason or "")
    rec2 = request(c, "RECONNECT_AGENT")
    assert c.post(f"/api/v1/remediations/{rec2['id']}/cancel").json()["status"] == "CANCELLED"


def test_dry_run_execution_changes_nothing(client: TestClient) -> None:
    c = client
    agent = Agent(c)
    agent.telemetry(30.0)
    rec = request(
        c, "RESTART_KNOWN_APPLICATION", parameters={"application_id": "slack.desktop"}, dry_run=True
    )
    assert rec["status"] == "QUEUED" and rec["dry_run"] is True  # no approval needed: nothing changes
    tick(c)
    env = agent.pull()[0]
    assert env["dry_run"] is True
    agent.report(env["execution_id"], "completed", "dry run: would ask slack.exe to close", dry_run=True)
    r = c.get(f"/api/v1/remediations/{rec['id']}").json()
    assert r["status"] == "SUCCEEDED" and r["verification"]["outcome"] == "DRY_RUN"
    assert (
        c.get(f"/api/v1/devices/{DEVICE}/twin", params={"format": "flat"}).json()["state"][
            "remediation.status"
        ]
        == "NONE"
    )


# ------------------------------------------------------------------ security
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"action_type": "RUN_POWERSHELL", "parameters": {"script": "Remove-Item C:\\ -Recurse"}}, 422),
        (
            {
                "action_type": "RESTART_KNOWN_APPLICATION",
                "parameters": {"application_id": "C:\\Windows\\System32\\cmd.exe"},
            },
            422,
        ),
        ({"action_type": "RESTART_KNOWN_APPLICATION", "parameters": {"application_id": "evil.app"}}, 422),
        (
            {
                "action_type": "RESTART_KNOWN_APPLICATION",
                "parameters": {"application_id": "slack.desktop", "exe": "x.exe"},
            },
            422,
        ),
        ({"action_type": "RESTART_AGENT"}, 422),
        ({"action_type": "REFRESH_TELEMETRY", "command": "whoami"}, 422),
        ({"action_type": "refresh; rm -rf /"}, 422),
    ],
)
def test_injection_and_arbitrary_actions_are_refused(
    client: TestClient, body: dict[str, Any], code: int
) -> None:
    Agent(client).telemetry(30.0)
    r = client.post("/api/v1/remediations", json={"device_id": DEVICE, **body})
    assert r.status_code == code, r.text


def test_agent_endpoint_security(client: TestClient) -> None:
    c = client
    a = Agent(c)
    b = Agent(c, "ldt-other-device")
    a.telemetry(30.0)
    rec = request(c)
    c.post(f"/api/v1/remediations/{rec['id']}/approve")
    tick(c)
    assert c.get("/api/v1/agent/actions", params={"device_id": DEVICE}, headers=KEY).status_code in (401, 403)
    assert c.get("/api/v1/agent/actions", params={"device_id": DEVICE}, headers=b.headers).status_code == 403
    assert b.pull() == []
    env = a.pull()[0]
    assert b.report(env["execution_id"], "completed").status_code == 404  # another device cannot report it
    assert a.report("not-an-execution", "completed").status_code == 404
    assert (
        c.post(
            f"/api/v1/agent/actions/{env['execution_id']}/report",
            json={"phase": "completed", "data": {"x": {"nested": 1}}},
            headers=a.headers,
        ).status_code
        == 422
    )


def test_policy_validation_api(client: TestClient) -> None:
    c = client
    assert c.put("/api/v1/remediation-policy", json={"approval_ttl_s": 1}).status_code == 422
    assert (
        c.put(
            "/api/v1/remediation-policy",
            json={"extra_applications": [{"application_id": "x.y", "executables": ["..\\cmd.exe"]}]},
        ).status_code
        == 422
    )
    ok = c.put("/api/v1/remediation-policy", json={"four_eyes_min_risk": "MEDIUM"})
    assert ok.status_code == 200 and ok.json()["version"] == 1 and ok.json()["updated_by"]
    cat = c.get("/api/v1/action-catalog").json()
    assert {a["action_id"] for a in cat["actions"] if not a["enabled"]} == {
        "RESTART_AGENT",
        "RESTART_KNOWN_WINDOWS_SERVICE",
        "CLEAR_APPLICATION_CACHE",
        "CLEAN_KNOWN_TEMPORARY_DATA",
    }


@pytest.fixture
def accounts() -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(AUTH_MODE="accounts", JWT_SECRET="x" * 40))) as c:
        yield c


def test_rbac_four_eyes_and_isolation(accounts: TestClient) -> None:
    c = accounts
    admin = {
        "Authorization": "Bearer "
        + c.post("/api/v1/auth/setup", json={"username": "admin", "password": PASSWORD}).json()[
            "access_token"
        ]
    }
    mine, other = Agent(c, "dev-mine"), Agent(c, "dev-other")
    mine.telemetry(30.0)
    other.telemetry(30.0)
    for user, role in (("ana", "employee"), ("vic", "viewer"), ("ops", "operator"), ("ops2", "operator")):
        assert (
            c.post(
                "/api/v1/users", json={"username": user, "password": PASSWORD, "role": role}, headers=admin
            ).status_code
            == 201
        )
    c.put("/api/v1/devices/dev-mine/assignment", json={"username": "ana"}, headers=admin)

    def login(u: str) -> dict[str, str]:
        return {
            "Authorization": "Bearer "
            + c.post("/api/v1/auth/login", json={"username": u, "password": PASSWORD}).json()["access_token"]
        }

    ana, vic, ops, ops2 = login("ana"), login("vic"), login("ops"), login("ops2")
    restart = {"action_type": "RESTART_KNOWN_APPLICATION", "parameters": {"application_id": "slack.desktop"}}
    # employee: own device only, LOW only
    assert (
        c.post(
            "/api/v1/remediations",
            json={"device_id": "dev-other", "action_type": "REFRESH_TELEMETRY"},
            headers=ana,
        ).status_code
        == 404
    )
    assert (
        c.post("/api/v1/remediations", json={"device_id": "dev-mine", **restart}, headers=ana).status_code
        == 403
    )
    low = c.post(
        "/api/v1/remediations",
        json={"device_id": "dev-mine", "action_type": "REFRESH_TELEMETRY"},
        headers=ana,
    )
    assert low.status_code == 201
    assert c.post(f"/api/v1/remediations/{low.json()['id']}/approve", headers=ana).status_code == 403
    assert c.get("/api/v1/remediations", params={"device_id": "dev-other"}, headers=ana).status_code == 404
    # viewer: read-only
    assert (
        c.post(
            "/api/v1/remediations",
            json={"device_id": "dev-other", "action_type": "REFRESH_TELEMETRY"},
            headers=vic,
        ).status_code
        == 403
    )
    assert c.post(f"/api/v1/remediations/{low.json()['id']}/approve", headers=vic).status_code == 403
    assert (
        c.put("/api/v1/remediation-policy", json={"four_eyes_min_risk": "LOW"}, headers=ops).status_code
        == 403
    )
    # four-eyes for MEDIUM (policy): the requester cannot approve their own request; someone else can
    assert (
        c.put("/api/v1/remediation-policy", json={"four_eyes_min_risk": "MEDIUM"}, headers=admin).status_code
        == 200
    )
    req = c.post("/api/v1/remediations", json={"device_id": "dev-other", **restart}, headers=ops)
    assert req.status_code == 201 and req.json()["requested_by"] == "ops"
    rid = req.json()["id"]
    self_approve = c.post(f"/api/v1/remediations/{rid}/approve", headers=ops)
    assert self_approve.status_code == 403 and "four-eyes" in self_approve.json()["detail"]["message"]
    assert c.get(f"/api/v1/remediations/{rid}", headers=ops).json()["allowed"]["approve"] is False
    ok = c.post(f"/api/v1/remediations/{rid}/approve", headers=ops2)
    assert ok.status_code == 200 and ok.json()["approved_by"] == "ops2"
    assert c.get("/api/v1/remediation-admin/audit/verify", headers=ops).status_code == 403
    assert c.get("/api/v1/remediation-admin/audit/verify", headers=admin).json()["ok"] is True
    assert c.get("/api/v1/remediations").status_code == 401


def test_refresh_is_only_proposed_for_a_stalled_pipeline(client: TestClient) -> None:
    c = client
    agent = Agent(c)
    agent.telemetry(30.0)
    svc = svc_of(c)
    assert svc.telemetry_stale(DEVICE) is False  # telemetry flowing: no reason to act
    p = svc._presence.get(DEVICE)
    p.last_batch_at = datetime.now(UTC) - timedelta(minutes=10)  # heartbeats continue, batches stopped
    assert svc.telemetry_stale(DEVICE) is True
    p.last_heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)  # agent gone: nothing to refresh
    assert svc.telemetry_stale(DEVICE) is False
