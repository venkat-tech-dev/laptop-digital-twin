# Phase 8: Safe remediation and human-approved actions

> AI can recommend. Policy can authorise. Humans can approve. The Action Catalog defines what is possible.
> The endpoint agent independently validates. Telemetry verifies the result.

Phase 8 is **controlled remediation**, not remote administration:
- No API, model output or setting can carry a command, script, executable path, URL or registry operation.
- An action is a catalog id plus strongly typed parameters.
- The endpoint checks it again against its own allowlist before anything runs.

## Flow

```
Diagnosis (Phase 7) / agent-health alert (Phase 6) / user request
  -> recommendation (deterministic mapping; AI suggestions validated like user input)
  -> catalog + parameter schema + allowlist + policy (mode, kill switch, circuit breaker)
  -> PENDING_APPROVAL (approval notification via Phase 6)      [or APPROVED by explicit low-risk policy]
  -> approve: permission + risk ceiling + four-eyes + approval still valid + diagnosis unchanged
  -> QUEUED (immediate / scheduled / maintenance window)
  -> worker (2 s tick): expiry, preconditions, per-device lock, fleet limit
  -> VALIDATING: Ed25519-signed envelope, pulled by the agent (device token only)
  -> agent: signature, device, expiry, local allowlist, parameters, replay, idempotency, local preconditions
  -> EXECUTING -> agent reports completed / failed / rejected
  -> VERIFYING: telemetry checks -> SUCCEEDED | PARTIALLY_SUCCEEDED | FAILED
  -> remediation.* events, twin "remediation.*", timeline, metrics, hash-chained audit
```

| Layer | Code |
|---|---|
| Action catalog, known applications | `backend/app/domain/remediation/catalog.py` |
| Record, lifecycle, audit hash | `backend/app/domain/remediation/models.py` |
| Policy, permissions, four-eyes, kill switches, maintenance windows | `backend/app/domain/remediation/policy.py` |
| Signed envelopes | `backend/app/domain/remediation/envelope.py` |
| Verification | `backend/app/domain/remediation/verification.py` |
| Recommendations (rules, untrusted AI) | `backend/app/domain/remediation/recommend.py` |
| Service: queue, worker, preconditions, agent protocol | `backend/app/services/remediation.py` |
| Storage (migration 0011) | `backend/app/repositories/remediation.py` |
| API | `backend/app/api/v1/remediation.py` |
| Agent: verification, ledger, key pin, executor, app restart | `agent/app/remediation/` |
| UI | `frontend/src/pages/RemediationPage.tsx`, `frontend/src/remediation/` |

## Action catalog

| Action | Risk | Changes the device | Rollback | Status |
|---|---|---|---|---|
| `REFRESH_TELEMETRY`: collect every sensor now, send a full snapshot | LOW | no | not needed | enabled; may be auto-approved |
| `REQUEST_SYSTEM_RESCAN`: re-run inventory discovery | LOW | no | not needed | enabled; may be auto-approved |
| `RECONNECT_AGENT`: drop and reopen platform connections | LOW | no | not needed | enabled; may be auto-approved |
| `RESTART_KNOWN_APPLICATION(application_id)`: graceful close (WM_CLOSE), then relaunch from the process's own program file with no arguments; never force-kills | MEDIUM | yes | **not available** | enabled; always needs approval |
| `RESTART_AGENT` | MEDIUM | yes | — | **disabled**: needs a supervisor (service recovery); `RECONNECT_AGENT` covers connectivity |
| `RESTART_KNOWN_WINDOWS_SERVICE` | HIGH | yes | — | **disabled**: needs administrator rights; the agent stays least-privileged |
| `CLEAR_APPLICATION_CACHE` | MEDIUM | yes | — | **disabled**: irreversible deletion; cache locations are version-specific |
| `CLEAN_KNOWN_TEMPORARY_DATA` | MEDIUM | yes | — | **disabled**: irreversible; Windows Storage Sense covers it under the user's control |

Each action defines:
- id and version, permission and supported OS;
- the minimum agent version (1.6.0);
- a parameter schema (`extra="forbid"`);
- preconditions;
- validation, execution and verification timeouts;
- verification checks, rollback strategy and cooldown;
- a per-day limit, impact text, estimated duration and lock group.

Known applications (Teams, OneDrive, Slack, Zoom) are listed by registry id with plain image names. Administrators may add more through policy (`remediation.manage_actions`); paths are rejected.

## Approval, policy and roles

**Modes:**
- `MANUAL_APPROVAL` is the default for LOW, MEDIUM and HIGH.
- CRITICAL is `DISABLED` unless explicitly enabled per action.
- `AUTO_APPROVE_LOW_RISK` applies only when all of these hold: auto-remediation is enabled, the action is LOW risk, it changes nothing, it is marked auto-eligible, and diagnosis confidence is at least `auto_min_confidence` (0.75). Otherwise it falls back to manual approval.
- Overrides resolve device first, then device group (workspace), then action, then risk.

**Four-eyes:** the requester may not approve their own request at or above `four_eyes_min_risk` (default HIGH, configurable). System-proposed actions have requester `system`.

**Roles (server-side):**

| Role | Permissions |
|---|---|
| Viewer | View |
| Employee | View and request LOW actions, own assigned devices only |
| Operator | View, request, approve (up to MEDIUM), execute (dry runs), cancel |
| Admin | Everything, including approving up to CRITICAL, `manage_policy` and `manage_actions` |

**Expiry:**
- An approval is valid for `approval_ttl_s` (30 min, counted from the scheduled start for scheduled actions).
- An action not started by then expires, as do pending approvals.
- A newer diagnosis version of a different type invalidates pending or approved actions.

**Offline devices:** approved actions wait while the device is offline. Once it has been offline longer than `offline_reapproval_s` (1 h), a MEDIUM or higher action needs a fresh approval.

**Kill switches:**
- global (also `REMEDIATION_KILL_SWITCH` in the environment, which the API cannot turn off);
- tenant, device group, action type and device.

While one is on, no new execution begins and approvals are refused. Actions already running on a device finish and are verified; they are never interrupted midway.

**Loop prevention:**
- per-action cooldown;
- per-action and per-device daily budgets;
- one action in flight per device;
- a fleet-wide in-flight limit (20);
- a circuit breaker: 3 failures of the same action on the same device within 24 h stops further proposals for 24 h and raises an alert.

Cooldowns and budgets count only attempts that actually ran; a device refusal does not count.

## Endpoint execution

The agent pulls envelopes over its device-token channel. An enrollment key can never pull or report actions. For each envelope it checks, in order:
1. Structure, with no unexpected fields.
2. Ed25519 signature against the **pinned** platform key.
3. Target device.
4. Expiry: not expired, not issued in the future, validity of at most 1 h.
5. Action and version against the agent's own `IMPLEMENTED` set and the **local allowlist** `AGENT_REMEDIATION_ACTIONS`. The default is the three no-change actions; application restart must be added locally.
6. Parameters: for restarts, the application must be on `AGENT_RESTARTABLE_APPLICATIONS`.
7. Replay: a nonce belongs to exactly one execution.
8. **Idempotency:** a known `execution_id` returns its earlier result and never runs again. The ledger is on disk, so this survives agent restarts. An execution interrupted mid-run is reported as failed and is not retried automatically.
9. Local preconditions. For a restart: the agent runs in the user session (never in the service's session 0), and the application is running in that session.

Execution runs under a timeout, with one action per device at a time.

The platform key is pinned on first use, or set explicitly with `AGENT_ACTION_PUBLIC_KEY`. A different key offered later is **not** accepted; rotating the key is a deliberate act.

The agent advertises its local allowlist in its heartbeat, so the platform never dispatches beyond it.

## Verification

Success is decided from telemetry, never from the agent saying "done". The checks:

| Check | Kind | Passes when |
|---|---|---|
| `agent_completed` | hard | the agent executed the action |
| `fresh_telemetry` | hard | telemetry arrived after completion |
| `application_running` | hard | the agent reports the application running again, or a complete process snapshot shows it |
| `target_improved` | soft | the 1-minute CPU mean drops below 70 %, or by 25 % or more, within 5 min |

If CPU was already below the target before the action, the check says so ("nothing to improve") rather than claiming an improvement.

**Outcomes:**
- Every check passes: SUCCEEDED.
- A hard check fails: FAILED.
- At the verification timeout, hard checks passed but the soft one did not: PARTIALLY_SUCCEEDED.

## Rollback

None of the enabled actions can be undone, and the catalog, API and UI say so:
- The LOW actions are `NOT_NEEDED`, because nothing on the device changes.
- `RESTART_KNOWN_APPLICATION` is `NOT_AVAILABLE`.

The `ROLLED_BACK` and `ROLLBACK_FAILED` statuses exist for future reversible actions.

## Dry run

- `POST /remediations/{id}/dry-run` evaluates every precondition live and describes the effect. It changes nothing.
- `POST /remediations` with `dry_run: true` sends a signed dry-run envelope. The agent performs every validation and reports what it *would* do. No approval is needed, because nothing changes.

## AI integration

The Phase 7 prompt lists the allowed action ids and application ids in the trusted system prompt (`diag-p2`).

The model may return one `suggested_action`, which is treated as untrusted input:
1. Validation checks its structure, and parameter values may not contain forbidden wording.
2. `recommend.from_ai` checks catalog membership, that the action is enabled, the parameter schema and the application allowlist.
3. Policy and approval then apply as for any other request.

AI-only suggestions get a discounted action confidence and can never bypass the catalog or approval.

Three numbers are kept separate and shown separately:
- **Diagnosis confidence:** how sure the platform is of the cause.
- **Action confidence:** diagnosis confidence × how well this action addresses that cause.
- **Expected success probability:** the action's verified success history, smoothed with a conservative prior.

## Storage (migration 0011)

**`remediations`:**
- Scalar columns plus a JSON body.
- Unique `execution_id`.
- Indexes on (device, created), status, (tenant, status), (action, created), diagnosis, alert and correlation.
- No foreign key to `devices`, so history outlives a removed device.

**`remediation_audit`:**
- Append-only and hash-chained: each row stores the previous row's hash, and its own hash covers its content.
- A PostgreSQL trigger rejects UPDATE and DELETE.
- Writers serialise on an advisory lock.
- Retention never deletes audit rows.
- `GET /remediation-admin/audit/verify` recomputes the chain.

## API

| Method | Path |
|---|---|
| GET | `/api/v1/remediations` (filters: device, status, action, risk, requester, diagnosis, since/until) |
| GET | `/api/v1/remediations/{id}`, `/execution`, `/verification` |
| POST | `/api/v1/remediations` (request a catalog action; `dry_run`; `mode` IMMEDIATE / SCHEDULED / MAINTENANCE_WINDOW) |
| POST | `/api/v1/remediations/{id}/approve`, `/reject`, `/cancel`, `/dry-run` |
| GET | `/api/v1/action-catalog`, `/action-catalog/{action_id}` |
| GET / PUT | `/api/v1/remediation-policy`; POST `/remediation-policy/kill-switch` |
| GET | `/api/v1/remediation-admin/status`, `/remediation-admin/audit/verify` |
| Agent | GET `/api/v1/agent/actions/key`, GET `/api/v1/agent/actions`, POST `/api/v1/agent/actions/{execution_id}/report` |

**WebSocket** (device topic): `remediation.proposed`, `approval_required`, `approved`, `rejected`, `queued`, `started`, `progress`, `verifying`, `succeeded`, `failed`, `cancelled`, `expired`, `rolled_back` and `circuit_open`. Clients refetch over REST on reconnect.

**Twin:** `remediation.status` (NONE, PROPOSED, PENDING_APPROVAL, EXECUTING, VERIFYING, SUCCEEDED or FAILED), `remediation.pending_count` and `remediation.latest`. These never replace observed state. Timeline entries use the `remediation.*` types.

**Notifications** (Phase 6): "Approval required" (closed by any decision), "Remediation failed", and "stopped after repeated failures".

## Configuration

**Backend:**

| Setting | Meaning |
|---|---|
| `REMEDIATION_ENABLED` | turns the remediation engine on or off |
| `REMEDIATION_SIGNING_KEY` | base64 Ed25519 private key, environment only. Without it nothing can be dispatched: fail closed |
| `REMEDIATION_KILL_SWITCH` | global kill switch the API cannot turn off |

**Agent:**

| Setting | Meaning |
|---|---|
| `AGENT_REMEDIATION_ENABLED` | turns action handling on or off |
| `AGENT_REMEDIATION_ACTIONS` | local allowlist; default: the three no-change actions |
| `AGENT_RESTARTABLE_APPLICATIONS` | for example `slack.desktop` or `vendor.app=app.exe` |
| `AGENT_ACTION_PUBLIC_KEY` | explicit key pin |
| `AGENT_ACTION_POLL_INTERVAL_S` | how often the agent pulls envelopes |

## Measured

From `scripts/remediation_bench.py`; results are in `docs/remediation-bench-results.json`.

**In-process engine** (fleet limit 20):

| Devices | Proposals/s | Approvals/s | Tick p50 | Tick max | Max in flight | Result |
|---|---|---|---|---|---|---|
| 100 | ~6,100 | ~1,700 | 6 ms | — | 20 | all succeeded |
| 1,000 | ~6,000 | ~3,550 | 8 ms | — | 20 | all succeeded |
| 10,000 | ~3,600 | ~3,700 | 18 ms | 0.6 s | 20 | all succeeded; 237 MB peak |

Before indexing, a tick at 1,000 devices took 0.96 s (quadratic scans).

**PostgreSQL** (isolated database): about 18 proposals/s and 22 approvals/s per process. One transaction per decision, with Docker-for-Windows round trips. Approvals are human-paced, and proposals are bounded by diagnosis volume.

**Real agent, end to end:** refresh, rescan and dry run all succeeded. A real graceful restart of a test application (`charmap.exe`) also succeeded, after a backend restart in between:
- The first attempt was correctly refused by the device (the application was not running).
- WM_CLOSE closed the original process and it was relaunched with a new pid.
- All four verification checks passed.
