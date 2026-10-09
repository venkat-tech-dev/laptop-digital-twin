"""IdentityService (Phase 9): secrets, sessions, local MFA (TOTP), OIDC, SAML (abstraction) and SCIM tokens.

Providers sit behind one abstraction: ``local`` (passwords, optional TOTP), ``oidc`` (authorization code
+ PKCE, state, nonce, issuer / audience / signature / expiry validation with clock skew) and ``saml``
(configuration,
SP metadata; assertions are REFUSED until an XML-signature validator is installed - signatures are never
skipped). Business logic only sees the resulting identity (username, organisation, role, MFA).

Sessions: every user token carries a session id; logout, disabling a user, SCIM deprovisioning and admin
revocation end sessions immediately on this instance and within ``SESSION_RECHECK_S`` on others.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import jwt
import structlog
from cryptography.fernet import Fernet, InvalidToken

from app.domain.tenancy.models import hash_secret
from app.domain.tenancy.permissions import PROVISIONABLE_ROLES, ROLES
from app.repositories.governance import IdentityProvider, MfaSeed, ScimToken, Session

log = structlog.get_logger("identity")

SESSION_RECHECK_S = 30.0
MAX_PENDING_OIDC = 10_000
MAX_SESSION_CACHE = 50_000
OIDC_STATE_TTL_S = 600
OIDC_ALGS = ("RS256", "RS384", "RS512", "PS256", "ES256", "ES384")
CLOCK_SKEW_S = 60
MFA_AMR = {"mfa", "otp", "hwk", "swk", "sms", "fpt", "face", "pin", "totp", "phr", "phrh"}
SCIM_PREFIX = "ldt_scim_"


class IdentityError(Exception):
    def __init__(self, code: str, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# ------------------------------------------------------------------------------- secrets
class SecretStore:
    """Provider-agnostic secret lookup: environment, then files in SECRETS_DIR (Docker / Kubernetes secrets,
    or an external secret manager's agent that renders files: Vault Agent, Key Vault CSI, AWS Secrets CSI)."""

    def __init__(self, secrets_dir: str | None = None) -> None:
        self._dir = Path(secrets_dir) if secrets_dir else None

    def get(self, name: str) -> str | None:
        if not name or not all(c.isalnum() or c in "_-." for c in name):
            return None
        v = os.environ.get(name)
        if v:
            return v
        if self._dir is not None:
            f = self._dir / name
            if f.is_file():
                return f.read_text(encoding="utf-8").strip() or None
        return None


# ------------------------------------------------------------------------------- TOTP (RFC 6238)
def totp(secret_b32: str, step: int, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


def totp_check(secret_b32: str, code: str, last_step: int, now: float | None = None) -> int | None:
    """-> matched step (> last_step: codes cannot be replayed) or None. Accepts +-1 step of clock skew."""
    if not code.isdigit() or len(code) != 6:
        return None
    step = int((now or time.time()) // 30)
    for s in (step - 1, step, step + 1):
        if s > last_step and hmac.compare_digest(totp(secret_b32, s), code):
            return s
    return None


@dataclass
class OidcPending:
    provider_id: str
    nonce: str
    verifier: str
    created: float
    return_to: str


@dataclass
class ExternalIdentity:
    provider_id: str
    org_id: str
    subject: str
    username: str
    email: str | None
    role: str | None  # mapped role (None: use membership / default)
    mfa: bool
    claims: dict[str, Any]


# Same-origin path only: starts with one "/", never "//"; the class has no backslash (browsers read "\"
# as "/", so "/\evil.com" would leave the site), no whitespace and no control characters.
_RETURN_TO = re.compile(r"^/(?!/)[A-Za-z0-9/._~%!$&'()*+,;=:@?#-]*$")


def safe_return_to(value: str) -> str:
    return value if len(value) <= 200 and _RETURN_TO.fullmatch(value) else "/"


class IdentityService:
    def __init__(
        self,
        settings: Any,
        repo: Any,
        secrets_store: SecretStore,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._s = settings
        self.repo = repo
        self.secrets = secrets_store
        self._transport = transport
        self._sessions: dict[str, tuple[Session, float]] = {}
        self.providers: dict[str, IdentityProvider] = {}  # loaded at start; maintained by the org API
        self._pending: dict[str, OidcPending] = {}
        self._discovery: dict[str, tuple[dict[str, Any], float]] = {}
        self._jwks: dict[str, tuple[dict[str, Any], float]] = {}
        key = secrets_store.get("DATA_ENCRYPTION_KEY")
        try:
            self._fernet: Fernet | None = Fernet(key.encode()) if key else None
        except (ValueError, TypeError):
            log.error("data_encryption_key_invalid")
            self._fernet = None

    async def load(self) -> None:
        self.providers = {p.provider_id: p for p in await self.repo.idps_for(None)}

    # ------------------------------------------------------------------ sessions
    async def open_session(
        self,
        username: str,
        org_id: str,
        method: str,
        mfa: bool,
        ttl_minutes: int,
        ip: str | None,
        user_agent: str | None,
    ) -> Session:
        now = datetime.now(UTC)
        s = Session(
            uuid.uuid4().hex,
            username,
            org_id,
            now,
            now + timedelta(minutes=ttl_minutes),
            method,
            mfa,
            now,
            ip=(ip or "")[:64] or None,
            user_agent=(user_agent or "")[:200] or None,
        )
        await self.repo.save_session(s)
        self._sessions[s.session_id] = (s, time.monotonic())
        return s

    async def session(self, session_id: str) -> Session | None:
        """Active session or None (revoked / expired / unknown)."""
        cached = self._sessions.get(session_id)
        if cached is None or time.monotonic() - cached[1] > SESSION_RECHECK_S:
            s: Session | None = await self.repo.get_session(session_id)
            if s is None:
                self._sessions.pop(session_id, None)
                return None
            self._sessions[session_id] = (s, time.monotonic())
        else:
            s = cached[0]
        now = datetime.now(UTC)
        if s.revoked_at is not None or s.expires_at <= now:
            self._sessions.pop(session_id, None)  # never valid again: do not keep it cached
            return None
        if len(self._sessions) > MAX_SESSION_CACHE:  # bound: drop entries due for a re-check anyway
            stale = time.monotonic() - SESSION_RECHECK_S
            for k in [k for k, (_, t) in self._sessions.items() if t < stale]:
                del self._sessions[k]
        if s.last_seen_at is None or (now - s.last_seen_at).total_seconds() > 60:
            s.last_seen_at = now
            try:
                await self.repo.save_session(s)
            except Exception as exc:  # last-seen is informational
                log.debug("session_touch_failed", error=str(exc)[:120])
        return s

    async def revoke(self, s: Session, reason: str) -> None:
        if s.revoked_at is None:
            s.revoked_at, s.revoked_reason = datetime.now(UTC), reason[:120]
            await self.repo.save_session(s)
        self._sessions[s.session_id] = (s, time.monotonic())

    async def revoke_user(self, username: str, reason: str, org_id: str | None = None) -> int:
        n = 0
        for s in await self.repo.sessions_of(username):
            if (
                s.revoked_at is None
                and s.expires_at > datetime.now(UTC)
                and (org_id is None or s.org_id == org_id)
            ):
                await self.revoke(s, reason)
                n += 1
        return n

    # ------------------------------------------------------------------ local MFA (TOTP)
    def mfa_available(self) -> bool:
        return self._fernet is not None

    async def totp_begin(self, username: str) -> tuple[str, str]:
        """-> (base32 secret, otpauth URI) shown once; stored encrypted, not yet enabled."""
        if self._fernet is None:
            raise IdentityError(
                "MFA_UNAVAILABLE", "MFA is not configured on this server (DATA_ENCRYPTION_KEY)", 503
            )
        existing = await self.repo.get_mfa(username)
        if existing is not None and existing.enabled:
            raise IdentityError("MFA_ALREADY_ENABLED", "An authenticator is already enrolled", 409)
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        await self.repo.save_mfa(
            MfaSeed(username, self._fernet.encrypt(secret.encode()).decode(), False, datetime.now(UTC))
        )
        uri = (
            "otpauth://totp/"
            + urlencode({"": f"LaptopDigitalTwin:{username}"})[1:]
            + "?"
            + urlencode(
                {
                    "secret": secret,
                    "issuer": "LaptopDigitalTwin",
                    "algorithm": "SHA1",
                    "digits": 6,
                    "period": 30,
                }
            )
        )
        return secret, uri

    async def totp_verify(self, username: str, code: str, enable: bool = False) -> bool:
        m = await self.repo.get_mfa(username)
        if m is None or self._fernet is None or (not m.enabled and not enable):
            return False
        try:
            secret = self._fernet.decrypt(m.secret_enc.encode()).decode()
        except InvalidToken:
            log.error("mfa_seed_decrypt_failed", username=username)
            return False
        step = totp_check(secret, code, m.last_step)
        if step is None:
            return False
        m.last_step = step
        m.enabled = m.enabled or enable
        await self.repo.save_mfa(m)
        return True

    async def mfa_enabled(self, username: str) -> bool:
        m = await self.repo.get_mfa(username)
        return bool(m and m.enabled)

    # ------------------------------------------------------------------ providers
    @staticmethod
    def check_provider_config(kind: str, config: dict[str, Any], allow_http: bool) -> dict[str, Any]:
        """Validate an identity-provider configuration (no secrets inside: client secrets are references)."""
        allowed = {
            "oidc": {
                "issuer",
                "client_id",
                "client_secret_ref",
                "redirect_uri",
                "scopes",
                "jit_provisioning",
                "default_role",
                "role_claim",
                "role_mapping",
                "require_mfa",
                "allowed_domains",
            },
            "saml": {
                "entity_id",
                "acs_url",
                "idp_entity_id",
                "idp_sso_url",
                "idp_certificate",
                "attribute_map",
                "default_role",
                "jit_provisioning",
            },
        }.get(kind)
        if allowed is None:
            raise IdentityError("INVALID_PROVIDER", "kind must be oidc or saml", 422)
        unknown = set(config) - allowed
        if unknown:
            raise IdentityError("INVALID_PROVIDER", f"unknown settings: {sorted(unknown)}", 422)
        if any(k.lower().endswith(("secret", "password", "key")) for k in config):
            raise IdentityError("INVALID_PROVIDER", "secrets must be given as secret references", 422)

        def url_ok(u: Any) -> bool:
            p = urlparse(str(u))
            return p.scheme == "https" or (allow_http and p.scheme == "http")

        if kind == "oidc":
            for k in ("issuer", "client_id", "redirect_uri"):
                if not config.get(k):
                    raise IdentityError("INVALID_PROVIDER", f"{k} is required", 422)
            if not url_ok(config["issuer"]) or not url_ok(config["redirect_uri"]):
                raise IdentityError("INVALID_PROVIDER", "issuer and redirect_uri must be https URLs", 422)
        default_role = config.get("default_role")
        if default_role is not None and default_role not in PROVISIONABLE_ROLES:
            raise IdentityError("INVALID_PROVIDER", "default_role must be a non-administrative role", 422)
        for role in (config.get("role_mapping") or {}).values():
            if role not in ROLES:
                raise IdentityError("INVALID_PROVIDER", f"unknown role {role} in role_mapping", 422)
        return config

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10.0, transport=self._transport, follow_redirects=False)

    async def _discover(self, issuer: str) -> dict[str, Any]:
        cached = self._discovery.get(issuer)
        if cached and time.monotonic() - cached[1] < 3600:
            return cached[0]
        async with self._client() as c:
            r = await c.get(issuer.rstrip("/") + "/.well-known/openid-configuration")
        if r.status_code != 200:
            raise IdentityError("OIDC_DISCOVERY_FAILED", "identity provider discovery failed", 502)
        doc: dict[str, Any] = r.json()
        if doc.get("issuer") != issuer:
            raise IdentityError(
                "OIDC_ISSUER_MISMATCH", "discovery issuer does not match the configured issuer", 502
            )
        self._discovery[issuer] = (doc, time.monotonic())
        return doc

    async def _jwk(self, jwks_uri: str, kid: str | None) -> Any:
        for attempt in (0, 1):  # refresh once on an unknown kid (key rotation at the IdP)
            cached = self._jwks.get(jwks_uri)
            if cached is None or attempt == 1 or time.monotonic() - cached[1] > 3600:
                async with self._client() as c:
                    r = await c.get(jwks_uri)
                if r.status_code != 200:
                    raise IdentityError("OIDC_JWKS_FAILED", "could not fetch signing keys", 502)
                cached = (r.json(), time.monotonic())
                self._jwks[jwks_uri] = cached
            for k in cached[0].get("keys", []):
                if kid is None or k.get("kid") == kid:
                    return jwt.PyJWK(k).key
        raise IdentityError("OIDC_UNKNOWN_KEY", "token signed with an unknown key", 401)

    async def oidc_begin(self, p: IdentityProvider, return_to: str = "/") -> str:
        cfg = p.config
        doc = await self._discover(cfg["issuer"])
        state, nonce, verifier = (
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(48),
        )
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self._gc_pending()
        if len(self._pending) >= MAX_PENDING_OIDC:  # unauthenticated endpoint: bound the memory it can use
            raise IdentityError("OIDC_BUSY", "Too many sign-ins in progress; try again shortly", 503)
        self._pending[state] = OidcPending(
            p.provider_id,
            nonce,
            verifier,
            time.monotonic(),
            safe_return_to(return_to),
        )
        q = {
            "response_type": "code",
            "client_id": cfg["client_id"],
            "redirect_uri": cfg["redirect_uri"],
            "scope": " ".join(cfg.get("scopes") or ["openid", "email", "profile"]),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{doc['authorization_endpoint']}?{urlencode(q)}"

    def _gc_pending(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._pending.items() if now - v.created > OIDC_STATE_TTL_S]:
            self._pending.pop(k, None)

    async def oidc_complete(
        self, providers: dict[str, IdentityProvider], state: str, code: str
    ) -> tuple[ExternalIdentity, str]:
        self._gc_pending()
        pending = self._pending.pop(state, None)  # single use
        if pending is None:
            raise IdentityError("OIDC_STATE_INVALID", "sign-in request expired or invalid (state)", 401)
        p = providers.get(pending.provider_id)
        if p is None or p.status != "ACTIVE":
            raise IdentityError("OIDC_PROVIDER_DISABLED", "identity provider not available", 401)
        cfg = p.config
        doc = await self._discover(cfg["issuer"])
        secret = (
            self.secrets.get(cfg.get("client_secret_ref") or "") if cfg.get("client_secret_ref") else None
        )
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg["redirect_uri"],
            "client_id": cfg["client_id"],
            "code_verifier": pending.verifier,
        }
        if secret:
            form["client_secret"] = secret
        async with self._client() as c:
            r = await c.post(doc["token_endpoint"], data=form, headers={"Accept": "application/json"})
        if r.status_code != 200 or "id_token" not in r.json():
            raise IdentityError("OIDC_TOKEN_FAILED", "the identity provider rejected the sign-in", 401)
        claims = await self.validate_id_token(r.json()["id_token"], cfg, doc, pending.nonce)
        return self._map(p, claims), pending.return_to

    async def validate_id_token(
        self, token: str, cfg: dict[str, Any], doc: dict[str, Any], nonce: str
    ) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise IdentityError("OIDC_TOKEN_INVALID", "malformed ID token", 401) from exc
        alg = header.get("alg")
        if alg not in OIDC_ALGS:  # never "none", never HS* with a public key
            raise IdentityError("OIDC_TOKEN_INVALID", f"unsupported signing algorithm {alg}", 401)
        key = await self._jwk(doc["jwks_uri"], header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=cfg["client_id"],
                issuer=cfg["issuer"],
                leeway=CLOCK_SKEW_S,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise IdentityError("OIDC_TOKEN_EXPIRED", "ID token expired", 401) from exc
        except jwt.PyJWTError as exc:
            raise IdentityError(
                "OIDC_TOKEN_INVALID", f"ID token rejected: {type(exc).__name__}", 401
            ) from exc
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise IdentityError("OIDC_NONCE_MISMATCH", "ID token nonce mismatch (replay?)", 401)
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != cfg["client_id"]:
            raise IdentityError("OIDC_TOKEN_INVALID", "authorized party mismatch", 401)
        return claims

    def _map(self, p: IdentityProvider, claims: dict[str, Any]) -> ExternalIdentity:
        cfg = p.config
        email = claims.get("email") if claims.get("email_verified") is True else None
        domains = cfg.get("allowed_domains") or []
        if domains and (
            email is None or email.rsplit("@", 1)[-1].lower() not in {d.lower() for d in domains}
        ):
            raise IdentityError("OIDC_DOMAIN_DENIED", "account domain not allowed for this organization", 403)
        sub = str(claims["sub"])
        username = (email or f"{p.provider_id[:8]}_{hashlib.sha256(sub.encode()).hexdigest()[:16]}").lower()[
            :64
        ]
        role = None
        mapping = cfg.get("role_mapping") or {}
        values = claims.get(cfg.get("role_claim") or "groups") or []
        for v in values if isinstance(values, list) else [values]:
            r = mapping.get(str(v))
            if r is not None and (role is None or _level(r) > _level(role)):
                role = r
        amr = {str(a).lower() for a in (claims.get("amr") or [])}
        return ExternalIdentity(
            p.provider_id, p.org_id, sub, username, email, role, bool(amr & MFA_AMR), claims
        )

    # ------------------------------------------------------------------ SCIM tokens
    async def create_scim_token(self, org_id: str, label: str, by: str) -> tuple[str, ScimToken]:
        secret = SCIM_PREFIX + secrets.token_urlsafe(32)
        t = ScimToken(uuid.uuid4().hex, org_id, hash_secret(secret), label[:120], by, datetime.now(UTC))
        await self.repo.save_scim(t)
        return secret, t

    async def scim_org(self, bearer: str | None) -> str | None:
        if not bearer or not bearer.startswith(SCIM_PREFIX):
            return None
        t = await self.repo.scim_by_hash(hash_secret(bearer))
        return t.org_id if t is not None and t.revoked_at is None else None


def _level(role: str) -> int:
    from app.domain.tenancy.permissions import ROLE_LEVEL

    return ROLE_LEVEL.get(role, -1)
