from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

AGENT_KEY = "test-agent-key-0123456789abcdef"
DEVICE = "ldt-test-device"

INVENTORY: dict[str, Any] = {
    "manufacturer": "LENOVO",
    "model": "ThinkPad L14 Gen 4",
    "model_number": "21H1S0PM00",
    "os": {"name": "Microsoft Windows 11 Pro", "version": "10.0.26200"},
    "cpu": {
        "model": "13th Gen Intel(R) Core(TM) i5-1335U",
        "cores": 10,
        "threads": 12,
        "base_clock_mhz": 1300,
    },
    "memory": {"total_bytes": 16 * 2**30, "modules": []},
    "gpu": [{"name": "Intel(R) UHD Graphics", "luid": "0x00000000_0x0000F10E", "integrated": True}],
    "storage": [{"disk": "PhysicalDrive0", "model": "WD PC SN740", "size_bytes": 512 * 10**9}],
    "network": [
        {"name": "Intel Wi-Fi", "interface": "Wi-Fi", "type": "wifi"},
        {"name": "Intel Ethernet", "interface": "Ethernet", "type": "ethernet"},
    ],
    "battery": {"name": "5B11M90000", "design_capacity_wh": 46.5},
    "display": {"panel_size": {"width_cm": 31, "height_cm": 17, "diagonal_in": 13.9}},
    "serial_number": "SECRET-SERIAL",
}


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "APP_ENV": "test",
        "DATABASE_URL": "",
        "REDIS_URL": "",
        "AGENT_INGEST_KEY": AGENT_KEY,
        "AUTH_MODE": "none",
        "API_KEYS": "",
        "LOG_LEVEL": "WARNING",
        "RATE_LIMIT_PER_MINUTE": 10_000,
        # Most API tests post with the shared key for brevity; Phase-2 token-only mode is tested explicitly.
        "ALLOW_ENROLLMENT_KEY_INGEST": True,
        "INGEST_RATE_PER_DEVICE_PER_MIN": 100_000,
        "INGEST_RATE_BURST": 10_000,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def sample(
    metric: str,
    value: Any,
    unit: str = "percent",
    *,
    component: str = "cpu",
    labels: dict[str, str] | None = None,
    ts: datetime | None = None,
    available: bool = True,
    reason: str | None = None,
    kind: str = "measured",
) -> dict[str, Any]:
    return {
        "metric": metric,
        "component": component,
        "value": value if available else None,
        "unit": unit,
        "timestamp": (ts or datetime.now(UTC)).isoformat(),
        "source": "test-source",
        "quality": "GOOD" if available else "UNAVAILABLE",
        "availability": "available" if available else "unavailable",
        "kind": kind,
        "reason": reason,
        "labels": labels or {},
    }


def batch(
    samples: list[dict[str, Any]], seq: int = 1, processes: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "device_id": DEVICE,
        "agent_version": "test",
        "sequence": seq,
        "sent_at": datetime.now(UTC).isoformat(),
        "samples": samples,
        "processes": processes,
    }


def inventory_envelope() -> dict[str, Any]:
    return {
        "device_id": DEVICE,
        "agent_version": "test",
        "discovered_at": datetime.now(UTC).isoformat(),
        "inventory": INVENTORY,
    }


def typical_samples(
    ts: datetime | None = None, cpu: float = 35.0, mem: float = 60.0, zone_c: float = 61.0
) -> list[dict[str, Any]]:
    ts = ts or datetime.now(UTC)
    return [
        sample("cpu.usage_percent", cpu, ts=ts),
        sample("cpu.core_usage_percent", cpu, labels={"core": "0"}, ts=ts),
        sample("cpu.frequency_mhz", 2600.0, "MHz", ts=ts, kind="derived"),
        sample("cpu.temperature_c", None, "celsius", ts=ts, available=False, reason="needs LHM"),
        sample("memory.usage_percent", mem, component="memory", ts=ts),
        sample("memory.used_bytes", 10 * 2**30, "bytes", component="memory", ts=ts),
        sample("memory.total_bytes", 16 * 2**30, "bytes", component="memory", ts=ts),
        sample(
            "thermal.zone_temperature_c",
            zone_c,
            "celsius",
            component="thermal",
            labels={"zone": "_TZ.THM0"},
            ts=ts,
        ),
        sample(
            "thermal.passive_limit_percent", 100.0, component="thermal", labels={"zone": "_TZ.THM0"}, ts=ts
        ),
        sample(
            "gpu.usage_percent",
            12.0,
            labels={"adapter": "Intel(R) UHD Graphics", "luid": "0x00000000_0x0000F10E"},
            component="gpu",
            ts=ts,
        ),
        sample("battery.charge_percent", 80.0, component="battery", ts=ts),
        sample("battery.charging_state", "discharging", "state", component="battery", ts=ts),
        sample("battery.health_percent", 95.6, component="battery", ts=ts, kind="derived"),
        sample("battery.remaining_capacity_wh", 35.0, "Wh", component="battery", ts=ts),
        sample("battery.full_charge_capacity_wh", 44.45, "Wh", component="battery", ts=ts),
        sample("power.source", "battery", "state", component="battery", ts=ts),
        sample("power.system_power_w", 9.5, "W", component="battery", ts=ts),
        sample("disk.usage_percent", 51.3, component="storage", labels={"volume": "C:"}, ts=ts),
        sample(
            "disk.health_status",
            "Healthy",
            "state",
            component="storage",
            labels={"disk": "PhysicalDrive0", "model": "WD"},
            ts=ts,
        ),
        sample("network.link_up", True, "bool", component="network", labels={"nic": "Wi-Fi"}, ts=ts),
        sample("network.rx_bytes_per_sec", 1000.0, "B/s", component="network", ts=ts, kind="derived"),
        sample(
            "fan.speed_rpm", None, "rpm", component="cooling", ts=ts, available=False, reason="no tachometer"
        ),
    ]


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(settings())
    with TestClient(app) as c:
        yield c


@pytest.fixture
def live_client(client: TestClient) -> TestClient:
    h = {"X-Agent-Key": AGENT_KEY}
    assert client.post("/api/v1/ingest/inventory", json=inventory_envelope(), headers=h).status_code == 202
    now = datetime.now(UTC)
    for i in range(3):
        r = client.post(
            "/api/v1/ingest/telemetry",
            json=batch(typical_samples(now - timedelta(seconds=2 - i)), seq=i + 1),
            headers=h,
        )
        assert r.status_code == 202, r.text
    return client
