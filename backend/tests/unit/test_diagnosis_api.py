# ruff: noqa: E501  (inline test payloads)
"""Phase 7 - integration (telemetry + anomaly -> alert -> automatic diagnosis -> API / twin / timeline /
WebSocket), caching and versioning, model failure modes, back-pressure, feedback and authorization."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.diagnosis import DiagnosisService
from app.services.diagnosis_providers import MockProvider
from tests.conftest import AGENT_KEY, DEVICE, INVENTORY, sample, settings
from tests.unit.test_alerting_api import PASSWORD, anomaly

KEY = {"X-Agent-Key": AGENT_KEY}


def _inventory(c: TestClient, device: str = DEVICE) -> None:
    env = {"device_id": device, "agent_version": "t", "discovered_at": datetime.now(UTC).isoformat(),
           "inventory": INVENTORY}  # fmt: skip
    assert c.post("/api/v1/ingest/inventory", json=env, headers=KEY).status_code in (200, 202)


def _procs(ts: datetime, hot: bool) -> dict[str, Any]:
    def p(pid: int, name: str, cpu: float, mem: int) -> dict[str, Any]:
        return {"pid": pid, "name": name, "status": "running", "cpu_percent": cpu, "memory_rss_bytes": mem,
                "memory_percent": None, "num_threads": 4, "io_read_bytes_per_sec": None,
                "io_write_bytes_per_sec": None, "path": "C:\\Users\\alice\\secret\\builder.exe",
                "user": "CORP\\alice"}  # fmt: skip

    return {"timestamp": ts.isoformat(), "source": "test", "total_processes": 2,
            "processes": [p(10, "builder.exe", 70.0 if hot else 1.0, 400 * 2**20),
                          p(11, "chrome.exe", 5.0, 1500 * 2**20)]}  # fmt: skip


def feed(c: TestClient, device: str = DEVICE, minutes: int = 12, mem: float = 60.0, seq0: int = 0) -> None:
    """CPU 20 % then 90 % from minute 4 on (builder.exe), one batch every 30 s, timestamps ascending."""
    start = datetime.now(UTC) - timedelta(minutes=minutes)
    for i in range(minutes * 2):
        ts = start + timedelta(seconds=30 * i)
        hot = i >= 8
        samples = [
            sample("cpu.usage_percent", 90.0 if hot else 20.0, ts=ts),
            sample("memory.usage_percent", mem, component="memory", ts=ts),
        ]
        body = {"device_id": device, "agent_version": "t", "sequence": seq0 + i + 1, "sent_at": ts.isoformat(),
                "samples": samples, "processes": _procs(ts, hot)}  # fmt: skip
        r = c.post("/api/v1/ingest/telemetry", json=body, headers=KEY)
        assert r.status_code in (200, 202), r.text


def svc_of(c: TestClient) -> DiagnosisService:
    svc = c.app.state.container.diagnosis  # type: ignore[attr-defined]
    assert svc is not None
    return svc


def settle(c: TestClient, timeout: float = 10.0) -> None:
    """Let the background alert + diagnosis workers finish."""
    container = c.app.state.container  # type: ignore[attr-defined]
    svc = svc_of(c)
    for _ in range(int(timeout / 0.05)):
        c.portal.call(container.alerts.drain)  # type: ignore[union-attr]
        busy = svc._queue.qsize() or svc._active or svc._pending
        if not busy:
            return
        c.portal.call(asyncio.sleep, 0.05)  # type: ignore[union-attr,arg-type]
    raise AssertionError("diagnosis did not settle")


def publish(c: TestClient, *events: Any) -> None:
    c.portal.call(c.app.state.container.bus.publish_all, list(events))  # type: ignore[attr-defined,union-attr]
    settle(c)


def cpu_anomaly(device: str = DEVICE, aid: str = "an-cpu") -> Any:
    return anomaly(device=device, aid=aid, level="HIGH")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(settings())) as c:
        _inventory(c)
        feed(c)
        yield c


def _alert_id(c: TestClient, aid: str = "an-cpu") -> str:
    items = c.get("/api/v1/alerts", params={"device_id": DEVICE}).json()["items"]
    return next(a["alert_id"] for a in items if (a.get("metadata") or {}).get("evidence", {}).get("anomaly_id") == aid
                or aid in json.dumps(a))  # fmt: skip


def wait_job(c: TestClient, job_id: str) -> dict[str, Any]:
    settle(c)
    return c.get(f"/api/v1/diagnosis-jobs/{job_id}").json()


# ------------------------------------------------------------------ integration
def test_high_alert_is_diagnosed_automatically(client: TestClient) -> None:
    c = client
    with c.websocket_connect("/ws/twin") as ws:
        ws.send_json({"type": "subscribe", "topics": [f"device:{DEVICE}"]})
        publish(c, cpu_anomaly())
        events = []
        for _ in range(200):
            m = ws.receive_json()
            events.append(m["event"])
            if m["event"] == "diagnosis.available":
                break
    assert "diagnosis.started" in events and "diagnosis.available" in events
    items = c.get(f"/api/v1/devices/{DEVICE}/diagnoses").json()["items"]
    assert len(items) == 1
    d = c.get(f"/api/v1/diagnoses/{items[0]['diagnosis_id']}").json()
    assert d["status"] in ("AVAILABLE", "LOW_CONFIDENCE")
    assert d["diagnosis_type"] == "CPU_PRESSURE"
    assert "builder.exe" in (d["likely_cause"] or "")
    assert d["reasoning_model"] == "rules"
    assert "AI reasoning unavailable. Showing deterministic evidence." in d["notices"]
    ids = {e["evidence_id"] for e in d["evidence"]}
    assert set(d["hypotheses"][0]["supporting"]) <= ids
    assert d["versions"][0]["version"] == 1 and d["alert_id"]
    # twin: a separate DIAGNOSED section; timeline entry
    flat = c.get(f"/api/v1/devices/{DEVICE}/twin", params={"format": "flat"}).json()["state"]
    assert flat["diagnoses.active_count"] == 1
    assert flat["diagnoses.latest"]["diagnosis_id"] == d["diagnosis_id"]
    tl = c.get(f"/api/v1/devices/{DEVICE}/timeline").json()["items"]
    assert any(e["type"] == "diagnosis.available" for e in tl)


def test_manual_request_cache_and_versioning(client: TestClient) -> None:
    c = client
    svc_of(c)._s.diagnosis_auto_min_severity  # noqa: B018 - auto on HIGH; first version comes from it
    publish(c, cpu_anomaly())
    alert_id = c.get(f"/api/v1/devices/{DEVICE}/diagnoses").json()["items"][0]["alert_id"]
    r = c.post(f"/api/v1/alerts/{alert_id}/diagnose")
    assert r.status_code == 202
    job = wait_job(c, r.json()["job"]["job_id"])
    assert job["status"] == "CACHED"  # unchanged facts -> same diagnosis, no recomputation
    first = job["diagnosis_id"]
    r = c.post(f"/api/v1/alerts/{alert_id}/diagnose", json={"force": True})
    job = wait_job(c, r.json()["job"]["job_id"])
    assert job["status"] == "DONE" and job["diagnosis_id"] != first
    d2 = c.get(f"/api/v1/diagnoses/{job['diagnosis_id']}").json()
    assert d2["version"] == 2 and d2["supersedes"] == first
    assert [v["status"] for v in d2["versions"]] == ["SUPERSEDED", d2["status"]]
    # history keeps both; the default (current) list shows only the new version
    assert len(c.get(f"/api/v1/devices/{DEVICE}/diagnoses", params={"current": False}).json()["items"]) == 2
    assert len(c.get(f"/api/v1/devices/{DEVICE}/diagnoses").json()["items"]) == 1
    by_alert = c.get("/api/v1/diagnoses", params={"alert_id": alert_id}).json()["items"]
    assert by_alert[0]["diagnosis_id"] == job["diagnosis_id"]


def test_anomaly_trigger_and_unknown_ids(client: TestClient) -> None:
    c = client
    assert c.post("/api/v1/anomalies/nope/diagnose").status_code == 404
    assert c.post("/api/v1/predictions/nope/diagnose").status_code == 404
    assert c.post("/api/v1/alerts/nope/diagnose").status_code == 404
    assert c.get("/api/v1/diagnoses/nope").status_code == 404
    assert c.get("/api/v1/diagnosis-jobs/nope").status_code == 404


# ------------------------------------------------------------------ local model paths
GOOD = {"summary": "CPU has been high since builder.exe became busy.", "ranking": ["cpu.process"],
        "claims": [{"text": "CPU has stayed above its usual range", "evidence_ids": ["E1"]}],
        "investigate": [{"text": "Check whether the build is expected right now", "evidence_ids": []},
                        {"text": "Use taskkill to end builder.exe", "evidence_ids": []}]}  # fmt: skip


def _with_provider(c: TestClient, provider: MockProvider) -> DiagnosisService:
    svc = svc_of(c)
    svc.provider = provider
    return svc


def test_model_output_is_validated_and_attributed(client: TestClient) -> None:
    c = client
    mock = MockProvider(GOOD)
    _with_provider(c, mock)
    publish(c, cpu_anomaly())
    d = c.get(
        f"/api/v1/diagnoses/{c.get(f'/api/v1/devices/{DEVICE}/diagnoses').json()['items'][0]['diagnosis_id']}"
    ).json()
    assert d["reasoning_model"] == "mock:mock" and d["prompt_version"]
    assert any(r["reason"] == "FORBIDDEN_ACTION" for r in d["rejected_claims"])
    assert all("taskkill" not in x for x in d["explanation"]["investigate"])
    assert "Check whether the build is expected right now" in d["explanation"]["investigate"]
    # telemetry reached the model only as data in the user message
    msgs = mock.calls[0]
    assert msgs[0]["role"] == "system" and "builder.exe" not in msgs[0]["content"]
    assert "alice" not in json.dumps(msgs) and "secret" not in json.dumps(msgs).lower()


@pytest.mark.parametrize(
    ("provider", "notice"),
    [
        (MockProvider(delay_s=2.0), "AI reasoning unavailable"),
        (MockProvider(error=__import__("app.services.diagnosis_providers", fromlist=["x"]).ModelUnavailableError("down")),
         "AI reasoning unavailable"),
        (MockProvider("this is not json"), "AI output failed validation"),
    ],
)  # fmt: skip
def test_model_failures_fall_back_to_rules(client: TestClient, provider: MockProvider, notice: str) -> None:
    c = client
    svc = _with_provider(c, provider)
    svc.timeout_s = 0.3
    publish(c, cpu_anomaly())
    item = c.get(f"/api/v1/devices/{DEVICE}/diagnoses").json()["items"][0]
    d = c.get(f"/api/v1/diagnoses/{item['diagnosis_id']}").json()
    assert d["status"] in ("AVAILABLE", "LOW_CONFIDENCE")  # deterministic result still delivered
    assert d["diagnosis_type"] == "CPU_PRESSURE"
    assert any(notice in n for n in d["notices"]), d["notices"]


def test_memory_pressure_on_model_host_defers_ai(client: TestClient) -> None:
    c = client
    mock = MockProvider(GOOD)
    _with_provider(c, mock).memory_gate = 50.0  # the device (memory at 60 %) also hosts the model
    publish(c, cpu_anomaly())
    item = c.get(f"/api/v1/devices/{DEVICE}/diagnoses").json()["items"][0]
    assert item["reasoning_model"] == "rules" and not mock.calls
    assert any("AI reasoning deferred" in n for n in item["notices"])


def test_queue_overflow_is_back_pressure(client: TestClient) -> None:
    c = client
    publish(c, cpu_anomaly())
    alert_id = c.get(f"/api/v1/devices/{DEVICE}/diagnoses").json()["items"][0]["alert_id"]
    svc = svc_of(c)
    full: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
    full.put_nowait(object())
    real, svc._queue = svc._queue, full
    try:
        r = c.post(f"/api/v1/alerts/{alert_id}/diagnose", json={"force": True})
        assert r.status_code == 429 and r.headers["Retry-After"]
    finally:
        svc._queue = real
    assert svc.stats["dropped"] == 1


def test_expiry_marks_and_notifies(client: TestClient) -> None:
    c = client
    publish(c, cpu_anomaly())
    svc = svc_of(c)
    d = next(iter(svc.repo.items.values()))  # type: ignore[attr-defined]
    n = c.portal.call(svc.expire_due, d.expires_at + timedelta(seconds=1))  # type: ignore[union-attr]
    assert n == 1
    assert c.get(f"/api/v1/diagnoses/{d.diagnosis_id}").json()["status"] == "EXPIRED"
    assert c.get(f"/api/v1/devices/{DEVICE}/diagnoses").json()["items"] == []


def test_feedback(client: TestClient) -> None:
    c = client
    publish(c, cpu_anomaly())
    did = c.get(f"/api/v1/devices/{DEVICE}/diagnoses").json()["items"][0]["diagnosis_id"]
    r = c.post(f"/api/v1/diagnoses/{did}/feedback",
               json={"verdict": "PARTIALLY_CORRECT", "actual_cause": "Nightly build\x00<b>", "note": "ok"})  # fmt: skip
    assert r.status_code == 201
    assert r.json()["actual_cause"] == "Nightly build (b)"
    assert c.post(f"/api/v1/diagnoses/{did}/feedback", json={"verdict": "MAYBE"}).status_code == 422
    d = c.get(f"/api/v1/diagnoses/{did}").json()
    assert d["feedback"][0]["verdict"] == "PARTIALLY_CORRECT"
    status = c.get("/api/v1/diagnosis-config/status").json()
    assert status["feedback"]["CPU_PRESSURE"]["PARTIALLY_CORRECT"] == 1
    assert status["provider"]["provider"] == "rules" and status["mode"] == "LOCAL_ONLY"


def test_no_sensitive_data_in_stored_diagnosis(client: TestClient) -> None:
    c = client
    publish(c, cpu_anomaly())
    did = c.get(f"/api/v1/devices/{DEVICE}/diagnoses").json()["items"][0]["diagnosis_id"]
    text = json.dumps(c.get(f"/api/v1/diagnoses/{did}").json())
    for secret in ("alice", "C:\\\\Users", "SECRET-SERIAL", "CORP"):
        assert secret not in text


def test_process_names_hidden_when_policy_forbids() -> None:
    with TestClient(create_app(settings(TWIN_SHOW_PROCESS_NAMES=False))) as c:
        _inventory(c)
        feed(c)
        publish(c, cpu_anomaly())
        did = c.get(f"/api/v1/devices/{DEVICE}/diagnoses").json()["items"][0]["diagnosis_id"]
        text = json.dumps(c.get(f"/api/v1/diagnoses/{did}").json())
        assert "builder.exe" not in text and "process #1" in text


def test_disabled_mode_and_public_endpoint_fall_back_to_rules() -> None:
    for extra in ({"DIAGNOSIS_MODE": "DISABLED", "DIAGNOSIS_MODELS": "qwen2.5:1.5b"},
                  {"DIAGNOSIS_LLM_URL": "https://8.8.8.8", "DIAGNOSIS_MODELS": "qwen2.5:1.5b"}):  # fmt: skip
        with TestClient(create_app(settings(**extra))) as c:
            assert svc_of(c).provider.name == "rules"


# ------------------------------------------------------------------ authorization
@pytest.fixture
def accounts() -> Iterator[TestClient]:
    with TestClient(create_app(settings(AUTH_MODE="accounts", JWT_SECRET="x" * 40))) as c:
        yield c


def test_authorization(accounts: TestClient) -> None:
    c = accounts
    admin = {"Authorization": "Bearer " + c.post("/api/v1/auth/setup", json={
        "username": "admin", "password": PASSWORD}).json()["access_token"]}  # fmt: skip
    for d in ("dev-mine", "dev-other"):
        _inventory(c, d)
        feed(c, device=d, minutes=6)
    for user, role in (("ana", "employee"), ("vic", "viewer"), ("ops", "operator")):
        r = c.post(
            "/api/v1/users", json={"username": user, "password": PASSWORD, "role": role}, headers=admin
        )
        assert r.status_code == 201
    c.put("/api/v1/devices/dev-mine/assignment", json={"username": "ana"}, headers=admin)

    def login(u: str) -> dict[str, str]:
        tok = c.post("/api/v1/auth/login", json={"username": u, "password": PASSWORD}).json()["access_token"]
        return {"Authorization": f"Bearer {tok}"}

    ana, vic, ops = login("ana"), login("vic"), login("ops")
    publish(c, cpu_anomaly("dev-mine", "m1"), cpu_anomaly("dev-other", "o1"))
    mine = c.get("/api/v1/devices/dev-mine/diagnoses", headers=ana).json()["items"]
    assert len(mine) == 1
    assert c.get("/api/v1/devices/dev-other/diagnoses", headers=ana).status_code == 404
    other = c.get("/api/v1/devices/dev-other/diagnoses", headers=admin).json()["items"][0]
    assert c.get(f"/api/v1/diagnoses/{other['diagnosis_id']}", headers=ana).status_code == 404
    assert c.post(f"/api/v1/diagnoses/{other['diagnosis_id']}/feedback", json={"verdict": "CORRECT"},
                  headers=ana).status_code == 404  # fmt: skip
    assert c.post(f"/api/v1/alerts/{other['alert_id']}/diagnose", headers=ana).status_code == 404
    # owner may request for their own device (not bypass the cache); viewer is read-only; operator may force
    assert c.post(f"/api/v1/alerts/{mine[0]['alert_id']}/diagnose", headers=ana).status_code == 202
    assert c.post(f"/api/v1/alerts/{mine[0]['alert_id']}/diagnose", json={"force": True},
                  headers=ana).status_code == 403  # fmt: skip
    assert c.get("/api/v1/devices/dev-other/diagnoses", headers=vic).status_code == 200
    assert c.post(f"/api/v1/alerts/{other['alert_id']}/diagnose", headers=vic).status_code == 403
    assert c.post(f"/api/v1/alerts/{other['alert_id']}/diagnose", json={"force": True},
                  headers=ops).status_code == 202  # fmt: skip
    assert c.get("/api/v1/diagnosis-config/status", headers=ana).status_code == 403
    assert c.get("/api/v1/diagnosis-config/status").status_code == 401
    settle(c)
