# Release, deployment and rollback

## 1. Environments

| Environment | What it is | Data |
|---|---|---|
| development | `scripts/dev.ps1` (processes) or `docker compose up` on a workstation | the developer's own laptop |
| test / CI | `.github/workflows/ci.yml`: service containers, throwaway database | synthetic |
| isolated load / drill | backend on 8020/8021, database `ldt_test` or `ldt_load`, Redis db 1 (see `scripts/failover_drill.py`) | synthetic |
| staging | **not provisioned.** Recommended: the same compose file on a separate host with a restored copy of production | restored copy |
| production | `docker compose` on one host (one active backend; a second backend may run as standby) | real |

## 2. Quality gates (all block a release)

`backend` (format, lint, types, unit tests incl. tenant isolation, Phase 9 security and Phase 10
reliability tests) · `backend-integration` (migrations from empty, one-step downgrade and upgrade,
integration tests incl. leader election) · `agent` (lint, types, tests, dependency audit) · `frontend`
(lint, types, tests, build) · `security` (pip-audit, npm audit for high/critical, secret scan, Prometheus
rule check, compose validation) · `containers` (both images build). The image vulnerability scan
(Trivy) is **non-blocking** until the current baseline is triaged; it is reported, not ignored.

A skipped job is not a passed job. Status on 2026-10-09: the workflow file exists and its YAML
structure was validated locally. **It has never run**: the git repository has no commits and no
remote. The same commands were run locally (section 6 of the Phase 10 report).

## 3. Versioned, immutable builds

1. Dependencies are constrained to tested versions: `backend/requirements.lock`,
   `agent/requirements.lock` (pip constraints) and `frontend/package-lock.json` (`npm ci`).
2. Build images tagged with the commit and a semantic version, e.g.
   `ldt-backend:1.10.0-<sha>`. Never deploy `latest`.
3. Record the image digests (`docker image inspect --format '{{.Id}}'`) in the release notes. Deploy by
   digest.
4. Release notes list: version, digests, migrations (expand or contract), configuration changes and
   rollback notes.

## 4. Database changes: expand and contract

* **Expand** (add tables, nullable columns, indexes): released with or before the code that uses them.
  The previous version keeps working against the new schema. Migrations 0012 and 0013 are expand-only.
* **Contract** (drop or rename, NOT NULL on existing columns): only in a later release, after no
  running version uses the old shape, and after a backup.
* Migrations run automatically at container start (`alembic upgrade head`). Therefore **no
  destructive (contract) migration may be merged without a release plan** that says when it runs and
  how to restore. Prefer a separate one-off job for contract steps.
* Every migration must pass "upgrade from empty" and "downgrade -1 / upgrade" in CI.

## 5. Health-gated deployment (single host)

```
backup  ->  pull/build new images  ->  start NEW backend as standby (second instance)
        ->  stop OLD backend gracefully (SIGTERM: flushes queues, releases the leader lock)
        ->  NEW becomes active within ~5 s  ->  check /health/ready = 200 and the SLO page
        ->  replace the frontend container  ->  watch error budget for 30 min
```

The active/standby election makes this a near-zero-downtime switch: the standby holds no state and
answers 503 `STANDBY` until it is active. Measured failover after a hard kill: 5.25 s
(`scripts/failover_drill.py`, 2026-10-09). With plain `docker compose up -d --build`, the backend is
replaced in place (about 10–30 s of 503 while it starts; agents queue and replay).

## 6. Rollback

1. Code: start the previous image digest (it runs against the expanded schema).
2. Database: migrations are expand-only, so no rollback is needed. If a contract migration ran,
   restore from the pre-release backup into a new database and switch (`backup-restore.md`).
   Do not run `alembic downgrade` on production as a reflex: downgrades drop the new tables' data.
3. Configuration: `.env` changes are part of the release notes; keep the previous file.
4. Verify readiness, the audit chain, and that agents are sending.

## 7. Windows agent releases

There is no remote update channel, and none may be added without the controls below. Agents are
updated by IT through their normal software distribution (Intune, SCCM, GPO) or manually.

**Fleet visibility (implemented):** agent version per device (heartbeat, else last batch), last
contact, credential expiry, enrollment method, compliance against the agent policy
(`minimum_version`, `recommended_version`, `deprecated_versions`, `blocked_versions`), and an
"outdated / legacy-enrolled" list on Fleet Operations → IT operations.

**Staged rollout procedure (policy-driven, no remote execution):**
1. Release: build the agent package, sign it (Authenticode) and publish its SHA-256 with the release
   notes. Only signed, integrity-verified packages may be distributed.
2. Pilot wave: a device group "agent-pilot" (5–10 % of devices, mixed hardware models) receives it
   through the software distribution tool.
3. Watch 48 h on the pilot group: `ldt_ingest_rejected_total`, collector failures
   (`ldt_agent_provider_failures`), crash/hang events of the agent, telemetry freshness. Use the fleet
   insights view: a burst correlated with `agent_version = <new>` is a stop signal.
4. Waves 2–4: 25 %, 50 %, 100 %, each after a clean observation window.
5. Rollback: redistribute the previous signed package. If a version is bad, add it to the
   organization's `blocked_versions` with `reject_blocked` (its telemetry is refused, which makes the
   problem visible) and to compliance.
6. When every device runs the new version, raise `minimum_version`.

Agent compatibility: the backend accepts telemetry schema 1.x from agents ≥ 1.4 (`minimum_version`
default). Agents < 1.7 cannot use enrollment tokens or rotate credentials, and keep the legacy key path
until upgraded. Agents < 1.8 delete batches on 202 and do not benefit from durable confirmation (crash
loss window). Recommended: set `recommended_version` to 1.8.0 in the agent policy.
