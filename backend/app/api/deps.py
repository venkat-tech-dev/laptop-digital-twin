from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from app.core.container import Container
from app.core.security import AuthError, Principal
from app.services.devices import CredentialStoreUnavailableError


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


MFA_SETUP_PATHS = ("/api/v1/auth/mfa/", "/api/v1/auth/me", "/api/v1/auth/logout")


def _deny(status_code: int, code: str, message: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message}, headers=headers)


async def resolve_principal(
    container: Container, principal: Principal, requested_org: str | None, path: str = ""
) -> Principal:
    """Authenticated principal -> principal with tenant context (organisation, role, permissions, scope).

    Accounts: the user must be active, the session open (logout / revocation / SCIM end it immediately), and
    the organisation one of the user's active memberships. The organisation comes from the
    ``X-Organization-Id`` header or the session; it is never trusted without a membership check.
    """
    tenancy = container.tenancy
    if principal.method != "account":
        org = requested_org or "default"
        if org != "default":  # service principals (no-auth / API keys) act in the default organisation only
            raise _deny(
                403, "ORGANIZATION_ACCESS_DENIED", "Service credentials are bound to the default organization"
            )
        ctx = tenancy.service_context("default", principal.subject)
        return Principal(
            principal.subject,
            principal.method,
            "admin",
            ctx.org_id,
            ctx.org_role,
            ctx.permissions,
            ctx.group_scope,
            False,
        )
    user = await container.admin.active_user(principal.subject)
    if user is None:  # disabled or deleted accounts stop immediately
        raise _deny(401, "ACCOUNT_DISABLED", "Account disabled or removed", {"WWW-Authenticate": "Bearer"})
    if not principal.session_id:
        raise _deny(401, "SESSION_REQUIRED", "Please sign in again", {"WWW-Authenticate": "Bearer"})
    session = await container.identity.session(principal.session_id)
    if session is None or session.username != user.username:
        raise _deny(
            401,
            "SESSION_REVOKED",
            "Session ended or revoked; please sign in again",
            {"WWW-Authenticate": "Bearer"},
        )
    from app.services.tenancy import TenancyError

    try:
        ctx = tenancy.context(user.username, requested_org or session.org_id)
    except TenancyError as exc:
        container.metrics_authz_denied("organization")
        raise _deny(exc.status, exc.code, str(exc)) from exc
    if principal.token_scope == "mfa_setup" and not path.startswith(MFA_SETUP_PATHS):  # noqa: S105
        raise _deny(403, "MFA_SETUP_REQUIRED", "Enroll an authenticator app before using the platform")
    return Principal(
        user.username,
        "account",
        tenancy.legacy_role(ctx),
        ctx.org_id,
        ctx.org_role,
        ctx.permissions,
        ctx.group_scope,
        ctx.platform_admin,
        session.session_id,
        principal.auth_time,
        session.mfa or principal.mfa,
        principal.token_scope,
    )


async def require_reader(
    request: Request,
    container: ContainerDep,
    x_api_key: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    x_organization_id: Annotated[str | None, Header(max_length=64)] = None,
) -> Principal:
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    try:
        principal = container.auth.authenticate(x_api_key, bearer)
    except AuthError as exc:
        container.note_auth_failure(request, "token", str(exc))
        raise _deny(401, "AUTHENTICATION_FAILED", str(exc), {"WWW-Authenticate": "Bearer"}) from exc
    principal = await resolve_principal(container, principal, x_organization_id, request.url.path)
    # tenant- and user-aware rate limits (noisy neighbour protection); agents have their own buckets
    ok, retry = container.tenancy.check_rate(principal.org_id, "api_requests_per_min")
    if ok and principal.method == "account":
        ok, retry = container.tenancy.check_rate(
            principal.org_id, "user_api_requests_per_min", principal.subject
        )
    if not ok:
        raise _deny(
            429,
            "RATE_LIMITED",
            "Too many requests for this organization or user",
            {"Retry-After": str(max(1, int(retry)))},
        )
    request.state.principal = principal
    return principal


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """Who is calling an agent endpoint: a registered device (token) or an enrollment-key holder."""

    device_id: str | None  # set when authenticated with a per-device token
    method: str  # "device_token" | "enrollment_key"

    def allows(self, device_id: str) -> bool:
        return self.device_id is None or self.device_id == device_id


async def require_agent(
    container: ContainerDep,
    x_agent_key: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    x_device_id: Annotated[str | None, Header()] = None,
) -> AgentIdentity:
    if authorization and authorization.lower().startswith("bearer ") and x_device_id:
        try:
            verified = await container.device_auth.verify(x_device_id, authorization[7:].strip())
        except CredentialStoreUnavailableError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Credential store unavailable; retry later",
                headers={"Retry-After": "10"},
            ) from exc
        if verified:
            return AgentIdentity(x_device_id, "device_token")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid, revoked or unknown device token")
    if container.auth.verify_agent_key(x_agent_key):
        if not container.settings.allow_enrollment_key_ingest:
            # Phase 2: an agent may only submit data for the device it was registered as.
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Per-device token required: register via POST /api/v1/agent/register",
            )
        return AgentIdentity(None, "enrollment_key")
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing agent credentials")


def require_enrollment_key(
    container: ContainerDep, x_agent_key: Annotated[str | None, Header()] = None
) -> None:
    if not container.auth.verify_agent_key(x_agent_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing X-Agent-Key (enrollment key)")


Agent = Annotated[AgentIdentity, Depends(require_agent)]


Reader = Annotated[Principal, Depends(require_reader)]


def require_operator(principal: Reader) -> Principal:
    if not principal.has_role("operator"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required")
    return principal


def require_admin(principal: Reader) -> Principal:
    """Phase 1-8 administration is platform-wide (alert policy, webhooks, agent settings, model files, user
    accounts ...). Since Phase 9 it is available to platform super-admins and administrators of the original
    ``default`` organisation only; other organisations administer themselves through the /org APIs."""
    if not principal.has_role("admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator role required")
    if principal.org_id != "default" and not principal.platform_admin:
        raise _deny(
            403,
            "PLATFORM_ADMIN_REQUIRED",
            "Platform-wide administration is not available to this organization",
        )
    return principal


def require_platform_scope(principal: Principal) -> None:
    """For platform-wide reads/writes reached through permission checks (e.g. global configuration)."""
    if principal.org_id != "default" and not principal.platform_admin:
        raise _deny(
            403,
            "PLATFORM_ADMIN_REQUIRED",
            "Platform-wide configuration is not available to this organization",
        )


Operator = Annotated[Principal, Depends(require_operator)]
Admin = Annotated[Principal, Depends(require_admin)]


def require_permission(*permissions: str) -> Any:
    """Dependency: the principal must hold every listed permission in its current organisation."""

    async def dep(principal: Reader, container: ContainerDep) -> Principal:
        missing = [p for p in permissions if not principal.can(p)]
        if missing:
            container.metrics_authz_denied(missing[0])
            container.audit.record(
                principal.org_id,
                principal.subject,
                "user",
                "security.authorization_denied",
                "authorization",
                result="DENIED",
                reason=f"missing {missing[0]}",
                severity="WARNING",
            )
            raise _deny(403, "PERMISSION_DENIED", f"Permission {missing[0]} required")
        return principal

    return Depends(dep)


def require_recent_auth(principal: Principal, container: Container) -> None:
    """Sensitive administration needs a recent sign-in (security policy ``reauth_minutes``)."""
    if principal.method != "account":
        return
    import time

    minutes = container.policies.value("security", "reauth_minutes", org_id=principal.org_id)
    if principal.auth_time is None or time.time() - principal.auth_time > minutes * 60:
        raise _deny(401, "REAUTHENTICATION_REQUIRED", f"Sign in again (within {minutes} min) to do this")
