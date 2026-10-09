"""Pinning of the platform's envelope-signing public key.

Explicit pin (``AGENT_ACTION_PUBLIC_KEY``) always wins. Otherwise the key is fetched once over the
device-token-authenticated channel and pinned to disk (trust on first use). A different key offered
later is NOT accepted automatically: every envelope then fails signature validation until an administrator
configures the new key explicitly or removes the pin file (key rotation is a deliberate act).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from app.remediation.envelope import key_id

log = structlog.get_logger("agent.remediation")


class KeyPin:
    def __init__(self, path: Path, explicit: str | None = None) -> None:
        self._path = path
        self._explicit = explicit.strip() if explicit else None
        self._pinned: str | None = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._pinned = str(data["public_key"]) if _valid(data.get("public_key")) else None
        except (OSError, ValueError, KeyError):
            self._pinned = None

    def current(self) -> str | None:
        return self._explicit if _valid(self._explicit) else self._pinned

    def trust_on_first_use(self, offered: dict[str, Any] | None) -> bool:
        if self.current() is not None or not offered or not _valid(offered.get("public_key")):
            return False
        key = str(offered["public_key"])
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {"public_key": key, "key_id": key_id(key), "pinned_at": datetime.now(UTC).isoformat()}
            ),
            encoding="utf-8",
        )
        self._pinned = key
        log.warning("action_key_pinned_on_first_use", key_id=key_id(key))
        return True


def _valid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except ValueError:
        return False
