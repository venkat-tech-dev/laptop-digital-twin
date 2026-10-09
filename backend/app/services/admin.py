"""Accounts, workspaces, agent configuration, acknowledgements and sync settings."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.core.security import Authenticator, AuthError
from app.domain.admin.models import AnomalyAck, Role, User, Workspace, hash_password, verify_password
from app.repositories.admin import AdminRepository

AGENT_CONFIG_KEY = "agent_config"
SYNC_CONFIG_KEY = "sync_config"
_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")

AGENT_LIMITS = {
    "telemetry_interval_ms": (250, 60_000),
    "process_interval_ms": (1000, 60_000),
    "top_process_count": (1, 100),
}


class ConflictError(Exception):
    pass


class AdminService:
    def __init__(self, repo: AdminRepository, auth: Authenticator, settings: Settings) -> None:
        self._repo = repo
        self._auth = auth
        self._settings = settings

    # ------------------------------------------------------------------ agent configuration
    async def agent_config(self) -> dict[str, Any]:
        stored = await self._repo.get_setting(AGENT_CONFIG_KEY)
        if stored is None:
            return {
                "configured": False,
                "telemetry_interval_ms": self._settings.telemetry_interval_ms,
                "process_interval_ms": 3000,
                "top_process_count": 15,
                "collect_process_details": False,
                "version": 0,
                "updated_at": None,
                "updated_by": None,
            }
        return {"configured": True, **stored}

    async def set_agent_config(self, changes: dict[str, Any], by: str) -> dict[str, Any]:
        current = await self.agent_config()
        merged = {k: v for k, v in current.items() if k != "configured"}
        for key, (lo, hi) in AGENT_LIMITS.items():
            if changes.get(key) is not None:
                value = int(changes[key])
                if not lo <= value <= hi:
                    raise ValueError(f"{key} must be between {lo} and {hi}")
                merged[key] = value
        if changes.get("collect_process_details") is not None:
            merged["collect_process_details"] = bool(changes["collect_process_details"])
        merged["version"] = int(current.get("version") or 0) + 1
        merged["updated_at"] = datetime.now(UTC).isoformat()
        merged["updated_by"] = by
        await self._repo.set_setting(AGENT_CONFIG_KEY, merged, by)
        return {"configured": True, **merged}

    # ------------------------------------------------------------------ sync configuration
    async def sync_config(self) -> dict[str, Any]:
        stored = await self._repo.get_setting(SYNC_CONFIG_KEY) or {}
        return {
            "enabled": bool(stored.get("enabled", False)),
            "target_url": stored.get("target_url"),
            "include_processes": bool(stored.get("include_processes", False)),
            "key_configured": bool(self._settings.sync_target_key),
            "updated_at": stored.get("updated_at"),
            "updated_by": stored.get("updated_by"),
        }

    async def set_sync_config(self, changes: dict[str, Any], by: str) -> dict[str, Any]:
        current = await self.sync_config()
        url = changes.get("target_url", current["target_url"])
        if url:
            url = str(url).rstrip("/")
            if not re.match(r"^https?://[^\s/]+", url):
                raise ValueError("target_url must be an http(s) URL of another Laptop Digital Twin backend")
            if url.startswith("http://") and not re.match(
                r"^http://(localhost|127\.|10\.|192\.168\.|172\.)", url
            ):
                raise ValueError("Use https:// for a sync target outside the local network")
        enabled = bool(changes.get("enabled", current["enabled"]))
        if enabled and not url:
            raise ValueError("Set target_url before enabling sync")
        value = {
            "enabled": enabled,
            "target_url": url or None,
            "include_processes": bool(changes.get("include_processes", current["include_processes"])),
            "updated_at": datetime.now(UTC).isoformat(),
            "updated_by": by,
        }
        await self._repo.set_setting(SYNC_CONFIG_KEY, value, by)
        return await self.sync_config()

    # ------------------------------------------------------------------ accounts
    async def setup_required(self) -> bool:
        return await self._repo.count_users() == 0

    async def setup(self, username: str, password: str, token: str | None) -> User:
        if not await self.setup_required():
            raise ConflictError("Setup already completed")
        expected = self._settings.setup_token
        if expected and token != expected:
            raise AuthError("Invalid setup token")
        return await self.create_user(username, password, Role.ADMIN)

    async def create_user(self, username: str, password: str, role: Role) -> User:
        if not _USERNAME.match(username):
            raise ValueError("Username: 3-64 characters, letters, digits, '.', '_' or '-'")
        _check_password(password)
        if await self._repo.get_user(username) is not None:
            raise ConflictError("Username already exists")
        user = User(str(uuid.uuid4()), username, role, hash_password(password), datetime.now(UTC))
        await self._repo.save_user(user)
        return user

    async def login(self, username: str, password: str) -> tuple[User, str, datetime]:
        user = await self._repo.get_user(username)
        # Verify against a dummy hash when the user is unknown so timing does not reveal usernames.
        encoded = user.password_hash if user else _DUMMY_HASH
        if not verify_password(password, encoded) or user is None or user.disabled:
            raise AuthError("Invalid username or password")
        user.last_login_at = datetime.now(UTC)
        await self._repo.save_user(user)
        token, expires = self._auth.issue_user_jwt(user.username, user.role.value)
        return user, token, expires

    async def verify_credentials(self, username: str, password: str) -> User:
        """Password check only (Phase 9: tokens are issued by the session-aware sign-in flow)."""
        user = await self._repo.get_user(username)
        encoded = user.password_hash if user else _DUMMY_HASH  # constant work for unknown users
        if not verify_password(password, encoded) or user is None or user.disabled:
            raise AuthError("Invalid username or password")
        return user

    async def mark_login(self, user: User) -> None:
        user.last_login_at = datetime.now(UTC)
        await self._repo.save_user(user)

    async def get_user(self, username: str) -> User | None:
        return await self._repo.get_user(username)

    async def create_external_user(self, username: str) -> User:
        """User provisioned by an identity provider / SCIM: no usable local password."""
        if await self._repo.get_user(username) is not None:
            raise ConflictError("Username already exists")
        user = User(str(uuid.uuid4()), username, Role.VIEWER, "!external", datetime.now(UTC))
        await self._repo.save_user(user)
        return user

    async def save_user(self, user: User) -> None:
        await self._repo.save_user(user)

    async def active_user(self, username: str) -> User | None:
        user = await self._repo.get_user(username)
        return user if user and not user.disabled else None

    async def users(self) -> list[User]:
        return await self._repo.list_users()

    async def update_user(self, user_id: str, changes: dict[str, Any], actor: str) -> User:
        users = await self._repo.list_users()
        user = next((u for u in users if u.user_id == user_id), None)
        if user is None:
            raise LookupError("User not found")
        if changes.get("role") is not None:
            new_role = Role(changes["role"])
            if user.role is Role.ADMIN and new_role is not Role.ADMIN and self._admins(users) <= 1:
                raise ConflictError("At least one active administrator is required")
            user.role = new_role
        if changes.get("disabled") is not None:
            if changes["disabled"] and user.username == actor:
                raise ConflictError("You cannot disable your own account")
            if changes["disabled"] and user.role is Role.ADMIN and self._admins(users) <= 1:
                raise ConflictError("At least one active administrator is required")
            user.disabled = bool(changes["disabled"])
        if changes.get("password"):
            _check_password(str(changes["password"]))
            user.password_hash = hash_password(str(changes["password"]))
        await self._repo.save_user(user)
        return user

    async def delete_user(self, user_id: str, actor: str) -> None:
        users = await self._repo.list_users()
        user = next((u for u in users if u.user_id == user_id), None)
        if user is None:
            raise LookupError("User not found")
        if user.username == actor:
            raise ConflictError("You cannot delete your own account")
        if user.role is Role.ADMIN and self._admins(users) <= 1:
            raise ConflictError("At least one active administrator is required")
        await self._repo.delete_user(user_id)

    @staticmethod
    def _admins(users: list[User]) -> int:
        return sum(1 for u in users if u.role is Role.ADMIN and not u.disabled)

    # ------------------------------------------------------------------ workspaces
    async def workspaces(self, known_devices: list[str]) -> list[Workspace]:
        """Workspaces; the first run creates "Local" holding every known device, new devices join it."""
        spaces = await self._repo.list_workspaces()
        if not spaces:
            ws = Workspace(str(uuid.uuid4()), "Local", datetime.now(UTC), sorted(known_devices))
            await self._repo.save_workspace(ws)
            return [ws]
        assigned = {d for w in spaces for d in w.device_ids}
        orphans = [d for d in known_devices if d not in assigned]
        if orphans:
            spaces[0].device_ids = sorted({*spaces[0].device_ids, *orphans})
            await self._repo.save_workspace(spaces[0])
        return spaces

    async def save_workspace(
        self, workspace_id: str | None, name: str, device_ids: list[str] | None
    ) -> Workspace:
        name = name.strip()
        if not 1 <= len(name) <= 96:
            raise ValueError("Workspace name must be 1-96 characters")
        spaces = await self._repo.list_workspaces()
        if any(w.name.lower() == name.lower() and w.workspace_id != workspace_id for w in spaces):
            raise ConflictError("A workspace with this name already exists")
        existing = next((w for w in spaces if w.workspace_id == workspace_id), None)
        if workspace_id and existing is None:
            raise LookupError("Workspace not found")
        ws = existing or Workspace(str(uuid.uuid4()), name, datetime.now(UTC), [])
        ws.name = name
        if device_ids is not None:
            ws.device_ids = sorted(set(device_ids))
            for other in spaces:  # a device belongs to exactly one workspace
                if other.workspace_id != ws.workspace_id and set(other.device_ids) & set(ws.device_ids):
                    other.device_ids = [d for d in other.device_ids if d not in ws.device_ids]
                    await self._repo.save_workspace(other)
        await self._repo.save_workspace(ws)
        return ws

    async def delete_workspace(self, workspace_id: str) -> None:
        spaces = await self._repo.list_workspaces()
        if len(spaces) <= 1:
            raise ConflictError("At least one workspace is required")
        target = next((w for w in spaces if w.workspace_id == workspace_id), None)
        if target is None:
            raise LookupError("Workspace not found")
        if target.device_ids:  # devices move to the first remaining workspace
            keep = next(w for w in spaces if w.workspace_id != workspace_id)
            keep.device_ids = sorted({*keep.device_ids, *target.device_ids})
            await self._repo.save_workspace(keep)
        await self._repo.delete_workspace(workspace_id)

    # ------------------------------------------------------------------ acknowledgements
    async def acks(self, device_id: str) -> dict[str, AnomalyAck]:
        return await self._repo.list_acks(device_id)

    async def acknowledge(self, anomaly_id: str, device_id: str, by: str, note: str | None) -> AnomalyAck:
        ack = AnomalyAck(anomaly_id, device_id, by, datetime.now(UTC), (note or "").strip()[:500] or None)
        await self._repo.save_ack(ack)
        return ack

    async def unacknowledge(self, anomaly_id: str) -> bool:
        return await self._repo.delete_ack(anomaly_id)


def _check_password(password: str) -> None:
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters")
    if password.lower() == password or password.upper() == password or not any(c.isdigit() for c in password):
        raise ValueError("Password needs upper- and lower-case letters and a digit")


_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password-1A")
