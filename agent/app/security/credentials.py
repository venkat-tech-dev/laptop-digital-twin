"""Per-device credential storage.

The device token issued by the backend at registration is encrypted with Windows DPAPI
(``CryptProtectData``) and written to ``credentials.bin`` in the agent data directory. DPAPI binds
the ciphertext to the Windows account running the agent (``LocalSystem`` for the service), so the
file is useless if copied elsewhere. The token is never logged or written in plain text.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger("agent.credentials")

_ENTROPY = b"ldt-agent-device-token-v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class CredentialError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class DeviceCredential:
    device_id: str
    token: str
    backend_url: str


def _protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise CredentialError("DPAPI is only available on Windows")
    import win32crypt

    return bytes(
        win32crypt.CryptProtectData(data, "ldt-agent", _ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN)
    )


def _unprotect(blob: bytes) -> bytes:
    if sys.platform != "win32":
        raise CredentialError("DPAPI is only available on Windows")
    import win32crypt

    try:
        _, data = win32crypt.CryptUnprotectData(blob, _ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN)
    except Exception as exc:  # pywintypes.error: wrong account / corrupted file
        raise CredentialError(f"Credential file cannot be decrypted by this account: {exc}") from exc
    return bytes(data)


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, device_id: str, backend_url: str) -> DeviceCredential | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(_unprotect(self._path.read_bytes()))
        except (CredentialError, ValueError) as exc:
            log.warning("credential_unreadable_reregistering", error=str(exc)[:200])
            return None
        if data.get("device_id") != device_id or data.get("backend_url") != backend_url:
            return None  # different device identity or different backend: register again
        token = data.get("token")
        return DeviceCredential(device_id, token, backend_url) if isinstance(token, str) and token else None

    def save(self, cred: DeviceCredential) -> None:
        blob = _protect(
            json.dumps(
                {"device_id": cred.device_id, "token": cred.token, "backend_url": cred.backend_url}
            ).encode()
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self._path)

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)
