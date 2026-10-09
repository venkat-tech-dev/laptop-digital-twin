# Dependency and container review (Phase 9)

## 1. New dependencies in Phase 9

**None.** Phase 9 was built on libraries already in use:

| Need | Implemented with |
|---|---|
| TOTP (RFC 6238) | standard library (`hmac`, `hashlib`, `struct`): about 30 lines, tested against the RFC test vector |
| Encryption of MFA seeds | `cryptography` (Fernet), already present for Phase 8 Ed25519 signatures |
| OIDC (discovery, code exchange, JWKS, ID-token validation) | `httpx` + `PyJWT` (RSA/EC via `cryptography`) |
| SCIM | FastAPI routes; no SCIM library |
| Hash-chained audit | `hashlib` |
| Backup / restore | `pg_dump` / `pg_restore` of the existing Postgres image |

Considered and not added: a SAML library. The available options (`python3-saml`/`xmlsec`) bring native
XML-signature dependencies. Rather than accept unvalidated assertions, the ACS endpoint refuses them
until a validator is approved.

## 2. Inventory (installed versions, 2026-10-09)

Backend (Python 3.12): fastapi 0.142.2, starlette 1.7.0, uvicorn 0.54.0, pydantic 2.13.5,
pydantic-settings 2.15.0, SQLAlchemy 2.1.3, asyncpg 0.31.0, alembic 1.20.0, redis 8.1.0,
structlog 26.1.0, httpx 0.28.1, prometheus-client, PyJWT 2.15.1, cryptography 50.0.2, tzdata.

Agent (Python, Windows): psutil, pywin32, httpx, pydantic, pydantic-settings, structlog, cryptography.

Frontend: react 19.3.0, three 0.186.1, @react-three/fiber 9.8.1, @react-three/drei 10.7.9,
recharts 3.10.1, zustand 5.0.15, @fontsource fonts. Build: vite 8.3.3, typescript 6.0.3.

`pyproject.toml` files use lower bounds (`>=`). The Docker image resolves the newest compatible versions
at build time, and `frontend/package-lock.json` pins the frontend. Recommendation: add a Python lock file
(`pip-compile` or `uv lock`) so that images are reproducible and reviewed before upgrade.

## 3. Vulnerability scanning

Not run from this environment: `pip-audit` and `npm audit` send the dependency list to external
advisory services. Run them in CI or on a workstation:

```
cd backend && pip install pip-audit && pip-audit
cd agent   && pip-audit
cd frontend && npm audit --omit=dev
```

Treat high and critical findings in runtime dependencies (not dev tooling) as release blockers.

## 4. Containers

| Item | Status |
|---|---|
| Backend base image | `python:3.12-slim`; runs as non-root `ldt` (uid 10001); `exec` so Python is PID 1 (graceful shutdown) |
| Frontend base image | build on `node:22-alpine`, serve with `nginxinc/nginx-unprivileged:1.27-alpine` (non-root, port 8080) |
| Privileges (Phase 9) | backend and frontend: `no-new-privileges`, `cap_drop: ALL`, `mem_limit` (1 GiB / 128 MiB), `pids_limit` |
| Ports | all published on 127.0.0.1 only |
| Postgres / Redis | official images; default capabilities (needed for initialisation); Redis without a password, loopback only (threat model RR4) |
| Image pinning | by tag, not digest; rebuild monthly and pin digests for production |
| Secrets | from `.env` / `SECRETS_DIR`; not baked into images |
