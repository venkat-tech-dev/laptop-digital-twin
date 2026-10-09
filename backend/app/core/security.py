"""Authentication primitives: API keys, JWT, agent ingest key, and a simple rate limiter."""

from __future__ import annotations

import hmac
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import AuthMode, Settings


class AuthError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated caller plus its tenant context (Phase 9). ``role`` is the legacy rank derived from the
    organisation role; new checks use ``permissions`` (see app/domain/tenancy/permissions.py)."""

    subject: str
    method: str  # "anonymous" | "api_key" | "jwt" | "account"
    role: str = "admin"  # legacy rank: admin | operator | viewer | employee
    org_id: str = "default"
    org_role: str | None = None
    permissions: frozenset[str] = field(default_factory=frozenset)
    group_scope: frozenset[str] = field(default_factory=frozenset)
    platform_admin: bool = False
    session_id: str | None = None
    auth_time: float | None = None
    mfa: bool = False
    token_scope: str | None = None  # "mfa_setup": may only enrol MFA
    claims: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def has_role(self, role: str) -> bool:
        rank = {"employee": -1, "viewer": 0, "operator": 1, "admin": 2}
        return rank.get(self.role, -1) >= rank[role]

    def can(self, permission: str) -> bool:
        return permission in self.permissions


def _matches_any(candidate: str, keys: list[str]) -> bool:
    # Constant-time compare against every key (no early exit on the first mismatch).
    result = False
    for key in keys:
        result |= hmac.compare_digest(candidate.encode(), key.encode())
    return result


class Authenticator:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    @property
    def mode(self) -> AuthMode:
        return self._s.auth_mode

    def verify_agent_key(self, key: str | None) -> bool:
        expected = self._s.agent_ingest_key
        return bool(expected) and key is not None and hmac.compare_digest(key.encode(), expected.encode())

    def authenticate(self, api_key: str | None, bearer: str | None) -> Principal:
        mode = self._s.auth_mode
        if mode is AuthMode.NONE:
            return Principal("local", "anonymous")
        if bearer:
            return self._verify_jwt(bearer)
        if (
            mode is AuthMode.ACCOUNTS
            and api_key
            and self._s.api_keys
            and _matches_any(api_key, self._s.api_keys)
        ):
            return Principal("api-key", "api_key")
        if mode is AuthMode.API_KEY and api_key and _matches_any(api_key, self._s.api_keys):
            return Principal("api-key", "api_key")
        raise AuthError("Missing or invalid credentials")

    def authenticate_token(self, token: str | None) -> Principal:
        """WebSocket auth: token may be a JWT, or an API key in api_key mode."""
        mode = self._s.auth_mode
        if mode is AuthMode.NONE:
            return Principal("local", "anonymous")
        if not token:
            raise AuthError("Missing token")
        if token.count(".") == 2:
            return self._verify_jwt(token)
        if mode is AuthMode.API_KEY and _matches_any(token, self._s.api_keys):
            return Principal("api-key", "api_key")
        raise AuthError("Invalid token")

    def issue_jwt(self, api_key: str) -> tuple[str, datetime]:
        if not _matches_any(api_key, self._s.api_keys):
            raise AuthError("Invalid API key")
        if not self._s.jwt_secret:
            raise AuthError("JWT_SECRET is not configured")
        expires = datetime.now(UTC) + timedelta(minutes=self._s.jwt_ttl_minutes)
        token = jwt.encode(
            {"sub": "api-key", "exp": expires, "iat": datetime.now(UTC), "scope": "read"},
            self._s.jwt_secret,
            algorithm="HS256",
        )
        return token, expires

    def issue_user_jwt(
        self,
        username: str,
        role: str,
        *,
        session_id: str | None = None,
        org_id: str | None = None,
        mfa: bool = False,
        ttl_minutes: int | None = None,
        scope: str | None = None,
    ) -> tuple[str, datetime]:
        if not self._s.jwt_secret:
            raise AuthError("JWT_SECRET is not configured")
        now = datetime.now(UTC)
        expires = now + timedelta(
            minutes=min(ttl_minutes or self._s.jwt_ttl_minutes, self._s.jwt_ttl_minutes)
        )
        claims: dict[str, Any] = {
            "sub": username,
            "role": role,
            "exp": expires,
            "iat": now,
            "typ": "user",
            "auth_time": int(now.timestamp()),
            "amr": ["mfa"] if mfa else ["pwd"],
        }
        if session_id:
            claims["sid"] = session_id
        if org_id:
            claims["org"] = org_id
        if scope:
            claims["scope"] = scope
        token = jwt.encode(claims, self._s.jwt_secret, algorithm="HS256")
        return token, expires

    def _verify_jwt(self, token: str) -> Principal:
        if not self._s.jwt_secret:
            raise AuthError("JWT not configured")
        claims: dict[str, Any] | None = None
        error: Exception | None = None
        # current secret first, then previous secrets (rotation without logging everyone out at once)
        for secret in [self._s.jwt_secret, *self._s.jwt_secret_previous]:
            try:
                claims = jwt.decode(
                    token, secret, algorithms=["HS256"], options={"require": ["exp", "sub", "iat"]}
                )
                break
            except jwt.InvalidSignatureError as exc:
                error = exc
                continue
            except jwt.PyJWTError as exc:
                raise AuthError(f"Invalid token: {type(exc).__name__}") from exc
        if claims is None:
            raise AuthError(f"Invalid token: {type(error).__name__}")
        if claims.get("typ") == "user":
            role = str(claims.get("role", "viewer"))
            amr = claims.get("amr") or []
            return Principal(
                str(claims["sub"]),
                "account",
                role if role in ("admin", "operator", "viewer", "employee") else "viewer",
                org_id=str(claims.get("org") or "default"),
                session_id=claims.get("sid"),
                auth_time=float(claims.get("auth_time") or claims.get("iat") or 0),
                mfa="mfa" in amr,
                token_scope=claims.get("scope"),
                claims={k: v for k, v in claims.items() if k in ("sid", "org", "auth_time", "scope")},
            )
        return Principal(str(claims["sub"]), "jwt")


class SlidingWindowRateLimiter:
    """Per-client sliding-window limiter (in-process; put a gateway in front for multi-instance)."""

    def __init__(self, limit: int, window_s: float = 60.0, max_clients: int = 10_000) -> None:
        self._limit = limit
        self._window = window_s
        self._hits: dict[str, deque[float]] = {}
        self._max_clients = max_clients

    def allow(self, client: str, now: float | None = None) -> tuple[bool, float]:
        now = time.monotonic() if now is None else now
        hits = self._hits.get(client)
        if hits is None:
            if len(self._hits) >= self._max_clients:
                self._hits.clear()  # bounded memory under client-id churn
            hits = self._hits[client] = deque()
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False, self._window - (now - hits[0])
        hits.append(now)
        return True, 0.0


class TokenBucketLimiter:
    """Per-key token bucket (used per device on the ingest path).

    ``rate_per_min`` tokens refill continuously up to ``burst``; a request costs one token. When the
    bucket is empty the caller gets the seconds until the next token, sent back as ``Retry-After``.
    In-process: with several backend replicas each enforces its own share.
    """

    def __init__(self, rate_per_min: float, burst: int, max_keys: int = 50_000) -> None:
        self._rate = rate_per_min / 60.0
        self._burst = float(burst)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._max_keys = max_keys
        self.limited_total = 0

    def allow(self, key: str, now: float | None = None, cost: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        if tokens >= cost:
            if len(self._buckets) >= self._max_keys and key not in self._buckets:
                self._buckets.clear()
            self._buckets[key] = (tokens - cost, now)
            return True, 0.0
        self._buckets[key] = (tokens, now)
        self.limited_total += 1
        return False, (cost - tokens) / self._rate if self._rate > 0 else 60.0
