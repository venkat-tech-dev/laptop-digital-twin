"""Accounts, workspaces, operator settings and acknowledgements (memory + PostgreSQL)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.domain.admin.models import AnomalyAck, DeviceAssignment, Role, User, Workspace
from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import (
    AnomalyAckRow,
    AppSettingRow,
    DeviceAssignmentRow,
    DeviceCredentialRow,
    UserRow,
    WorkspaceDeviceRow,
    WorkspaceRow,
)


class AdminRepository(Protocol):
    async def get_setting(self, key: str) -> dict[str, Any] | None: ...

    async def set_setting(self, key: str, value: dict[str, Any], by: str | None) -> None: ...

    async def count_users(self) -> int: ...

    async def list_users(self) -> list[User]: ...

    async def get_user(self, username: str) -> User | None: ...

    async def save_user(self, user: User) -> None: ...

    async def delete_user(self, user_id: str) -> bool: ...

    async def list_workspaces(self) -> list[Workspace]: ...

    async def save_workspace(self, ws: Workspace) -> None: ...

    async def delete_workspace(self, workspace_id: str) -> bool: ...

    async def list_acks(self, device_id: str) -> dict[str, AnomalyAck]: ...

    async def save_ack(self, ack: AnomalyAck) -> None: ...

    async def delete_ack(self, anomaly_id: str) -> bool: ...

    async def get_device_credential(self, device_id: str) -> DeviceCredential | None: ...

    async def save_device_credential(self, cred: DeviceCredential) -> None: ...

    async def list_device_credentials(self) -> list[DeviceCredential]: ...

    async def list_assignments(self) -> list[DeviceAssignment]: ...

    async def save_assignment(self, assignment: DeviceAssignment) -> None: ...


@dataclass(slots=True)
class DeviceCredential:
    device_id: str
    token_hash: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked: bool = False
    expires_at: datetime | None = None  # Phase 9: None = legacy (no expiry)


class MemoryAdminRepository:
    def __init__(self) -> None:
        self._assignments: dict[str, DeviceAssignment] = {}
        self._settings: dict[str, dict[str, Any]] = {}
        self._users: dict[str, User] = {}
        self._workspaces: dict[str, Workspace] = {}
        self._acks: dict[str, AnomalyAck] = {}
        self._creds: dict[str, DeviceCredential] = {}

    async def get_setting(self, key: str) -> dict[str, Any] | None:
        return self._settings.get(key)

    async def set_setting(self, key: str, value: dict[str, Any], by: str | None) -> None:
        self._settings[key] = dict(value)

    async def count_users(self) -> int:
        return len(self._users)

    async def list_users(self) -> list[User]:
        return sorted(self._users.values(), key=lambda u: u.created_at)

    async def get_user(self, username: str) -> User | None:
        return next((u for u in self._users.values() if u.username.lower() == username.lower()), None)

    async def save_user(self, user: User) -> None:
        self._users[user.user_id] = user

    async def delete_user(self, user_id: str) -> bool:
        return self._users.pop(user_id, None) is not None

    async def list_workspaces(self) -> list[Workspace]:
        return sorted(self._workspaces.values(), key=lambda w: w.created_at)

    async def save_workspace(self, ws: Workspace) -> None:
        self._workspaces[ws.workspace_id] = ws

    async def delete_workspace(self, workspace_id: str) -> bool:
        return self._workspaces.pop(workspace_id, None) is not None

    async def list_acks(self, device_id: str) -> dict[str, AnomalyAck]:
        return {k: a for k, a in self._acks.items() if a.device_id == device_id}

    async def save_ack(self, ack: AnomalyAck) -> None:
        self._acks[ack.anomaly_id] = ack

    async def delete_ack(self, anomaly_id: str) -> bool:
        return self._acks.pop(anomaly_id, None) is not None

    async def get_device_credential(self, device_id: str) -> DeviceCredential | None:
        return self._creds.get(device_id)

    async def save_device_credential(self, cred: DeviceCredential) -> None:
        self._creds[cred.device_id] = cred

    async def list_device_credentials(self) -> list[DeviceCredential]:
        return list(self._creds.values())

    async def list_assignments(self) -> list[DeviceAssignment]:
        return list(self._assignments.values())

    async def save_assignment(self, assignment: DeviceAssignment) -> None:
        self._assignments[assignment.device_id] = assignment


def _user(row: UserRow) -> User:
    return User(
        user_id=row.id,
        username=row.username,
        role=Role(row.role),
        password_hash=row.password_hash,
        created_at=row.created_at,
        disabled=row.disabled,
        last_login_at=row.last_login_at,
    )


class SqlAdminRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_setting(self, key: str) -> dict[str, Any] | None:
        async with self._db.sessions() as session:
            row = await session.get(AppSettingRow, key)
        return dict(row.value) if row else None

    async def set_setting(self, key: str, value: dict[str, Any], by: str | None) -> None:
        stmt = pg_insert(AppSettingRow).values(
            key=key, value=value, updated_at=datetime.now(UTC), updated_by=by
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[AppSettingRow.key],
            set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at, "updated_by": by},
        )
        async with self._db.sessions.begin() as session:
            await session.execute(stmt)

    async def count_users(self) -> int:
        async with self._db.sessions() as session:
            return int((await session.execute(select(func.count()).select_from(UserRow))).scalar_one())

    async def list_users(self) -> list[User]:
        async with self._db.sessions() as session:
            rows = (await session.execute(select(UserRow).order_by(UserRow.created_at))).scalars().all()
        return [_user(r) for r in rows]

    async def get_user(self, username: str) -> User | None:
        async with self._db.sessions() as session:
            row = (
                await session.execute(select(UserRow).where(func.lower(UserRow.username) == username.lower()))
            ).scalar_one_or_none()
        return _user(row) if row else None

    async def save_user(self, user: User) -> None:
        values = {
            "id": user.user_id,
            "username": user.username,
            "role": user.role.value,
            "password_hash": user.password_hash,
            "created_at": user.created_at,
            "disabled": user.disabled,
            "last_login_at": user.last_login_at,
        }
        stmt = pg_insert(UserRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[UserRow.id], set_={k: stmt.excluded[k] for k in values if k != "id"}
        )
        async with self._db.sessions.begin() as session:
            await session.execute(stmt)

    async def delete_user(self, user_id: str) -> bool:
        async with self._db.sessions.begin() as session:
            result = await session.execute(delete(UserRow).where(UserRow.id == user_id))
        return bool(getattr(result, "rowcount", 0))

    async def list_workspaces(self) -> list[Workspace]:
        async with self._db.sessions() as session:
            rows = (
                (await session.execute(select(WorkspaceRow).order_by(WorkspaceRow.created_at)))
                .scalars()
                .all()
            )
            links = (await session.execute(select(WorkspaceDeviceRow))).scalars().all()
        devices: dict[str, list[str]] = {}
        for link in links:
            devices.setdefault(link.workspace_id, []).append(link.device_id)
        return [Workspace(r.id, r.name, r.created_at, sorted(devices.get(r.id, []))) for r in rows]

    async def save_workspace(self, ws: Workspace) -> None:
        stmt = pg_insert(WorkspaceRow).values(id=ws.workspace_id, name=ws.name, created_at=ws.created_at)
        stmt = stmt.on_conflict_do_update(index_elements=[WorkspaceRow.id], set_={"name": stmt.excluded.name})
        async with self._db.sessions.begin() as session:
            await session.execute(stmt)
            await session.execute(
                delete(WorkspaceDeviceRow).where(WorkspaceDeviceRow.workspace_id == ws.workspace_id)
            )
            for device_id in ws.device_ids:
                session.add(WorkspaceDeviceRow(workspace_id=ws.workspace_id, device_id=device_id))

    async def delete_workspace(self, workspace_id: str) -> bool:
        async with self._db.sessions.begin() as session:
            result = await session.execute(delete(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        return bool(getattr(result, "rowcount", 0))

    async def list_acks(self, device_id: str) -> dict[str, AnomalyAck]:
        async with self._db.sessions() as session:
            rows = (
                (await session.execute(select(AnomalyAckRow).where(AnomalyAckRow.device_id == device_id)))
                .scalars()
                .all()
            )
        return {
            r.anomaly_id: AnomalyAck(r.anomaly_id, r.device_id, r.acknowledged_by, r.acknowledged_at, r.note)
            for r in rows
        }

    async def save_ack(self, ack: AnomalyAck) -> None:
        values = {
            "anomaly_id": ack.anomaly_id,
            "device_id": ack.device_id,
            "acknowledged_by": ack.acknowledged_by,
            "acknowledged_at": ack.acknowledged_at,
            "note": ack.note,
        }
        stmt = pg_insert(AnomalyAckRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[AnomalyAckRow.anomaly_id],
            set_={k: stmt.excluded[k] for k in values if k != "anomaly_id"},
        )
        async with self._db.sessions.begin() as session:
            await session.execute(stmt)

    async def delete_ack(self, anomaly_id: str) -> bool:
        async with self._db.sessions.begin() as session:
            result = await session.execute(
                delete(AnomalyAckRow).where(AnomalyAckRow.anomaly_id == anomaly_id)
            )
        return bool(getattr(result, "rowcount", 0))

    async def get_device_credential(self, device_id: str) -> DeviceCredential | None:
        async with self._db.sessions() as session:
            row = await session.get(DeviceCredentialRow, device_id)
        if row is None:
            return None
        return DeviceCredential(
            row.device_id, row.token_hash, row.created_at, row.last_used_at, row.revoked, row.expires_at
        )

    async def save_device_credential(self, cred: DeviceCredential) -> None:
        values = {
            "device_id": cred.device_id,
            "token_hash": cred.token_hash,
            "created_at": cred.created_at,
            "last_used_at": cred.last_used_at,
            "revoked": cred.revoked,
            "expires_at": cred.expires_at,
        }
        stmt = pg_insert(DeviceCredentialRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[DeviceCredentialRow.device_id],
            set_={k: stmt.excluded[k] for k in values if k != "device_id"},
        )
        async with self._db.sessions.begin() as session:
            await session.execute(stmt)

    async def list_device_credentials(self) -> list[DeviceCredential]:
        async with self._db.sessions() as session:
            rows = (await session.execute(select(DeviceCredentialRow))).scalars().all()
        return [
            DeviceCredential(r.device_id, r.token_hash, r.created_at, r.last_used_at, r.revoked, r.expires_at)
            for r in rows
        ]

    async def list_assignments(self) -> list[DeviceAssignment]:
        async with self._db.sessions() as session:
            rows = (await session.execute(select(DeviceAssignmentRow))).scalars().all()
        return [
            DeviceAssignment(r.device_id, r.username, r.employee_name, r.updated_at, r.updated_by)
            for r in rows
        ]

    async def save_assignment(self, assignment: DeviceAssignment) -> None:
        values = {
            "device_id": assignment.device_id,
            "username": assignment.username,
            "employee_name": assignment.employee_name,
            "updated_at": assignment.updated_at,
            "updated_by": assignment.updated_by,
        }
        stmt = pg_insert(DeviceAssignmentRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[DeviceAssignmentRow.device_id],
            set_={k: stmt.excluded[k] for k in values if k != "device_id"},
        )
        async with self._db.sessions.begin() as session:
            await session.execute(stmt)
