"""Envelope verification (mirror of the backend's canonical form; independent code on purpose)."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SUPPORTED_VERSIONS = (1,)


class EnvelopeError(ValueError):
    pass


@dataclass
class Envelope:
    execution_id: str
    remediation_id: str
    action_id: str
    action_version: int
    device_id: str
    issued_at: str
    expires_at: str
    requested_by: str
    approved_by: str | None
    policy_version: int
    parameters: dict[str, Any]
    execution_timeout_s: int
    dry_run: bool
    nonce: str
    key_id: str
    v: int
    signature: str

    def canonical(self) -> bytes:
        body = {k: v for k, v in asdict(self).items() if k != "signature"}
        return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def key_id(public_key_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(public_key_b64)).hexdigest()[:16]


def parse_and_verify(data: Any, public_key_b64: str) -> Envelope:
    if not isinstance(data, dict):
        raise EnvelopeError("malformed envelope")
    fields = Envelope.__dataclass_fields__
    if set(data) - set(fields):
        raise EnvelopeError("unexpected envelope fields")
    try:
        env = Envelope(**{k: data[k] for k in fields})
    except (KeyError, TypeError) as exc:
        raise EnvelopeError("malformed envelope") from exc
    if env.v not in SUPPORTED_VERSIONS:
        raise EnvelopeError(f"unsupported envelope version {env.v}")
    if not isinstance(env.parameters, dict) or not isinstance(env.execution_timeout_s, int):
        raise EnvelopeError("malformed envelope")
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(env.signature), env.canonical())
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise EnvelopeError("invalid signature") from exc
    return env
