"""Phase-9 identity APIs: session-aware sign-in (local + MFA), logout, sessions, TOTP enrolment, OIDC sign-in,
SAML service-provider endpoints (assertions refused until a signature validator is installed) and the
organisation switcher.

Every sign-in opens a server-side session (revocable); the token carries its id, the organisation, the
authentication time and whether MFA was used. Failures are audited and rate limited per client.
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, Reader, require_recent_auth
from app.core.security import AuthError, Principal
from app.domain.tenancy.models import DEFAULT_ORG
from app.domain.tenancy.permissions import LEGACY_RANK, ROLE_LABELS
from app.services.admin import ConflictError
from app.services.identity import IdentityError
from app.services.tenancy import TenancyError

router = APIRouter(prefix="/auth", tags=["auth"])


def _err(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _accounts_only(container: Any) -> None:
    if container.settings.auth_mode.value != "accounts":
        raise _err(409, "ACCOUNTS_DISABLED", "User accounts are used only when AUTH_MODE=accounts")


async def sign_in(
    container: Any,
    request: Request,
    username: str,
    org_id: str | None,
    method: str,
    mfa: bool,
    mfa_capable: bool,
) -> dict[str, Any]:
    """Open a session for an authenticated user (local or external) and issue its token."""
    try:
        ctx = container.tenancy.context(username, org_id)
    except TenancyError as exc:
        raise _err(exc.status, exc.code, str(exc)) from exc
    sec = container.policies.effective("security", ctx.org_id)[0]
    scope = None
    if method == "local" and not sec["local_login_allowed"] and not ctx.platform_admin:
        raise _err(403, "LOCAL_LOGIN_DISABLED", "This organization signs in through its identity provider")
    if sec["mfa"] in ("MFA_REQUIRED", "MFA_PROVIDER_MANAGED") and not mfa:
        if method != "local" or sec["mfa"] == "MFA_PROVIDER_MANAGED":
            raise _err(403, "MFA_REQUIRED", "Multi-factor authentication is required by your organization")
        if mfa_capable:
            raise _err(401, "MFA_CODE_REQUIRED", "Enter the code from your authenticator app")
        scope = "mfa_setup"  # may only enroll an authenticator, then sign in again
    session = await container.identity.open_session(
        username,
        ctx.org_id,
        method,
        mfa,
        int(sec["session_ttl_minutes"]),
        _ip(request),
        request.headers.get("user-agent"),
    )
    token, expires = container.auth.issue_user_jwt(
        username,
        container.tenancy.legacy_role(ctx),
        session_id=session.session_id,
        org_id=ctx.org_id,
        mfa=mfa,
        ttl_minutes=int(sec["session_ttl_minutes"]),
        scope=scope,
    )
    user = await container.admin.get_user(username)
    if user is not None:
        await container.admin.mark_login(user)
    container.audit.record(
        ctx.org_id,
        username,
        "user",
        "auth.login",
        "authentication",
        ip=_ip(request),
        metadata={"method": method, "mfa": mfa, "session_id": session.session_id, "scope": scope},
    )
    return {
        "user": user.public() if user else {"username": username},
        "access_token": token,
        "expires_at": expires.isoformat(),
        "organization_id": ctx.org_id,
        "mfa": mfa,
        "mfa_setup_required": scope == "mfa_setup",
    }


def _fail(container: Any, request: Request, username: str, reason: str) -> None:
    from app.core.metrics import AUTH_FAILURES

    AUTH_FAILURES.labels("password" if "password" in reason else "mfa").inc()
    org = next((m.org_id for m in container.tenancy.memberships(username)), None)
    container.audit.record(
        org,
        username[:64],
        "user",
        "auth.login_failed",
        "authentication",
        result="FAILURE",
        reason=reason,
        severity="WARNING",
        ip=_ip(request),
    )


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    otp: str | None = Field(default=None, max_length=8)
    organization_id: str | None = Field(default=None, max_length=64)


@router.post("/login", summary="Sign in (local account; MFA when enrolled or required)")
async def login(body: LoginIn, container: ContainerDep, request: Request) -> dict[str, Any]:
    _accounts_only(container)
    if not container.login_limiter.allow(_ip(request) or "unknown")[0]:
        raise _err(429, "RATE_LIMITED", "Too many sign-in attempts")
    try:
        user = await container.admin.verify_credentials(body.username, body.password)
    except AuthError as exc:
        _fail(container, request, body.username, "invalid password or unknown user")
        raise _err(401, "INVALID_CREDENTIALS", "Invalid username or password") from exc
    enrolled = await container.identity.mfa_enabled(user.username)
    mfa = False
    if enrolled:
        if not body.otp:
            raise _err(401, "MFA_CODE_REQUIRED", "Enter the code from your authenticator app")
        if not container.mfa_limiter.allow(user.username)[0] or not await container.identity.totp_verify(
            user.username, body.otp
        ):
            _fail(container, request, user.username, "invalid MFA code")
            raise _err(401, "MFA_CODE_INVALID", "Invalid or reused authenticator code")
        mfa = True
    return await sign_in(container, request, user.username, body.organization_id, "local", mfa, enrolled)


@router.post("/logout", summary="End the current session")
async def logout(principal: Reader, container: ContainerDep) -> dict[str, Any]:
    if principal.session_id:
        s = await container.identity.session(principal.session_id)
        if s is not None:
            await container.identity.revoke(s, "logout")
        container.audit.record(principal.org_id, principal.subject, "user", "auth.logout", "authentication")
    return {"ok": True}


class SwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: str = Field(max_length=64)


@router.post("/switch-organization", summary="Continue in another organization you are a member of")
async def switch_org(
    body: SwitchIn, principal: Reader, container: ContainerDep, request: Request
) -> dict[str, Any]:
    _accounts_only(container)
    out = await sign_in(
        container, request, principal.subject, body.organization_id, "switch", principal.mfa, False
    )
    if principal.session_id:
        s = await container.identity.session(principal.session_id)
        if s is not None:
            await container.identity.revoke(s, "organization switch")
    return out


@router.get("/me", summary="Who is signed in, the current organization and what they may do")
async def me(principal: Reader, container: ContainerDep) -> dict[str, Any]:
    mode = container.settings.auth_mode.value
    display = {"anonymous": "Local operator", "api_key": "API key holder", "jwt": "API key session"}
    t = container.tenancy
    orgs = [
        {"org_id": m.org_id, "name": t.orgs[m.org_id].name, "role": m.role, "role_label": ROLE_LABELS[m.role]}
        for m in t.memberships(principal.subject)
    ]
    if principal.platform_admin:
        known = {o["org_id"] for o in orgs}
        orgs += [
            {
                "org_id": o.org_id,
                "name": o.name,
                "role": "platform_admin",
                "role_label": "Platform Super Admin",
            }
            for o in t.orgs.values()
            if o.org_id not in known and o.status.value == "ACTIVE"
        ]
    org = t.orgs.get(principal.org_id)
    return {
        "username": principal.subject,
        "display_name": principal.subject if principal.method == "account" else display.get(principal.method),
        "role": principal.role,
        "method": principal.method,
        "auth_mode": mode,
        "can_operate": principal.has_role("operator"),
        "can_admin": principal.has_role("admin"),
        "organization": {"org_id": principal.org_id, "name": org.name if org else principal.org_id},
        "org_role": principal.org_role,
        "org_role_label": ROLE_LABELS.get(
            principal.org_role or "", "Platform Super Admin" if principal.platform_admin else "Service"
        ),
        "platform_admin": principal.platform_admin,
        "permissions": sorted(principal.permissions),
        "organizations": orgs,
        "mfa": principal.mfa,
        "mfa_enrolled": await container.identity.mfa_enabled(principal.subject)
        if principal.method == "account"
        else False,
        "mfa_setup_required": principal.token_scope == "mfa_setup",  # noqa: S105
        "session_id": principal.session_id,
    }


@router.get("/sessions", summary="My sessions (current and recent)")
async def my_sessions(principal: Reader, container: ContainerDep) -> dict[str, Any]:
    items = await container.identity.repo.sessions_of(principal.subject)
    return {"items": [dict(s.public(), current=s.session_id == principal.session_id) for s in items[:50]]}


@router.post("/sessions/{session_id}/revoke", summary="End one of my sessions")
async def revoke_session(session_id: str, principal: Reader, container: ContainerDep) -> dict[str, Any]:
    s = await container.identity.repo.get_session(session_id[:64])
    if s is None or s.username != principal.subject:
        raise _err(404, "SESSION_NOT_FOUND", "Unknown session")
    await container.identity.revoke(s, "revoked by user")
    container.audit.record(
        principal.org_id,
        principal.subject,
        "user",
        "auth.session_revoked",
        "authentication",
        resource_type="session",
        resource_id=s.session_id,
    )
    return {"ok": True}


# --------------------------------------------------------------------------------- MFA (TOTP)
class CodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=6, max_length=8)


@router.post("/mfa/totp/setup", summary="Start authenticator enrolment (secret shown once)")
async def totp_setup(principal: Reader, container: ContainerDep) -> dict[str, Any]:
    _accounts_only(container)
    try:
        secret, uri = await container.identity.totp_begin(principal.subject)
    except IdentityError as exc:
        raise _err(exc.status, exc.code, str(exc)) from exc
    return {
        "secret": secret,
        "otpauth_uri": uri,
        "note": "Add it to an authenticator app, then confirm a code.",
    }


@router.post("/mfa/totp/activate", summary="Confirm a code to turn MFA on (sign in again afterwards)")
async def totp_activate(body: CodeIn, principal: Reader, container: ContainerDep) -> dict[str, Any]:
    if not container.mfa_limiter.allow(principal.subject)[0]:
        raise _err(429, "RATE_LIMITED", "Too many attempts")
    if not await container.identity.totp_verify(principal.subject, body.code, enable=True):
        raise _err(401, "MFA_CODE_INVALID", "Invalid code")
    await container.identity.revoke_user(principal.subject, "mfa enrolled")  # sign in again with MFA
    container.audit.record(principal.org_id, principal.subject, "user", "auth.mfa_enrolled", "authentication")
    return {"ok": True, "sign_in_again": True}


# --------------------------------------------------------------------------------- OIDC / SAML
def _providers(container: Any) -> dict[str, Any]:
    providers: dict[str, Any] = container.identity.providers
    return providers


@router.get("/providers", summary="Sign-in providers of one organization (public; no enumeration)")
async def providers(
    container: ContainerDep, organization: str = Query(min_length=2, max_length=64)
) -> dict[str, Any]:
    """Only for a known organisation id (tenant-specific sign-in page); never lists other organisations."""
    items = [
        {"provider_id": p.provider_id, "name": p.name, "kind": p.kind}
        for p in _providers(container).values()
        if p.status == "ACTIVE" and p.org_id == organization
    ]
    return {"items": items}


@router.get("/oidc/{provider_id}/login", summary="Start OIDC sign-in (authorization code + PKCE)")
async def oidc_login(
    provider_id: str, container: ContainerDep, return_to: str = Query(default="/", max_length=200)
) -> Response:
    if not container.settings.feature_oidc:
        raise _err(404, "FEATURE_DISABLED", "OIDC sign-in is disabled")
    p = _providers(container).get(provider_id[:64])
    if p is None or p.kind != "oidc" or p.status != "ACTIVE":
        raise _err(404, "PROVIDER_NOT_FOUND", "Unknown identity provider")
    try:
        url = await container.identity.oidc_begin(p, return_to)
    except IdentityError as exc:
        raise _err(exc.status, exc.code, str(exc)) from exc
    return RedirectResponse(url, status_code=302)


@router.get("/oidc/callback", summary="OIDC redirect URI")
async def oidc_callback(
    container: ContainerDep,
    request: Request,
    state: str = Query(max_length=200),
    code: str = Query(max_length=4096),
) -> Response:
    try:
        ident, return_to = await container.identity.oidc_complete(_providers(container), state, code)
    except IdentityError as exc:
        from app.core.metrics import AUTH_FAILURES

        AUTH_FAILURES.labels("oidc").inc()
        container.audit.record(
            None,
            "anonymous",
            "anonymous",
            "auth.oidc_failed",
            "authentication",
            result="FAILURE",
            reason=exc.code,
            severity="WARNING",
            ip=_ip(request),
            source="oidc",
        )
        raise _err(exc.status, exc.code, str(exc)) from exc
    username = await provision_external(container, ident)
    out = await sign_in(container, request, username, ident.org_id, "oidc", ident.mfa, False)
    # the SPA reads the token from the fragment (never sent to a server, not logged by proxies)
    return RedirectResponse(f"{return_to}#access_token={out['access_token']}", status_code=302)


async def provision_external(container: Any, ident: Any) -> str:
    """Map an external identity to a platform user + membership (JIT only when the provider allows it)."""
    t, p = container.tenancy, _providers(container)[ident.provider_id]
    user = await container.admin.get_user(ident.username)
    member = t.member(ident.org_id, ident.username)
    if user is None or member is None:
        if not p.config.get("jit_provisioning"):
            raise _err(403, "NOT_PROVISIONED", "Your account has not been provisioned for this organization")
        if user is None:
            with contextlib.suppress(ConflictError):
                await container.admin.create_external_user(ident.username)
        role = ident.role or p.config.get("default_role") or "read_only"
        await t.set_member(None, ident.org_id, ident.username, role, source=p.kind)
        container.audit.record(
            ident.org_id,
            ident.username,
            "user",
            "user.provisioned",
            "user",
            resource_type="user",
            resource_id=ident.username,
            metadata={"provider": p.provider_id, "role": role},
        )
    elif member.status != "ACTIVE":
        raise _err(403, "ACCOUNT_DISABLED", "Your membership in this organization is disabled")
    elif ident.role and ident.role != member.role and member.source == p.kind:
        await t.set_member(
            None, ident.org_id, ident.username, ident.role, member.group_scope
        )  # IdP-managed role
        container.audit.record(
            ident.org_id,
            ident.username,
            "user",
            "user.role_changed",
            "user",
            resource_type="user",
            resource_id=ident.username,
            metadata={"role": ident.role, "previous_role": member.role, "source": p.kind},
        )
    return str(ident.username)


@router.get("/saml/{provider_id}/metadata", summary="SAML service-provider metadata")
async def saml_metadata(provider_id: str, container: ContainerDep) -> Response:
    p = _providers(container).get(provider_id[:64])
    if p is None or p.kind != "saml" or not container.settings.feature_saml:
        raise _err(404, "PROVIDER_NOT_FOUND", "Unknown identity provider")
    from xml.sax.saxutils import quoteattr

    xml = (
        f'<?xml version="1.0"?><md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        f"entityID={quoteattr(str(p.config.get('entity_id', '')))}><md:SPSSODescriptor "
        f'AuthnRequestsSigned="false" WantAssertionsSigned="true" '
        f'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol"><md:AssertionConsumerService '
        f'Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f"Location={quoteattr(str(p.config.get('acs_url', '')))} "
        f'index="0"/></md:SPSSODescriptor></md:EntityDescriptor>'
    )
    return Response(xml, media_type="application/samlmetadata+xml")


@router.post("/saml/{provider_id}/acs", summary="SAML assertion consumer (refused: validator not installed)")
async def saml_acs(provider_id: str, container: ContainerDep, request: Request) -> Response:
    container.audit.record(
        None,
        "anonymous",
        "anonymous",
        "auth.saml_refused",
        "authentication",
        result="FAILURE",
        reason="SAML assertion validation is not installed",
        severity="WARNING",
        ip=_ip(request),
        source="saml",
    )
    raise _err(
        501,
        "SAML_VALIDATOR_UNAVAILABLE",
        "SAML assertions cannot be accepted: XML-signature validation is not installed on this server. "
        "Use OIDC, or install the SAML validator (see docs/security/identity.md).",
    )


# --------------------------------------------------------------------------------- setup
class SetupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    setup_token: str | None = Field(default=None, max_length=256)


@router.post("/setup", summary="Create the first administrator (only while no account exists)")
async def setup(body: SetupIn, container: ContainerDep, request: Request) -> dict[str, Any]:
    _accounts_only(container)
    try:
        user = await container.admin.setup(body.username, body.password, body.setup_token)
    except (AuthError, ConflictError, ValueError) as exc:
        code = 409 if isinstance(exc, ConflictError) else 401 if isinstance(exc, AuthError) else 422
        raise _err(code, "SETUP_FAILED", str(exc)) from exc
    # the first account owns the default organisation and administers the platform
    await container.tenancy.set_member(None, DEFAULT_ORG, user.username, "org_owner")
    await container.tenancy.set_platform_admin(user.username, True)
    container.audit.record(
        DEFAULT_ORG,
        user.username,
        "user",
        "user.setup",
        "user",
        resource_type="user",
        resource_id=user.username,
        metadata={"platform_admin": True},
    )
    return await sign_in(container, request, user.username, DEFAULT_ORG, "local", False, False)


def recent_auth_guard(principal: Principal, container: Any) -> None:
    require_recent_auth(principal, container)


__all__ = ["LEGACY_RANK", "provision_external", "router", "sign_in"]
