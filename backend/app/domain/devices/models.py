from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class DeviceStatus(StrEnum):
    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    OFFLINE = "OFFLINE"


def status_from_age(
    age_s: float | None, degraded_after: float, stale_after: float, offline_after: float
) -> DeviceStatus:
    if age_s is None or age_s > offline_after:
        return DeviceStatus.OFFLINE
    if age_s > stale_after:
        return DeviceStatus.STALE
    if age_s > degraded_after:
        return DeviceStatus.DEGRADED
    return DeviceStatus.LIVE


@dataclass(slots=True)
class Device:
    device_id: str
    manufacturer: str | None
    model: str | None
    model_number: str | None
    os_name: str | None
    inventory: dict[str, Any]
    agent_version: str
    first_seen: datetime
    last_inventory_at: datetime
    last_seen: datetime | None = None
    last_sequence: int = -1
    status: DeviceStatus = DeviceStatus.OFFLINE
    sensor_provider: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
