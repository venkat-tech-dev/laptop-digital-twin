"""Signed action envelopes (Ed25519).

The backend signs a canonical JSON form of the envelope with its private key; agents hold only the
public key, so neither a compromised agent nor someone with database access can forge an action. The
agent rejects an envelope that is expired, issued in the future, for another device, for an action or
version it does not allow, with malformed parameters, a reused nonce or an invalid signature; an
already-executed ``execution_id`` returns the previous result instead of running again.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ENVELOPE_VERSION = 1


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
    dry_run: bool = False
    nonce: str = field(default_factory=lambda: secrets.token_hex(16))
    key_id: str = ""
    v: int = ENVELOPE_VERSION
    signature: str = ""

    def canonical(self) -> bytes:
        body = {k: v for k, v in asdict(self).items() if k != "signature"}
        return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()

    def digest(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    def public(self) -> dict[str, Any]:
        return asdict(self)


class Signer:
    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key
        raw = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.public_key_b64 = base64.b64encode(raw).decode()
        self.key_id = hashlib.sha256(raw).hexdigest()[:16]

    @classmethod
    def from_b64(cls, value: str) -> Signer:
        raw = base64.b64decode(value.strip())
        if len(raw) != 32:
            raise ValueError("REMEDIATION_SIGNING_KEY must be a base64 Ed25519 private key (32 bytes)")
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    @staticmethod
    def generate_b64() -> str:
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        )
        return base64.b64encode(raw).decode()

    def sign(self, env: Envelope) -> Envelope:
        env.key_id = self.key_id
        env.signature = base64.b64encode(self._key.sign(env.canonical())).decode()
        return env


def issue(
    signer: Signer,
    *,
    execution_id: str,
    remediation_id: str,
    action_id: str,
    action_version: int,
    device_id: str,
    requested_by: str,
    approved_by: str | None,
    policy_version: int,
    parameters: dict[str, Any],
    execution_timeout_s: int,
    ttl_s: int,
    now: datetime,
    dry_run: bool = False,
) -> Envelope:
    env = Envelope(
        execution_id=execution_id,
        remediation_id=remediation_id,
        action_id=action_id,
        action_version=action_version,
        device_id=device_id,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
        requested_by=requested_by,
        approved_by=approved_by,
        policy_version=policy_version,
        parameters=parameters,
        execution_timeout_s=execution_timeout_s,
        dry_run=dry_run,
    )
    return signer.sign(env)


def verify(public_key_b64: str, data: dict[str, Any]) -> Envelope:
    """Signature check only (the agent adds expiry / device / allowlist / replay checks). Raises ValueError."""  # noqa: E501
    try:
        env = Envelope(**{k: data[k] for k in Envelope.__dataclass_fields__ if k in data})
    except TypeError as exc:
        raise ValueError(f"malformed envelope: {exc}") from exc
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(env.signature), env.canonical())
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("invalid envelope signature") from exc
    return env
