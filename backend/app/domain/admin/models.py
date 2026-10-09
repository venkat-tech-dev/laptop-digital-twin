"""Accounts, workspaces, operator settings and anomaly acknowledgements."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    ADMIN = "admin"  # manage users, workspaces, agent configuration, sync, model files
    OPERATOR = "operator"  # acknowledge anomalies, run simulations
    VIEWER = "viewer"  # read-only, every device
    EMPLOYEE = "employee"  # read-only, only the devices assigned to this account

    @property
    def rank(self) -> int:
        return {"employee": -1, "viewer": 0, "operator": 1, "admin": 2}[self.value]


@dataclass(slots=True)
class DeviceAssignment:
    """Organization -> department (workspace) -> employee -> device."""

    device_id: str
    username: str | None  # account that may see this device (role employee); None = unassigned
    employee_name: str | None  # display name of the person using the laptop
    updated_at: datetime
    updated_by: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "username": self.username,
            "employee_name": self.employee_name,
            "updated_at": self.updated_at.isoformat(),
            "updated_by": self.updated_by,
        }


@dataclass(slots=True)
class User:
    user_id: str
    username: str
    role: Role
    password_hash: str
    created_at: datetime
    disabled: bool = False
    last_login_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role.value,
            "created_at": self.created_at.isoformat(),
            "disabled": self.disabled,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


@dataclass(slots=True)
class Workspace:
    workspace_id: str
    name: str
    created_at: datetime
    device_ids: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "device_ids": list(self.device_ids),
        }


@dataclass(frozen=True, slots=True)
class AnomalyAck:
    anomaly_id: str
    device_id: str
    acknowledged_by: str
    acknowledged_at: datetime
    note: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": self.acknowledged_at.isoformat(),
            "note": self.note,
        }


# ---------------------------------------------------------------- password hashing (stdlib scrypt)
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)
