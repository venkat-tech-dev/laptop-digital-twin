"""Accounts, workspaces, agent configuration, acknowledgements, insights, diagnostics, uploads, sync."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.domain.admin.models import hash_password, verify_password
from app.domain.anomalies.models import Anomaly, Detector, Severity
from app.main import create_app
from app.services.insights import correlate, detection_confidence, pearson
from tests.conftest import AGENT_KEY, batch, inventory_envelope, settings, typical_samples

H = {"X-Agent-Key": AGENT_KEY}
SECRET = "x" * 40
PASSWORD = "Correct-Horse-9"


def _feed(client: TestClient, n: int = 3, **kw: Any) -> None:
    assert client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=H).status_code == 202
    now = datetime.now(UTC)
    for i in range(n):
        r = client.post(
            "/api/v1/ingest/telemetry",
            json=batch(typical_samples(now - timedelta(seconds=n - i), **kw), seq=i + 1),
            headers=H,
        )
        assert r.status_code == 202


@pytest.fixture
def accounts_client() -> Iterator[TestClient]:
    app = create_app(settings(AUTH_MODE="accounts", JWT_SECRET=SECRET))
    with TestClient(app) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------------------------- passwords
def test_password_hash_roundtrip() -> None:
    encoded = hash_password(PASSWORD)
    assert encoded.startswith("scrypt$") and PASSWORD not in encoded
    assert verify_password(PASSWORD, encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password(PASSWORD, "garbage")


# ------------------------------------------------------------------------------------- accounts
def test_first_run_setup_login_and_roles(accounts_client: TestClient) -> None:
    c = accounts_client
    cfg = c.get("/api/v1/auth/config").json()
    assert cfg["mode"] == "accounts" and cfg["setup_required"] is True
    assert c.get("/api/v1/twin").status_code == 401

    weak = c.post("/api/v1/auth/setup", json={"username": "admin", "password": "short"})
    assert weak.status_code == 422
    r = c.post("/api/v1/auth/setup", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200
    admin = _bearer(r.json()["access_token"])
    assert c.get("/api/v1/auth/config").json()["setup_required"] is False
    # setup can only run once
    assert c.post("/api/v1/auth/setup", json={"username": "second", "password": PASSWORD}).status_code == 409

    me = c.get("/api/v1/auth/me", headers=admin).json()
    assert me["role"] == "admin" and me["can_admin"] is True

    created = c.post(
        "/api/v1/users", json={"username": "viewer1", "password": PASSWORD, "role": "viewer"}, headers=admin
    )
    assert created.status_code == 201
    assert c.post("/api/v1/auth/login", json={"username": "viewer1", "password": "nope"}).status_code == 401
    viewer = _bearer(
        c.post("/api/v1/auth/login", json={"username": "viewer1", "password": PASSWORD}).json()[
            "access_token"
        ]
    )
    assert c.get("/api/v1/auth/me", headers=viewer).json()["can_operate"] is False
    assert c.get("/api/v1/users", headers=viewer).status_code == 403
    assert (
        c.put("/api/v1/settings/agent", json={"telemetry_interval_ms": 2000}, headers=viewer).status_code
        == 403
    )

    # disabling an account invalidates its token immediately
    uid = created.json()["user_id"]
    assert c.patch(f"/api/v1/users/{uid}", json={"disabled": True}, headers=admin).status_code == 200
    assert c.get("/api/v1/auth/me", headers=viewer).status_code == 401

    # the last administrator cannot be removed or demoted
    admin_id = next(
        u["user_id"] for u in c.get("/api/v1/users", headers=admin).json() if u["username"] == "admin"
    )
    assert c.patch(f"/api/v1/users/{admin_id}", json={"role": "viewer"}, headers=admin).status_code == 409
    assert c.delete(f"/api/v1/users/{admin_id}", headers=admin).status_code == 409


def test_accounts_mode_requires_strong_jwt_secret() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET"):
        settings(AUTH_MODE="accounts", JWT_SECRET="short")


# ------------------------------------------------------------------------------------- workspaces
def test_default_workspace_holds_the_device(client: TestClient) -> None:
    _feed(client)
    spaces = client.get("/api/v1/workspaces").json()
    assert len(spaces) == 1 and spaces[0]["name"] == "Local"
    assert spaces[0]["devices"][0]["name"].startswith("LENOVO")
    lab = client.post(
        "/api/v1/workspaces", json={"name": "Lab", "device_ids": [spaces[0]["device_ids"][0]]}
    ).json()
    spaces = {w["name"]: w for w in client.get("/api/v1/workspaces").json()}
    assert (
        spaces["Lab"]["device_ids"] and not spaces["Local"]["device_ids"]
    )  # a device lives in one workspace
    assert client.post("/api/v1/workspaces", json={"name": "lab"}).status_code == 409
    assert client.delete(f"/api/v1/workspaces/{lab['workspace_id']}").status_code == 204
    assert client.get("/api/v1/workspaces").json()[0]["device_ids"]  # device moved back


# ------------------------------------------------------------------------------------- agent configuration
def test_agent_config_roundtrip(client: TestClient) -> None:
    assert client.get("/api/v1/agent/config").status_code == 401  # agent key required
    first = client.get("/api/v1/agent/config", headers=H).json()
    assert first["configured"] is False
    r = client.put(
        "/api/v1/settings/agent", json={"telemetry_interval_ms": 2000, "collect_process_details": True}
    )
    assert r.status_code == 200 and r.json()["requested"]["version"] == 1
    cfg = client.get("/api/v1/agent/config", headers=H).json()
    assert (
        cfg["configured"] and cfg["telemetry_interval_ms"] == 2000 and cfg["collect_process_details"] is True
    )
    assert client.put("/api/v1/settings/agent", json={"telemetry_interval_ms": 10}).status_code == 422


# ------------------------------------------------------------------------------------- anomalies
def test_acknowledgement_persists_and_analysis(client: TestClient) -> None:
    _feed(client, n=90, mem=97.0)  # >= 60 s above the 92 % memory-pressure threshold
    active = client.get("/api/v1/anomalies", params={"status": "active"}).json()
    assert active, "expected an active memory anomaly"
    a = active[0]
    assert a["acknowledgement"] is None and 0.5 <= a["confidence"]["value"] <= 0.99
    r = client.post(f"/api/v1/anomalies/{a['anomaly_id']}/acknowledge", json={"note": "known: big build"})
    assert r.status_code == 200 and r.json()["acknowledgement"]["note"] == "known: big build"
    again = client.get("/api/v1/anomalies", params={"status": "active"}).json()[0]
    assert again["acknowledgement"]["acknowledged_by"] == "local"
    analysis = client.get(f"/api/v1/anomalies/{a['anomaly_id']}/analysis").json()
    assert {"confidence", "correlated_signals", "process_attribution", "summary"} <= set(analysis)
    assert client.delete(f"/api/v1/anomalies/{a['anomaly_id']}/acknowledge").json()["acknowledgement"] is None
    assert client.get("/api/v1/anomalies/does-not-exist/analysis").status_code == 404


def _anomaly(**kw: Any) -> Anomaly:
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "anomaly_id": "a",
        "device_id": "d",
        "detector": Detector.RULE,
        "rule_id": "r",
        "component_id": "memory",
        "metric_key": "memory.usage_percent",
        "severity": Severity.WARNING,
        "title": "Memory pressure",
        "message": "",
        "value": 97.0,
        "threshold": 92.0,
        "started_at": now - timedelta(seconds=120),
        "last_seen_at": now,
        "context": {"duration_s": 60},
    }
    base.update(kw)
    return Anomaly(**base)


def test_confidence_grows_with_margin_and_persistence() -> None:
    weak = detection_confidence(_anomaly(value=92.5, started_at=datetime.now(UTC) - timedelta(seconds=60)))
    strong = detection_confidence(_anomaly(value=99.0))
    assert 0.5 <= weak["value"] < strong["value"] <= 0.99
    stat = detection_confidence(_anomaly(detector=Detector.STATISTICAL, context={"zscore": 6.5}))
    assert stat["value"] == 0.95


def test_pearson_and_correlate() -> None:
    assert pearson([1, 2, 3, 4, 5, 6], [2, 4, 6, 8, 10, 12]) == pytest.approx(1.0)
    assert pearson([1, 1, 1, 1, 1, 1], [1, 2, 3, 4, 5, 6]) is None
    t0 = datetime.now(UTC)
    ts = [t0 + timedelta(seconds=10 * i) for i in range(10)]
    target = {t: float(i) for i, t in enumerate(ts)}
    ranked = correlate(target, {"cpu.usage_percent": {t: float(-i) for i, t in enumerate(ts)}, "x": {}})
    assert ranked[0]["metric_key"] == "cpu.usage_percent" and ranked[0]["r"] == pytest.approx(-1.0)


# ------------------------------------------------------------------------------------- history, settings
def test_history_accepts_start_and_end(client: TestClient) -> None:
    _feed(client)
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    r = client.get(
        "/api/v1/telemetry/history",
        params={"keys": "cpu.usage_percent", "start": start.isoformat(), "end": end.isoformat()},
    )
    assert r.status_code == 200 and r.json()["bucket_seconds"] == 10
    bad = client.get(
        "/api/v1/telemetry/history",
        params={"keys": "cpu.usage_percent", "start": end.isoformat(), "end": start.isoformat()},
    )
    assert bad.status_code == 422
    perf = client.get("/api/v1/analytics/performance", params={"minutes": 60, "end": start.isoformat()})
    assert perf.status_code == 200 and perf.json()["window_end"].startswith(start.isoformat()[:16])


def test_system_info_exposes_retention(client: TestClient) -> None:
    info = client.get("/api/v1/system/info").json()
    assert info["retention_days"] == 30 and info["persist_sample_interval_s"] == 5.0
    assert info["retention_mechanism"].startswith("in-memory")


# ------------------------------------------------------------------------------------- diagnostics
def test_diagnostics_bundle_redacts_secrets(client: TestClient) -> None:
    _feed(client)
    r = client.get("/api/v1/system/diagnostics", params={"anonymize": True})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert {"README.txt", "backend.json", "device.json", "components.json", "anomalies.json"} <= names
    blob = b"".join(zf.read(n) for n in names)
    assert AGENT_KEY.encode() not in blob
    assert b"ldt-" not in zf.read("device.json") or b"device-" in zf.read("device.json")
    assert b"SECRET-SERIAL" not in blob


# ------------------------------------------------------------------------------------- uploads
def test_upload_photo_and_mesh(tmp_path: Path) -> None:
    app = create_app(settings(MODELS_DIR=str(tmp_path)))
    with TestClient(app) as c:
        _feed(c)
        assert c.put("/api/v1/models/photo", content=b"not an image").status_code == 422
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        geo = c.put("/api/v1/models/photo", content=png, params={"attribution": "own photo"}).json()
        assert geo["photo_url"].startswith("/models/uploads/") and geo["photo_attribution"] == "own photo"
        glb = b"glTF" + b"\x02\x00\x00\x00" + b"0" * 64
        geo = c.put("/api/v1/models/mesh", content=glb, params={"exact": False}).json()
        assert geo["kind"] == "matched" and geo["url"].startswith("/models/uploads/")
        assert (tmp_path / "manifest.json").is_file()
        geo = c.delete("/api/v1/models/mesh").json()
        assert geo["kind"] == "profile" and geo["photo_url"]  # photo kept, mesh removed
        assert c.get("/api/v1/device").json()["geometry"]["photo_url"]


# ------------------------------------------------------------------------------------- sync
def test_sync_configuration_validation(client: TestClient) -> None:
    status = client.get("/api/v1/settings/sync").json()
    assert status["enabled"] is False and status["active"] is False and status["key_configured"] is False
    assert client.put("/api/v1/settings/sync", json={"enabled": True}).status_code == 422
    assert client.put("/api/v1/settings/sync", json={"target_url": "http://example.com"}).status_code == 422
    ok = client.put(
        "/api/v1/settings/sync", json={"target_url": "https://hub.example.com", "enabled": True}
    ).json()
    assert ok["enabled"] is True and ok["active"] is False  # no SYNC_TARGET_KEY in the environment


# ------------------------------------------------------------------------------------- simulation environment
def test_simulation_environment_and_intervals(client: TestClient) -> None:
    _feed(client)
    base = {"scenario": "cpu_intensive", "duration_minutes": 10}
    cool = client.post(
        "/api/v1/simulation/run", json={**base, "ambient_c": 20, "thermal_profile": "performance"}
    ).json()
    hot = client.post(
        "/api/v1/simulation/run", json={**base, "ambient_c": 35, "thermal_profile": "quiet"}
    ).json()
    assert hot["predicted"]["steady_state_temperature_c"] > cool["predicted"]["steady_state_temperature_c"]
    p = hot["predicted"]
    assert p["temperature_low_c"] < p["temperature_c"] < p["temperature_high_c"]
    assert p["fan_duty_percent_est"] is not None and p["thermal_profile"] == "quiet"
    assert p["battery_runtime_h_low"] < p["battery_runtime_h"] < p["battery_runtime_h_high"]
    traj = hot["trajectory"]
    assert traj[0]["temperature_low_c"] == traj[0]["temperature_c"]  # interval grows from zero
    assert client.post("/api/v1/simulation/run", json={**base, "ambient_c": 80}).status_code == 422
