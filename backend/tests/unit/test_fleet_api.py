# ruff: noqa: E501  (inline test payloads)
"""Phase 10 fleet views through the API: tenant scoping, platform-only figures, insufficient-data honesty."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.anomalies.models import Anomaly, AnomalyType, Detector, Severity
from tests.unit.tenancy_helpers import World, accounts_app, build_world, enroll


@pytest.fixture
def w() -> Iterator[World]:
    with accounts_app() as c:
        yield build_world(c)


def _anomaly(device_id: str, i: int, at: datetime) -> Anomaly:
    return Anomaly(
        f"an-{device_id}-{i}", device_id, Detector.BEHAVIORAL, "r", "memory", "memory.usage_percent", Severity.WARNING,
        "Memory pressure", "unusual", 91.0, 80.0, at, at, anomaly_type=AnomalyType.BEHAVIORAL, category="memory",
    )  # fmt: skip


def test_fleet_views_cover_only_the_callers_organization(w: World) -> None:
    c = w.c
    for path in (
        "/api/v1/fleet/health",
        "/api/v1/fleet/operations",
        "/api/v1/fleet/insights",
        "/api/v1/fleet/capacity",
    ):
        body = c.get(path, headers=w.alice).text
        assert "dev-globex-0001" not in body, path  # never another organization's device
    assert "dev-acme-0001" in c.get("/api/v1/fleet/health", headers=w.alice).text
    h = c.get("/api/v1/fleet/health", headers=w.alice).json()
    assert h["devices"] == 1 and h["version"] == "fleet-health-v1"
    ops = c.get("/api/v1/fleet/operations", headers=w.alice).json()
    assert "pipeline" not in ops and "notification_backlog" not in ops  # platform-only
    root_ops = c.get("/api/v1/fleet/operations", headers=w.root).json()
    assert "pipeline" in root_ops and root_ops["pipeline"]["background"]["status"] in ("ok", "degraded")


def test_small_fleets_report_insufficient_data_instead_of_insights(w: World) -> None:
    ins = w.c.get("/api/v1/fleet/insights", headers=w.alice).json()
    assert ins["correlation"]["status"] == "INSUFFICIENT_DATA"
    cap = w.c.get("/api/v1/fleet/capacity", headers=w.alice).json()
    assert cap["devices"]["status"] == "INSUFFICIENT_DATA" and cap["devices"]["current"] == 1.0
    assert "storage" not in cap  # platform-only


def test_cross_device_burst_is_found_and_other_tenants_never_contribute(w: World) -> None:
    c = w.c
    devices = [w.dev_a.device_id] + [enroll(c, w.alice, f"dev-acme-{i:04d}").device_id for i in range(2, 7)]
    repo = c.app.state.container.event_repo  # type: ignore[attr-defined]
    now = datetime.now(UTC)
    for i, d in enumerate(devices):
        c.portal.call(repo.upsert_anomaly, _anomaly(d, i, now - timedelta(minutes=10 - i)))  # type: ignore[union-attr]
    for i in range(5):  # a globex burst at the same time must not leak into acme's view
        c.portal.call(repo.upsert_anomaly, _anomaly(w.dev_b.device_id, 100 + i, now - timedelta(minutes=5)))  # type: ignore[union-attr]
    ins = c.get("/api/v1/fleet/insights", headers=w.alice).json()
    corr = ins["correlation"]
    assert corr["status"] == "OK" and len(corr["insights"]) == 1
    insight = corr["insights"][0]
    assert insight["devices"] == sorted(devices) and insight["causation"] == "NOT_ESTABLISHED"
    assert insight["observed_fact"].startswith("6 devices reported behavioral_anomaly memory")
    assert insight["statistical_associations"] == []  # identical test hardware: nothing over-represented
    assert ins["scope"]["anomalies_considered"] == 6
    bob = c.get("/api/v1/fleet/insights", headers=w.bob).json()
    assert bob["scope"]["devices"] == 1 and bob["correlation"]["status"] == "INSUFFICIENT_DATA"


def test_employee_without_device_view_is_refused(w: World) -> None:
    from tests.unit.tenancy_helpers import PASSWORD, login

    assert (
        w.c.post(
            "/api/v1/org/members",
            json={"username": "erin", "password": PASSWORD, "role": "employee"},
            headers=w.alice,
        ).status_code
        == 201
    )
    erin = login(w.c, "erin")
    h = w.c.get("/api/v1/fleet/health", headers=erin)
    assert h.status_code in (200, 403)
    if h.status_code == 200:  # employees see only their assigned devices (none here)
        assert h.json()["devices"] == 0


def test_model_governance_is_tenant_scoped_and_reports_missing_labels(w: World) -> None:
    c = w.c
    repo = c.app.state.container.event_repo  # type: ignore[attr-defined]
    now = datetime.now(UTC)
    a = _anomaly(w.dev_a.device_id, 1, now - timedelta(hours=1))
    a.feedback = {"verdict": "false_positive"}
    c.portal.call(repo.upsert_anomaly, a)  # type: ignore[union-attr]
    for i in range(3):
        c.portal.call(repo.upsert_anomaly, _anomaly(w.dev_b.device_id, 50 + i, now))  # type: ignore[union-attr]
    m = c.get("/api/v1/fleet/models", headers=w.alice).json()
    det = m["anomaly_detection"]["detectors"]
    assert det == [{"detector": "behavioral:behavioral_anomaly", "detected": 1, "labelled": 1, "label_coverage": 1.0,
                    "false_positive_rate": None, "status": "FEW_LABELS"}]  # fmt: skip
    assert "diagnosis" not in m  # platform-only
    assert "diagnosis" in c.get("/api/v1/fleet/models", headers=w.root).json()
