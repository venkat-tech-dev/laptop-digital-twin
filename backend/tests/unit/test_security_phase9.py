# ruff: noqa: E501  (inline test payloads and fake credentials)
"""Phase 9 - security negative tests: authentication (JWT manipulation, expiry, revocation, rotation, MFA, OIDC),
authorization (escalation, mass assignment, scope), enrollment abuse, device lifecycle, policy governance,
quotas / noisy neighbours, SCIM deprovisioning, data deletion, error format and audit."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.identity import totp
from tests.unit.tenancy_helpers import (
    PASSWORD,
    World,
    accounts_app,
    bearer,
    build_world,
    enroll,
    login,
    telemetry,
)

SECRET = "x" * 40


@pytest.fixture
def w() -> Iterator[World]:
    with accounts_app() as c:
        yield build_world(c)


def ct(w: World) -> Any:
    return w.c.app.state.container  # type: ignore[attr-defined]


def add_member(w: World, owner: dict[str, str], username: str, role: str, **kw: Any) -> dict[str, str]:
    r = w.c.post(
        "/api/v1/org/members",
        json={"username": username, "password": PASSWORD, "role": role, **kw},
        headers=owner,
    )
    assert r.status_code == 201, r.text
    return login(w.c, username)


# ------------------------------------------------------------------ authentication
def test_jwt_manipulation_is_rejected(w: World) -> None:
    claims = jwt.decode(w.alice["Authorization"][7:], options={"verify_signature": False})
    forged = {**claims, "sub": "bob"}  # signature no longer matches
    tampered = (
        w.alice["Authorization"][7:].rsplit(".", 1)[0]
        + "."
        + jwt.encode(forged, "wrong-secret-" * 4, algorithm="HS256").rsplit(".", 1)[1]
    )
    none_alg = jwt.encode({**claims}, key="", algorithm="none")
    other_key = jwt.encode(claims, "another-secret-which-is-long-enough!!", algorithm="HS256")
    expired = jwt.encode({**claims, "exp": int(time.time()) - 10}, SECRET, algorithm="HS256")
    no_sid = jwt.encode({k: v for k, v in claims.items() if k != "sid"}, SECRET, algorithm="HS256")
    for token, code in (
        (tampered, "AUTHENTICATION_FAILED"),
        (none_alg, "AUTHENTICATION_FAILED"),
        (other_key, "AUTHENTICATION_FAILED"),
        (expired, "AUTHENTICATION_FAILED"),
        (no_sid, "SESSION_REQUIRED"),
    ):
        r = w.c.get("/api/v1/org/devices", headers=bearer(token))
        assert r.status_code == 401 and r.json()["code"] == code, (code, r.text)


def test_role_claim_in_token_is_not_trusted(w: World) -> None:
    emp = add_member(w, w.alice, "erin", "employee")
    claims = jwt.decode(emp["Authorization"][7:], options={"verify_signature": False})
    elevated = jwt.encode({**claims, "role": "admin"}, SECRET, algorithm="HS256")  # even correctly signed
    r = w.c.get("/api/v1/org/members", headers=bearer(elevated))
    assert r.status_code == 403  # permissions come from the membership, not from the token


def test_logout_and_revocation_end_sessions(w: World) -> None:
    tok = dict(w.alice)
    assert w.c.post("/api/v1/auth/logout", headers=tok).status_code == 200
    r = w.c.get("/api/v1/org/devices", headers=tok)
    assert r.status_code == 401 and r.json()["code"] == "SESSION_REVOKED"
    a2 = login(w.c, "alice")
    sessions = w.c.get("/api/v1/auth/sessions", headers=a2).json()["items"]
    assert any(s["revoked_reason"] == "logout" for s in sessions) and any(s["current"] for s in sessions)


def test_previous_jwt_secret_accepted_during_rotation() -> None:
    old = "o" * 40
    with accounts_app(JWT_SECRET="n" * 40, JWT_SECRET_PREVIOUS=old) as c:
        r = c.post("/api/v1/auth/setup", json={"username": "root", "password": PASSWORD})
        claims = jwt.decode(r.json()["access_token"], options={"verify_signature": False})
        old_signed = jwt.encode(claims, old, algorithm="HS256")
        assert c.get("/api/v1/auth/me", headers=bearer(old_signed)).status_code == 200
        assert (
            c.get(
                "/api/v1/auth/me", headers=bearer(jwt.encode(claims, "z" * 40, algorithm="HS256"))
            ).status_code
            == 401
        )


def test_recent_authentication_required_for_sensitive_admin(w: World) -> None:
    claims = jwt.decode(w.alice["Authorization"][7:], options={"verify_signature": False})
    stale = jwt.encode({**claims, "auth_time": int(time.time()) - 3600}, SECRET, algorithm="HS256")
    r = w.c.post(
        "/api/v1/org/members",
        json={"username": "frank", "password": PASSWORD, "role": "read_only"},
        headers=bearer(stale),
    )
    assert r.status_code == 401 and r.json()["code"] == "REAUTHENTICATION_REQUIRED"


def test_local_mfa_enrolment_login_and_replay(w: World) -> None:
    setup = w.c.post("/api/v1/auth/mfa/totp/setup", headers=w.alice).json()
    code = totp(setup["secret"], int(time.time() // 30))
    assert w.c.post("/api/v1/auth/mfa/totp/activate", json={"code": code}, headers=w.alice).json()[
        "sign_in_again"
    ]
    assert w.c.get("/api/v1/org/devices", headers=w.alice).status_code == 401  # sessions ended on enrolment
    r = w.c.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 401 and r.json()["code"] == "MFA_CODE_REQUIRED"
    r = w.c.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD, "otp": code})
    assert (
        r.status_code == 401 and r.json()["code"] == "MFA_CODE_INVALID"
    )  # the code used for enrolment is spent
    nxt = totp(setup["secret"], int(time.time() // 30) + 1)
    ok = w.c.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD, "otp": nxt})
    assert ok.status_code == 200 and ok.json()["mfa"] is True
    again = w.c.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD, "otp": nxt})
    assert again.status_code == 401  # replay


def test_mfa_required_policy_restricts_until_enrolled(w: World) -> None:
    p = w.c.post(
        "/api/v1/org/policies",
        json={"kind": "security", "scope_type": "organization", "body": {"mfa": "MFA_REQUIRED"}},
        headers=w.bob,
    ).json()
    assert (
        w.c.post(
            f"/api/v1/org/policies/{p['policy_id']}/publish", json={"version": p["version"]}, headers=w.bob
        ).status_code
        == 200
    )
    r = w.c.post("/api/v1/auth/login", json={"username": "bob", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["mfa_setup_required"] is True
    limited = bearer(r.json()["access_token"])
    blocked = w.c.get("/api/v1/org/devices", headers=limited)
    assert blocked.status_code == 403 and blocked.json()["code"] == "MFA_SETUP_REQUIRED"
    assert w.c.get("/api/v1/auth/me", headers=limited).json()["mfa_setup_required"] is True
    assert w.c.post("/api/v1/auth/mfa/totp/setup", headers=limited).status_code == 200


# ------------------------------------------------------------------ OIDC
class FakeIdP:
    ISS = "https://idp.example.test"

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": "k1", "alg": "RS256", "use": "sig"}]}
        self.id_token = ""

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.ISS,
                    "authorization_endpoint": f"{self.ISS}/authorize",
                    "token_endpoint": f"{self.ISS}/token",
                    "jwks_uri": f"{self.ISS}/jwks",
                },
            )
        if req.url.path == "/jwks":
            return httpx.Response(200, json=self.jwks)
        if req.url.path == "/token":
            form = parse_qs(req.content.decode())
            assert form["code_verifier"][0] and form["grant_type"] == ["authorization_code"]
            return httpx.Response(200, json={"id_token": self.id_token, "token_type": "Bearer"})
        return httpx.Response(404)

    def sign(self, claims: dict[str, Any], kid: str = "k1", key: Any = None, alg: str = "RS256") -> str:
        return jwt.encode(claims, key or self.key, algorithm=alg, headers={"kid": kid})


def _oidc_setup(w: World) -> tuple[FakeIdP, str]:
    idp = FakeIdP()
    ct(w).identity._transport = httpx.MockTransport(idp.handler)
    r = w.c.post(
        "/api/v1/org/identity-providers",
        json={
            "kind": "oidc",
            "name": "Acme SSO",
            "config": {
                "issuer": FakeIdP.ISS,
                "client_id": "ldt-acme",
                "redirect_uri": "https://ldt.example/api/v1/auth/oidc/callback",
                "jit_provisioning": True,
                "default_role": "read_only",
                "role_claim": "groups",
                "role_mapping": {"acme-it": "it_operator"},
            },
        },
        headers=w.alice,
    )
    assert r.status_code == 201, r.text
    return idp, r.json()["provider_id"]


def _begin(w: World, pid: str) -> dict[str, str]:
    r = w.c.get(f"/api/v1/auth/oidc/{pid}/login", follow_redirects=False)
    assert r.status_code == 302
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    assert (
        q["code_challenge_method"] == "S256" and len(q["code_challenge"]) >= 43 and q["state"] and q["nonce"]
    )
    return q


def _claims(q: dict[str, str], **over: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "iss": FakeIdP.ISS,
        "aud": "ldt-acme",
        "sub": "u-123",
        "email": "Grace@Acme.Test",
        "email_verified": True,
        "nonce": q["nonce"],
        "iat": now,
        "exp": now + 300,
        "amr": ["pwd", "mfa"],
        "groups": ["acme-it"],
    }
    base.update(over)
    return base


def test_oidc_happy_path_with_jit_and_role_mapping(w: World) -> None:
    idp, pid = _oidc_setup(w)
    q = _begin(w, pid)
    idp.id_token = idp.sign(_claims(q))
    r = w.c.get(f"/api/v1/auth/oidc/callback?state={q['state']}&code=abc", follow_redirects=False)
    assert r.status_code == 302, r.text
    token = r.headers["location"].split("#access_token=")[1]
    me = w.c.get("/api/v1/auth/me", headers=bearer(token)).json()
    assert me["username"] == "grace@acme.test" and me["organization"]["org_id"] == "acme"
    assert me["org_role"] == "it_operator" and me["mfa"] is True
    replay = w.c.get(f"/api/v1/auth/oidc/callback?state={q['state']}&code=abc", follow_redirects=False)
    assert replay.status_code == 401 and replay.json()["code"] == "OIDC_STATE_INVALID"  # state is single-use


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda q, idp: idp.sign(_claims(q, nonce="other")), "OIDC_NONCE_MISMATCH"),
        (lambda q, idp: idp.sign(_claims(q, aud="someone-else")), "OIDC_TOKEN_INVALID"),
        (lambda q, idp: idp.sign(_claims(q, iss="https://evil.test")), "OIDC_TOKEN_INVALID"),
        (
            lambda q, idp: idp.sign(_claims(q, exp=int(time.time()) - 600, iat=int(time.time()) - 900)),
            "OIDC_TOKEN_EXPIRED",
        ),
        (lambda q, idp: idp.sign(_claims(q), kid="unknown"), "OIDC_UNKNOWN_KEY"),
        (
            lambda q, idp: idp.sign(
                _claims(q), key=rsa.generate_private_key(public_exponent=65537, key_size=2048)
            ),
            "OIDC_TOKEN_INVALID",
        ),
        (lambda q, idp: jwt.encode(_claims(q), "", algorithm="none"), "OIDC_TOKEN_INVALID"),
        (
            lambda q, idp: jwt.encode(_claims(q), "shared-secret-guess-0123456789abcdef", algorithm="HS256"),
            "OIDC_TOKEN_INVALID",
        ),
    ],
)
def test_oidc_rejects_invalid_id_tokens(w: World, mutate: Any, code: str) -> None:
    idp, pid = _oidc_setup(w)
    q = _begin(w, pid)
    idp.id_token = mutate(q, idp)
    r = w.c.get(f"/api/v1/auth/oidc/callback?state={q['state']}&code=abc", follow_redirects=False)
    assert r.status_code == 401 and r.json()["code"] == code, r.text


def test_oidc_unknown_state_and_mfa_policy(w: World) -> None:
    idp, pid = _oidc_setup(w)
    assert w.c.get("/api/v1/auth/oidc/callback?state=forged&code=abc").json()["code"] == "OIDC_STATE_INVALID"
    p = w.c.post(
        "/api/v1/org/policies",
        json={"kind": "security", "scope_type": "organization", "body": {"mfa": "MFA_REQUIRED"}},
        headers=w.alice,
    ).json()
    w.c.post(
        f"/api/v1/org/policies/{p['policy_id']}/publish", json={"version": p["version"]}, headers=w.alice
    )
    q = _begin(w, pid)
    idp.id_token = idp.sign(_claims(q, amr=["pwd"]))
    r = w.c.get(f"/api/v1/auth/oidc/callback?state={q['state']}&code=abc", follow_redirects=False)
    assert r.status_code == 403 and r.json()["code"] == "MFA_REQUIRED"


def test_provider_config_cannot_hold_secrets_or_escalate(w: World) -> None:
    base = {"issuer": "https://idp.example.test", "client_id": "c", "redirect_uri": "https://x/cb"}
    assert (
        w.c.post(
            "/api/v1/org/identity-providers",
            json={"kind": "oidc", "name": "x", "config": {**base, "client_secret": "s"}},
            headers=w.alice,
        ).status_code
        == 422
    )
    assert (
        w.c.post(
            "/api/v1/org/identity-providers",
            json={"kind": "oidc", "name": "x", "config": {**base, "issuer": "http://plain"}},
            headers=w.alice,
        ).status_code
        == 422
    )
    assert (
        w.c.post(
            "/api/v1/org/identity-providers",
            json={"kind": "oidc", "name": "x", "config": {**base, "default_role": "org_owner"}},
            headers=w.alice,
        ).status_code
        == 422
    )
    sec = add_member(w, w.alice, "sam", "security_admin")
    r = w.c.post(
        "/api/v1/org/identity-providers",
        json={"kind": "oidc", "name": "x", "config": {**base, "role_mapping": {"g": "org_owner"}}},
        headers=sec,
    )
    assert r.status_code == 403 and r.json()["code"] == "ROLE_ESCALATION"


def test_saml_assertions_are_refused_not_skipped(w: World) -> None:
    r = w.c.post(
        "/api/v1/org/identity-providers",
        json={
            "kind": "saml",
            "name": "Acme SAML",
            "config": {
                "entity_id": "https://ldt.example/saml",
                "acs_url": "https://ldt.example/api/v1/auth/saml/x/acs",
            },
        },
        headers=w.alice,
    )
    assert r.status_code == 201
    pid = r.json()["provider_id"]
    assert "EntityDescriptor" in w.c.get(f"/api/v1/auth/saml/{pid}/metadata").text
    acs = w.c.post(f"/api/v1/auth/saml/{pid}/acs", data={"SAMLResponse": base64.b64encode(b"<x/>").decode()})
    assert acs.status_code == 501 and acs.json()["code"] == "SAML_VALIDATOR_UNAVAILABLE"


# ------------------------------------------------------------------ SCIM
def test_scim_provisioning_and_deprovisioning(w: World) -> None:
    tok = w.c.post("/api/v1/org/scim-tokens", json={"label": "okta"}, headers=w.alice).json()["token"]
    scim = {"Authorization": f"Bearer {tok}"}
    assert w.c.get("/scim/v2/Users", headers={"Authorization": "Bearer ldt_scim_forged"}).status_code == 401
    assert (
        w.c.post(
            "/scim/v2/Users", json={"userName": "hank", "roles": [{"value": "org_admin"}]}, headers=scim
        ).status_code
        == 400
    )
    assert (
        w.c.post(
            "/scim/v2/Users", json={"userName": "hank", "roles": [{"value": "read_only"}]}, headers=scim
        ).status_code
        == 201
    )
    users = w.c.get("/scim/v2/Users", headers=scim).json()
    names = {u["userName"] for u in users["Resources"]}
    assert "hank" in names and "bob" not in names  # only this organization
    # an existing member with a session is deprovisioned -> session ends immediately
    ivy = add_member(w, w.alice, "ivy", "analyst")
    assert w.c.get("/api/v1/org/devices", headers=ivy).status_code == 200
    r = w.c.patch(
        "/scim/v2/Users/ivy",
        json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
        headers=scim,
    )
    assert r.status_code == 200 and r.json()["active"] is False
    assert w.c.get("/api/v1/org/devices", headers=ivy).status_code == 401
    assert w.c.post("/api/v1/auth/login", json={"username": "ivy", "password": PASSWORD}).status_code == 403
    assert (
        w.c.patch(
            "/scim/v2/Users/alice",
            json={"Operations": [{"op": "replace", "path": "roles", "value": [{"value": "employee"}]}]},
            headers=scim,
        ).status_code
        == 403
    )


# ------------------------------------------------------------------ authorization
def test_privilege_escalation_and_mass_assignment(w: World) -> None:
    op = add_member(w, w.alice, "otto", "it_operator")
    r = w.c.put("/api/v1/org/members/otto", json={"role": "org_admin"}, headers=op)
    assert r.status_code == 403
    adm = add_member(w, w.alice, "ada", "it_admin")
    assert (
        w.c.post(
            "/api/v1/org/members",
            json={"username": "xavier1", "password": PASSWORD, "role": "employee"},
            headers=adm,
        ).status_code
        == 403
    )  # no user.manage
    oa = add_member(w, w.alice, "olga", "org_admin")
    r = w.c.post(
        "/api/v1/org/members",
        json={"username": "xavier2", "password": PASSWORD, "role": "org_owner"},
        headers=oa,
    )
    assert r.status_code == 403 and r.json()["code"] == "ROLE_ESCALATION"
    r = w.c.put("/api/v1/org/members/olga", json={"role": "org_owner"}, headers=oa)
    assert r.status_code == 403
    assert (
        w.c.post(
            "/api/v1/org/members",
            json={"username": "xavier3", "password": PASSWORD, "role": "employee", "platform_admin": True},
            headers=w.alice,
        ).status_code
        == 422
    )
    assert (
        w.c.put(
            "/api/v1/org/members/otto", json={"role": "employee", "org_id": "globex"}, headers=w.alice
        ).status_code
        == 422
    )
    assert w.c.put("/api/v1/org/members/alice", json={"role": "employee"}, headers=w.alice).status_code in (
        403,
        409,
    )  # last owner / self
    emp = add_member(w, w.alice, "emma", "employee")
    for path in (
        "/api/v1/org/members",
        "/api/v1/org/audit",
        "/api/v1/org/enrollment-tokens",
        "/api/v1/org/policies",
    ):
        assert w.c.get(path, headers=emp).status_code == 403, path


def test_group_scoped_operator_sees_only_its_groups(w: World) -> None:
    second = enroll(w.c, w.alice, "dev-acme-0002")
    g = w.c.post("/api/v1/org/groups", json={"name": "Finance"}, headers=w.alice).json()["group_id"]
    w.c.post(f"/api/v1/org/groups/{g}/members", json={"add": [second.device_id]}, headers=w.alice)
    scoped = add_member(w, w.alice, "fiona", "it_operator", group_scope=[g])
    ids = [d["device_id"] for d in w.c.get("/api/v1/org/devices", headers=scoped).json()["items"]]
    assert ids == [second.device_id]
    assert w.c.get(f"/api/v1/devices/{w.dev_a.device_id}/twin", headers=scoped).status_code == 404


def test_policy_governance(w: World) -> None:
    ro = add_member(w, w.alice, "rory", "read_only")
    assert (
        w.c.post(
            "/api/v1/org/policies",
            json={"kind": "agent", "scope_type": "organization", "body": {}},
            headers=ro,
        ).status_code
        == 403
    )
    adm = add_member(w, w.alice, "ian", "it_admin")
    assert (
        w.c.post(
            "/api/v1/org/policies",
            json={"kind": "security", "scope_type": "organization", "body": {"mfa": "MFA_REQUIRED"}},
            headers=adm,
        ).status_code
        == 403
    )
    bad = w.c.post(
        "/api/v1/org/policies",
        json={"kind": "agent", "scope_type": "organization", "body": {"telemetry_interval_ms": 5}},
        headers=w.alice,
    ).json()
    pub = w.c.post(
        f"/api/v1/org/policies/{bad['policy_id']}/publish", json={"version": bad["version"]}, headers=w.alice
    )
    assert pub.status_code == 422 and pub.json()["code"] == "POLICY_INVALID"
    # versioning + rollback keep history
    ok = w.c.post(
        "/api/v1/org/policies",
        json={"kind": "agent", "scope_type": "organization", "body": {"telemetry_interval_ms": 3000}},
        headers=w.alice,
    ).json()
    assert (
        ok["policy_id"] == bad["policy_id"] and ok["version"] == bad["version"]
    )  # the open draft was edited
    v1 = w.c.post(
        f"/api/v1/org/policies/{ok['policy_id']}/publish", json={"version": ok["version"]}, headers=w.alice
    ).json()
    d2 = w.c.post(
        "/api/v1/org/policies",
        json={"kind": "agent", "scope_type": "organization", "body": {"telemetry_interval_ms": 9000}},
        headers=w.alice,
    ).json()
    w.c.post(
        f"/api/v1/org/policies/{d2['policy_id']}/publish", json={"version": d2["version"]}, headers=w.alice
    )
    rb = w.c.post(
        f"/api/v1/org/policies/{ok['policy_id']}/rollback",
        json={"to_version": v1["version"]},
        headers=w.alice,
    ).json()
    assert rb["version"] == 3 and rb["body"] == {"telemetry_interval_ms": 3000}
    versions = [
        p
        for p in w.c.get("/api/v1/org/policies?kind=agent", headers=w.alice).json()["items"]
        if p["policy_id"] == ok["policy_id"]
    ]
    assert [p["status"] for p in sorted(versions, key=lambda p: p["version"])] == [
        "ARCHIVED",
        "ARCHIVED",
        "PUBLISHED",
    ]
    eff = w.c.get(
        f"/api/v1/org/policies/effective?kind=agent&device_id={w.dev_a.device_id}", headers=w.alice
    ).json()
    assert eff["values"]["telemetry_interval_ms"] == 3000 and eff["source"][
        "telemetry_interval_ms"
    ].startswith("organization")


def test_remediation_four_eyes_follows_organization_policy(w: World) -> None:
    p = w.c.post(
        "/api/v1/org/policies",
        json={"kind": "remediation", "scope_type": "organization", "body": {"four_eyes_min_risk": "LOW"}},
        headers=w.alice,
    ).json()
    assert (
        w.c.post(
            f"/api/v1/org/policies/{p['policy_id']}/publish", json={"version": p["version"]}, headers=w.alice
        ).status_code
        == 200
    )
    r = w.c.post(
        "/api/v1/remediations",
        json={"device_id": w.dev_a.device_id, "action_type": "REFRESH_TELEMETRY"},
        headers=w.alice,
    ).json()
    own = w.c.post(f"/api/v1/remediations/{r['id']}/approve", headers=w.alice)
    assert own.status_code == 403 and "four-eyes" in own.json()["detail"]["message"]
    other = add_member(w, w.alice, "omar", "it_operator")
    assert w.c.post(f"/api/v1/remediations/{r['id']}/approve", headers=other).status_code == 200
    sec = add_member(w, w.alice, "sid", "security_admin")
    r2 = w.c.post(
        "/api/v1/remediations",
        json={"device_id": w.dev_a.device_id, "action_type": "RECONNECT_AGENT"},
        headers=other,
    ).json()
    assert (
        w.c.post(f"/api/v1/remediations/{r2['id']}/approve", headers=sec).status_code == 403
    )  # security admin cannot


# ------------------------------------------------------------------ enrollment and devices
def test_enrollment_token_abuse(w: World) -> None:
    c = w.c
    bad = c.post(
        "/api/v1/agent/enroll",
        json={
            "enrollment_token": "ldt_enr_forged-token-value",
            "device_id": "dev-x-1",
            "agent_version": "1.6.0",
        },
    )
    assert bad.status_code == 401
    t = c.post("/api/v1/org/enrollment-tokens", json={"ttl_hours": 1}, headers=w.alice).json()
    assert (
        c.post(
            "/api/v1/agent/enroll",
            json={"enrollment_token": t["token"], "device_id": "dev-x-1", "agent_version": "1.6.0"},
        ).status_code
        == 200
    )
    reuse = c.post(
        "/api/v1/agent/enroll",
        json={"enrollment_token": t["token"], "device_id": "dev-x-2", "agent_version": "1.6.0"},
    )
    assert reuse.status_code == 401 and "already used" in reuse.json()["message"]
    t2 = c.post("/api/v1/org/enrollment-tokens", json={"ttl_hours": 1}, headers=w.alice).json()
    c.post(f"/api/v1/org/enrollment-tokens/{t2['token_id']}/revoke", headers=w.alice)
    assert (
        "revoked"
        in c.post(
            "/api/v1/agent/enroll",
            json={"enrollment_token": t2["token"], "device_id": "dev-x-3", "agent_version": "1.6.0"},
        ).json()["message"]
    )
    t3 = c.post("/api/v1/org/enrollment-tokens", json={"ttl_hours": 1}, headers=w.alice).json()
    ct(w).tenancy.repo.tokens[t3["token_id"]].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (
        "expired"
        in c.post(
            "/api/v1/agent/enroll",
            json={"enrollment_token": t3["token"], "device_id": "dev-x-4", "agent_version": "1.6.0"},
        ).json()["message"]
    )
    # a device of acme cannot be taken over with globex's token
    tb = c.post("/api/v1/org/enrollment-tokens", json={"ttl_hours": 1}, headers=w.bob).json()
    steal = c.post(
        "/api/v1/agent/enroll",
        json={"enrollment_token": tb["token"], "device_id": w.dev_a.device_id, "agent_version": "1.6.0"},
    )
    assert steal.status_code == 409 and steal.json()["code"] == "DEVICE_OWNED_ELSEWHERE"
    assert (
        c.post("/api/v1/org/enrollment-tokens", json={"ttl_hours": 500}, headers=w.alice).status_code == 422
    )
    assert (
        c.post(
            "/api/v1/org/enrollment-tokens", json={"ttl_hours": 1, "max_uses": 5}, headers=w.alice
        ).status_code
        == 422
    )  # policy


def test_device_lifecycle_enforcement(w: World) -> None:
    c, d = w.c, w.dev_a
    assert (
        c.post(
            f"/api/v1/org/devices/{d.device_id}/lifecycle", json={"to": "DISABLED"}, headers=w.alice
        ).status_code
        == 200
    )
    r = telemetry(c, d, 30.0, expect=403)
    assert r.json()["code"] == "DEVICE_DISABLED"
    c.post(f"/api/v1/org/devices/{d.device_id}/lifecycle", json={"to": "QUARANTINED"}, headers=w.alice)
    telemetry(c, d, 30.0)  # quarantined: telemetry still accepted ...
    rem = c.post(
        "/api/v1/remediations",
        json={"device_id": d.device_id, "action_type": "REFRESH_TELEMETRY"},
        headers=w.alice,
    ).json()
    otherop = add_member(w, w.alice, "quinn", "it_operator")
    c.post(f"/api/v1/remediations/{rem['id']}/approve", headers=otherop)
    c.portal.call(ct(w).remediation.tick, datetime.now(UTC))  # type: ignore[union-attr]
    after = c.get(f"/api/v1/remediations/{rem['id']}", headers=w.alice).json()
    assert (
        after["status"] == "FAILED" and "device_lifecycle" in after["failure_reason"]
    )  # ... but nothing may act on it
    assert (
        c.post(
            f"/api/v1/org/devices/{d.device_id}/lifecycle",
            json={"to": "REVOKED", "reason": "stolen"},
            headers=w.alice,
        ).status_code
        == 200
    )
    assert telemetry(c, d, 30.0, expect=401).status_code == 401  # credential revoked
    legacy = c.post(
        "/api/v1/agent/register",
        json={"device_id": d.device_id, "agent_version": "1.6.0"},
        headers={"X-Agent-Key": "test-agent-key-0123456789abcdef"},
    )
    assert legacy.status_code in (403, 409)  # the shared key can never bring it back
    assert (
        c.post(f"/api/v1/org/devices/{d.device_id}/lifecycle", json={"to": "ACTIVE"}, headers=w.alice).json()[
            "code"
        ]
        == "REENROLL_REQUIRED"
    )
    again = enroll(c, w.alice, d.device_id)  # re-enrollment with a new token
    assert c.get("/api/v1/org/devices?lifecycle=ACTIVE", headers=w.alice).json()["count"] >= 1
    assert again.headers != d.headers


def test_credential_rotation(w: World) -> None:
    c, d = w.c, w.dev_a
    r = c.post("/api/v1/agent/credentials/rotate", headers=d.headers)
    assert r.status_code == 200 and r.json()["credential_expires_at"]
    assert telemetry(c, d, 30.0, expect=401).status_code == 401  # old token stops working
    d.headers = {"Authorization": f"Bearer {r.json()['device_token']}", "X-Device-Id": d.device_id}
    telemetry(c, d, 30.0)


def test_blocked_agent_versions_and_compliance(w: World) -> None:
    p = w.c.post(
        "/api/v1/org/policies",
        json={
            "kind": "agent",
            "scope_type": "organization",
            "body": {"blocked_versions": ["1.6.0"], "recommended_version": "1.7.0", "reject_blocked": True},
        },
        headers=w.alice,
    ).json()
    val = w.c.post(f"/api/v1/org/policies/{p['policy_id']}/validate", headers=w.alice).json()
    assert val["preview"]["devices_on_blocked_versions"] == 1 and val["warnings"]
    w.c.post(
        f"/api/v1/org/policies/{p['policy_id']}/publish", json={"version": p["version"]}, headers=w.alice
    )
    assert telemetry(w.c, w.dev_a, 30.0, expect=403).json()["code"] == "AGENT_VERSION_BLOCKED"
    comp = w.c.get(f"/api/v1/org/devices/{w.dev_a.device_id}/compliance", headers=w.alice).json()
    assert comp["status"] == "NON_COMPLIANT" and any("blocked" in r for r in comp["reasons"])
    telemetry(w.c, w.dev_b, 30.0)  # globex unaffected


# ------------------------------------------------------------------ quotas / noisy neighbour
def test_noisy_tenant_is_throttled_and_others_are_not(w: World) -> None:
    r = w.c.patch(
        "/api/v1/platform/organizations/acme",
        json={
            "quotas": {
                "telemetry_batches_per_min": {"limit": 3, "mode": "THROTTLE"},
                "api_requests_per_min": {"limit": 5, "mode": "THROTTLE"},
            }
        },
        headers=w.root,
    )
    assert r.status_code == 200, r.text
    codes = []
    for _ in range(6):
        w.dev_a.seq += 1
        body = {
            "device_id": w.dev_a.device_id,
            "agent_version": "1.6.0",
            "sequence": w.dev_a.seq,
            "sent_at": datetime.now(UTC).isoformat(),
            "samples": [],
        }
        r = w.c.post("/api/v1/ingest/telemetry", json=body, headers=w.dev_a.headers)
        codes.append(r.status_code)
    assert codes.count(429) >= 3 and codes[-1] == 429 and 202 not in codes[codes.index(429) :], codes
    assert r.json()["code"] == "TENANT_TELEMETRY_QUOTA" and r.headers.get("Retry-After")
    for _ in range(5):
        telemetry(w.c, w.dev_b, 30.0)  # globex keeps flowing
    statuses = [w.c.get("/api/v1/org/devices", headers=w.alice).status_code for _ in range(8)]
    assert 429 in statuses
    assert all(w.c.get("/api/v1/org/devices", headers=w.bob).status_code == 200 for _ in range(8))
    usage = w.c.get("/api/v1/org/usage", headers=w.bob).json()
    assert usage["quotas"]["telemetry_batches_per_min"]["rejected_total"] == 0


def test_device_quota_rejects_enrollment(w: World) -> None:
    w.c.patch(
        "/api/v1/platform/organizations/acme",
        json={"quotas": {"max_devices": {"limit": 1, "mode": "REJECT"}}},
        headers=w.root,
    )
    t = w.c.post("/api/v1/org/enrollment-tokens", json={"ttl_hours": 1}, headers=w.alice).json()
    r = w.c.post(
        "/api/v1/agent/enroll",
        json={"enrollment_token": t["token"], "device_id": "dev-acme-0009", "agent_version": "1.6.0"},
    )
    assert r.status_code == 401 and "quota" in r.json()["message"]


# ------------------------------------------------------------------ governance
def test_data_deletion_workflow(w: World) -> None:
    c, d = w.c, w.dev_a
    assert (
        c.post(
            f"/api/v1/org/devices/{d.device_id}/data-deletion",
            json={"confirm_device_id": d.device_id, "reason": "gdpr"},
            headers=w.alice,
        ).status_code
        == 409
    )
    c.post(f"/api/v1/org/devices/{d.device_id}/lifecycle", json={"to": "RETIRED"}, headers=w.alice)
    assert (
        c.post(
            f"/api/v1/org/devices/{d.device_id}/data-deletion",
            json={"confirm_device_id": "nope", "reason": "gdpr"},
            headers=w.alice,
        ).status_code
        == 422
    )
    r = c.post(
        f"/api/v1/org/devices/{d.device_id}/data-deletion",
        json={"confirm_device_id": d.device_id, "reason": "gdpr"},
        headers=w.alice,
    )
    assert r.status_code == 202
    for _ in range(50):
        c.portal.call(asyncio.sleep, 0.02)  # type: ignore[union-attr,arg-type]
        if ct(w).governance_jobs.jobs[r.json()["job_id"]]["status"] == "DONE":
            break
    assert ct(w).tenancy.lifecycle(d.device_id).value == "DECOMMISSIONED"
    c.portal.call(ct(w).audit.flush)  # type: ignore[union-attr]
    actions = [e["action"] for e in c.get("/api/v1/org/audit?category=data", headers=w.alice).json()["items"]]
    assert "data.deletion_requested" in actions and "data.deleted" in actions


def test_audit_records_security_events_and_is_tamper_evident(w: World) -> None:
    w.c.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong"})
    w.c.get("/api/v1/devices/dev-globex-0001/twin", headers=w.alice)  # cross-tenant probe
    c = ct(w)
    w.c.portal.call(c.audit.flush)  # type: ignore[union-attr]
    mine = w.c.get("/api/v1/org/audit?limit=500", headers=w.alice).json()["items"]
    assert any(e["action"] == "auth.login_failed" for e in mine)
    assert not any(e["action"] == "security.cross_tenant_attempt" for e in mine)  # no inference channel
    platform = w.c.get("/api/v1/platform/audit?action=security.", headers=w.root).json()["items"]
    assert any(e["action"] == "security.cross_tenant_attempt" for e in platform)
    assert w.c.get("/api/v1/org/audit/verify", headers=w.alice).json()["ok"] is True
    c.audit.repo.audit[0].actor_id = "mallory"
    assert w.c.get("/api/v1/org/audit/verify", headers=w.alice).json()["ok"] is False


def test_audit_export_neutralises_formula_injection(w: World) -> None:
    w.c.post("/api/v1/org/groups", json={"name": '=HYPERLINK("http://evil")'}, headers=w.alice)
    w.c.portal.call(ct(w).audit.flush)  # type: ignore[union-attr]
    csv = w.c.get("/api/v1/org/audit/export?fmt=csv", headers=w.alice).text
    assert "=HYPERLINK" not in csv.replace("'=HYPERLINK", "")


def test_error_body_has_code_and_request_id(w: World) -> None:
    r = w.c.get("/api/v1/remediations/unknown-id", headers=w.alice)
    body = r.json()
    assert r.status_code == 404 and body["code"] and body["message"] and body["request_id"]
    assert r.headers["X-Request-ID"] == body["request_id"]
    assert r.headers["Permissions-Policy"].startswith("camera=()")


@pytest.mark.parametrize(
    "target", ["//evil.example", r"/\evil.example", "https://evil.example", "/x\r\nSet-Cookie: a=b", " /x"]
)
def test_oidc_return_to_cannot_leave_the_site(w: World, target: str) -> None:
    idp, pid = _oidc_setup(w)
    r = w.c.get(f"/api/v1/auth/oidc/{pid}/login", params={"return_to": target}, follow_redirects=False)
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    idp.id_token = idp.sign(_claims(q))
    cb = w.c.get(f"/api/v1/auth/oidc/callback?state={q['state']}&code=abc", follow_redirects=False)
    assert cb.status_code == 302 and cb.headers["location"].startswith("/#access_token=")
