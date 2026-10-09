"""Central enterprise audit events (append-only, hash-chained).

Each event records who (actor id + type), what (action), on what (resource type + id), in which organisation,
when, from where (source, request id, network metadata), the result and the reason. The hash covers the
event and the previous event's hash; edits or deletions break verification, and in PostgreSQL a trigger
rejects UPDATE / DELETE outright. Metadata is free of secrets by construction (callers pass ids and codes).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

GENESIS = "0" * 64
CATEGORIES = (
    "authentication",
    "authorization",
    "user",
    "device",
    "policy",
    "remediation",
    "alert",
    "diagnosis",
    "data",
    "security",
    "organization",
    "identity",
)
SECRET_KEYS = ("password", "token", "secret", "key", "assertion", "code_verifier", "otp", "authorization")


@dataclass
class AuditEvent:
    event_id: str
    at: datetime
    org_id: str | None  # None: platform-level event (e.g. failed login of an unknown user)
    actor_id: str
    actor_type: str  # user | agent | system | scim | api_key | anonymous
    action: str  # e.g. auth.login, user.role_changed, device.revoked, policy.published, data.exported
    category: str
    resource_type: str | None = None
    resource_id: str | None = None
    result: str = "SUCCESS"  # SUCCESS | FAILURE | DENIED
    reason: str | None = None
    severity: str = "INFO"  # INFO | WARNING | HIGH
    source: str = "api"  # api | websocket | agent | worker | scim | oidc
    request_id: str | None = None
    ip: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = ""
    hash: str = ""

    def body(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("prev_hash")
        d.pop("hash")
        d["at"] = self.at.isoformat()
        return d

    def public(self) -> dict[str, Any]:
        d = self.body()
        d["hash"] = self.hash
        return d


def chain(prev: str, e: AuditEvent) -> str:
    return hashlib.sha256(
        (prev + "|" + json.dumps(e.body(), sort_keys=True, default=str)).encode()
    ).hexdigest()


def scrub(meta: dict[str, Any] | None, depth: int = 0) -> dict[str, Any]:
    """Defensive: drop secret-looking keys and cap sizes (callers should never pass secrets)."""
    out: dict[str, Any] = {}
    for k, v in list((meta or {}).items())[:30]:
        key = str(k)[:64]
        if any(s in key.lower() for s in SECRET_KEYS):
            out[key] = "[redacted]"
        elif isinstance(v, dict) and depth < 2:
            out[key] = scrub(v, depth + 1)
        elif isinstance(v, (list, tuple)):
            out[key] = [str(x)[:200] for x in list(v)[:30]]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[key] = v
        else:
            out[key] = str(v)[:500]
    return out
