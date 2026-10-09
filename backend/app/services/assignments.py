"""Device ownership and the organization hierarchy.

    Organization (this deployment) -> Department (workspace) -> Employee (assignment) -> Device

Held in memory (loaded at startup, updated on every change) because the authorization check runs on
every device-scoped request and WebSocket subscription.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.admin.models import DeviceAssignment
from app.repositories.admin import AdminRepository


class AssignmentService:
    def __init__(self, repo: AdminRepository) -> None:
        self._repo = repo
        self._by_device: dict[str, DeviceAssignment] = {}
        self._departments: dict[str, list[str]] = {}  # device_id -> workspace names

    async def load(self) -> int:
        self._by_device = {a.device_id: a for a in await self._repo.list_assignments()}
        return len(self._by_device)

    def set_departments(self, workspaces: list[Any]) -> None:
        deps: dict[str, list[str]] = {}
        for w in workspaces:
            for d in w.device_ids:
                deps.setdefault(d, []).append(w.name)
        self._departments = deps

    def get(self, device_id: str) -> DeviceAssignment | None:
        return self._by_device.get(device_id)

    def devices_for(self, username: str) -> set[str]:
        return {d for d, a in self._by_device.items() if a.username == username}

    def departments(self, device_id: str) -> list[str]:
        return self._departments.get(device_id, [])

    def extras(self, device_id: str) -> dict[str, Any]:
        a = self._by_device.get(device_id)
        deps = self._departments.get(device_id) or []
        return {
            "owner": (a.employee_name or a.username) if a else None,
            "department": deps[0] if deps else None,
        }

    async def assign(
        self, device_id: str, username: str | None, employee_name: str | None, by: str | None
    ) -> DeviceAssignment:
        a = DeviceAssignment(device_id, username, employee_name, datetime.now(UTC), by)
        await self._repo.save_assignment(a)
        self._by_device[device_id] = a
        return a
