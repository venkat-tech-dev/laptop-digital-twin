from datetime import UTC, datetime, timedelta

import pytest

from app.domain.devices.models import DeviceStatus
from app.domain.events.events import AnomalyDetected, DeviceOffline, SensorUnavailable, TelemetryReceived
from app.schemas.ingest import InventoryEnvelopeIn, TelemetryBatchIn
from app.services.digital_twin import DigitalTwinService, UnknownDeviceError
from tests.conftest import DEVICE, batch, inventory_envelope, sample, settings, typical_samples


@pytest.fixture
def twin() -> DigitalTwinService:
    svc = DigitalTwinService(settings())
    svc.apply_inventory(InventoryEnvelopeIn.model_validate(inventory_envelope()))
    return svc


def update(svc: DigitalTwinService, samples: list, seq: int = 1, now: datetime | None = None):  # type: ignore[no-untyped-def]
    return svc.update(TelemetryBatchIn.model_validate(batch(samples, seq)), now=now)


def test_topology_built_from_inventory(twin: DigitalTwinService) -> None:
    t = twin.get()
    assert t is not None
    ids = set(t.components)
    assert {
        "laptop",
        "chassis",
        "display",
        "motherboard",
        "cpu",
        "memory",
        "vrm",
        "storage",
        "battery",
        "cooling",
        "fan",
        "thermal_sensors",
        "network",
        "power",
        "os",
    } <= ids
    assert "gpu:0x00000000_0x0000F10E" in ids and "disk:PhysicalDrive0" in ids and "nic:Wi-Fi" in ids
    assert t.components["cpu"].parent_id == "motherboard"
    assert t.components["fan"].parent_id == "cooling"


def test_unknown_device_rejected() -> None:
    svc = DigitalTwinService(settings())
    with pytest.raises(UnknownDeviceError):
        update(svc, typical_samples())


def test_telemetry_routed_to_physical_components(twin: DigitalTwinService) -> None:
    result = update(twin, typical_samples())
    t = twin.get()
    assert t is not None
    assert t.components["cpu"].value("cpu.usage_percent") == 35.0
    assert t.components["gpu:0x00000000_0x0000F10E"].value("gpu.usage_percent") == 12.0
    assert t.components["disk:PhysicalDrive0"].reading("disk.health_status").value == "Healthy"  # type: ignore[union-attr]
    assert t.components["nic:Wi-Fi"].current_state == "up"
    assert t.components["thermal_sensors"].current_state == "normal"
    assert t.components["battery"].current_state == "discharging"
    assert t.components["fan"].current_state == "unobservable"
    assert t.device.status is DeviceStatus.LIVE
    assert isinstance(result.events[0], TelemetryReceived)


def test_unavailable_sensor_kept_as_unavailable_not_guessed(twin: DigitalTwinService) -> None:
    update(twin, typical_samples())
    t = twin.get()
    assert t is not None
    r = t.components["cpu"].reading("cpu.temperature_c")
    assert r is not None and r.value is None and not r.available and r.reason == "needs LHM"
    assert t.components["cpu"].availability.value == "partial"


def test_older_sample_does_not_overwrite_live_state(twin: DigitalTwinService) -> None:
    now = datetime.now(UTC)
    update(twin, [sample("cpu.usage_percent", 50.0, ts=now)])
    update(twin, [sample("cpu.usage_percent", 10.0, ts=now - timedelta(seconds=30))], seq=2)
    t = twin.get()
    assert t is not None and t.components["cpu"].value("cpu.usage_percent") == 50.0


def test_sensor_unavailable_transition_emits_event(twin: DigitalTwinService) -> None:
    now = datetime.now(UTC)
    update(twin, [sample("gpu.usage_percent", 5.0, component="gpu", ts=now)])
    res = update(
        twin,
        [
            sample(
                "gpu.usage_percent",
                None,
                component="gpu",
                ts=now + timedelta(seconds=1),
                available=False,
                reason="driver reset",
            )
        ],
        seq=2,
    )
    ev = [e for e in res.events if isinstance(e, SensorUnavailable)]
    assert ev and ev[0].available is False and ev[0].reason == "driver reset"


def test_failure_placeholder_superseded_by_labelled_series(twin: DigitalTwinService) -> None:
    now = datetime.now(UTC)
    failed = sample(
        "thermal.zone_temperature_c",
        None,
        "celsius",
        component="thermal",
        ts=now,
        available=False,
        reason="timed out",
    )
    failed["quality"] = "ERROR"
    update(twin, [failed])
    update(
        twin,
        [
            sample(
                "thermal.zone_temperature_c",
                60.0,
                "celsius",
                component="thermal",
                labels={"zone": "_TZ.THM0"},
                ts=now + timedelta(seconds=1),
            )
        ],
        seq=2,
    )
    t = twin.get()
    assert t is not None
    assert "thermal.zone_temperature_c" not in t.components["thermal_sensors"].telemetry


def test_liveness_degraded_stale_offline(twin: DigitalTwinService) -> None:
    now = datetime.now(UTC)
    update(twin, typical_samples(now), now=now)
    assert twin.check_liveness(now + timedelta(seconds=1)) == []
    twin.check_liveness(now + timedelta(seconds=5))
    assert twin.get().device.status is DeviceStatus.DEGRADED  # type: ignore[union-attr]
    twin.check_liveness(now + timedelta(seconds=15))
    assert twin.get().device.status is DeviceStatus.STALE  # type: ignore[union-attr]
    events = twin.check_liveness(now + timedelta(seconds=40))
    assert any(isinstance(e, DeviceOffline) for e in events)


def test_snapshot_marks_old_readings_stale(twin: DigitalTwinService) -> None:
    now = datetime.now(UTC)
    update(twin, typical_samples(now), now=now)
    snap = twin.snapshot(now=now + timedelta(seconds=20))
    assert snap is not None
    cpu = next(c for c in snap["components"] if c["component_id"] == "cpu")
    assert cpu["telemetry"]["cpu.usage_percent"]["quality"] == "STALE"
    assert snap["data_source"] == "LOCAL WINDOWS HARDWARE" and snap["mode"] == "live"


def test_anomaly_detected_event_for_disk_space(twin: DigitalTwinService) -> None:
    res = update(twin, [sample("disk.usage_percent", 96.0, component="storage", labels={"volume": "C:"})])
    titles = [e.anomaly["rule_id"] for e in res.events if isinstance(e, AnomalyDetected)]
    assert "disk_space_low" in titles and "disk_space_critical" in titles


def test_inventory_refresh_keeps_live_telemetry(twin: DigitalTwinService) -> None:
    update(twin, typical_samples())
    twin.apply_inventory(InventoryEnvelopeIn.model_validate(inventory_envelope()))
    t = twin.get(DEVICE)
    assert t is not None and t.components["cpu"].value("cpu.usage_percent") == 35.0
