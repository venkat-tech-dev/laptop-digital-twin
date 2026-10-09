from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.api.access import ScopedDevice, visible_devices
from app.api.deps import ContainerDep, Reader
from app.core.container import Container
from app.schemas.twin import (
    ComponentOut,
    DeviceOut,
    GeometryOut,
    HealthEventOut,
    HealthOverviewOut,
    TwinOut,
)
from app.services.digital_twin import TwinState

router = APIRouter(tags=["digital twin"])

NO_DEVICE = "No device has connected yet. Start the telemetry agent on the laptop (see README)."


def _twin(container: Container, device_id: str | None) -> TwinState:
    twin = container.twin.get(device_id)
    if twin is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_DEVICE)
    return twin


def _device_out(container: Container, twin: TwinState) -> DeviceOut:
    d = twin.device
    geometry = container.geometry.resolve(d.manufacturer, d.model, d.inventory)
    return DeviceOut(
        device_id=d.device_id,
        manufacturer=d.manufacturer,
        model=d.model,
        model_number=d.model_number,
        os_name=d.os_name,
        status=d.status.value,
        last_seen=d.last_seen,
        agent_version=d.agent_version,
        data_source="LOCAL WINDOWS HARDWARE",
        mode="LIVE",
        telemetry="REAL",
        sensor_provider=d.sensor_provider or d.inventory.get("sensor_provider"),
        geometry=GeometryOut(**geometry),
    )


@router.get("/device", response_model=DeviceOut, summary="Identity, live status and geometry of the device")
async def get_device(container: ContainerDep, device_id: ScopedDevice) -> DeviceOut:
    return _device_out(container, _twin(container, device_id))


@router.get("/device/list", response_model=list[DeviceOut])
async def list_devices(container: ContainerDep, principal: Reader) -> list[DeviceOut]:
    allowed = visible_devices(principal, container)
    return [
        _device_out(container, t)
        for t in (container.twin.get(d.device_id) for d in container.twin.devices())
        if t and (allowed is None or t.device.device_id in allowed)
    ]


@router.get("/hardware", summary="Hardware inventory discovered by the agent")
async def get_hardware(container: ContainerDep, device_id: ScopedDevice) -> dict[str, Any]:
    twin = _twin(container, device_id)
    inventory = dict(twin.device.inventory)
    if not container.settings.expose_serial_numbers:
        inventory.pop("serial_number", None)
        inventory["storage"] = [
            {k: v for k, v in s.items() if k != "serial_number"} for s in inventory.get("storage", [])
        ]
    return {
        "device_id": twin.device.device_id,
        "discovered_at": twin.device.last_inventory_at,
        "inventory": inventory,
    }


@router.get("/twin", response_model=TwinOut, summary="Full current digital twin state")
async def get_twin(container: ContainerDep, device_id: ScopedDevice) -> Any:
    snapshot = container.twin.snapshot(device_id)
    if snapshot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_DEVICE)
    return snapshot


@router.get("/twin/components", response_model=list[ComponentOut])
async def list_components(
    container: ContainerDep,
    device_id: ScopedDevice,
    include_telemetry: bool = Query(default=False),
) -> Any:
    twin = _twin(container, device_id)
    now = datetime.now(UTC)
    return [container.twin.component_dict(c, now, include_telemetry) for c in twin.components.values()]


@router.get("/twin/components/{component_id}", response_model=ComponentOut)
async def get_component(component_id: str, container: ContainerDep, device_id: ScopedDevice) -> Any:
    twin = _twin(container, device_id)
    comp = twin.components.get(component_id)
    if comp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown component '{component_id}'")
    return container.twin.component_dict(comp, datetime.now(UTC))


@router.get(
    "/health", response_model=HealthOverviewOut, summary="Explainable health scores (device + components)"
)
async def get_health(container: ContainerDep, device_id: ScopedDevice) -> Any:
    twin = _twin(container, device_id)
    return {
        "overall": twin.overall.to_dict(),
        "components": {
            cid: c.health.to_dict()
            for cid, c in twin.components.items()
            if c.health.reasons or c.health.score is not None
        },
    }


@router.get("/health/events", response_model=list[HealthEventOut])
async def get_health_events(
    container: ContainerDep,
    device_id: ScopedDevice,
    limit: int = Query(default=100, ge=1, le=1000),
) -> Any:
    twin = _twin(container, device_id)
    records = await container.event_repo.list_health_events(twin.device.device_id, limit)
    return [asdict(r) for r in records]
