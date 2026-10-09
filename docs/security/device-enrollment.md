# Device enrollment, credentials and lifecycle

## 1. Enrollment with a token (Phase 9, recommended)

1. An administrator with `device.enroll` creates a token (console → Organization → Devices &
   enrollment). The token can carry a label and a group to join. It is valid for 1 h to 7 days (capped by
   the enrollment policy `token_max_ttl_hours`, default 24) and is single-use (multi-use only if the
   policy allows it).
2. The token (`ldt_enr_…`, 256 bits) is shown **once**. The server stores its SHA-256 only.
3. On the laptop, put it in the agent's `.env` and start the agent:
   ```
   AGENT_BACKEND_URL=https://twin.example.com
   AGENT_ENROLLMENT_TOKEN=ldt_enr_...
   ```
4. The agent calls `POST /api/v1/agent/enroll` once. The server consumes the token atomically, assigns
   the device to the token's organization (and group), and returns a per-device credential with its
   expiry. The agent stores the credential with Windows DPAPI (`credentials.bin`) and never sends the
   enrollment token again. The token can then be removed from `.env`.

Refusals (all audited as `device.enrollment_failed`, generic message, 20 attempts/min per client):

| Case | Response |
|---|---|
| unknown, used, expired or revoked token | 401 `ENROLLMENT_FAILED` (reason in the message, never another organization's details) |
| device id already owned by another organization | 409 `DEVICE_OWNED_ELSEWHERE` |
| device quota of the organization reached | 401 with "quota" in the message |
| organization suspended | 403 |

## 2. Credentials

* Per-device bearer token, random, stored hashed. It is sent as `Authorization: Bearer …` with
  `X-Device-Id` and only works for that device.
* Expiry: the enrollment policy `credential_ttl_days` (default 90, 7 to 730). The heartbeat reports
  `credential_expires_at`.
* **Rotation:** the agent rotates on its own when fewer than `AGENT_CREDENTIAL_ROTATE_DAYS` (default 7)
  remain: `POST /api/v1/agent/credentials/rotate` with the current token returns a new one, and the old
  one stops working immediately. Rotation failures keep the current credential and are retried at the
  next heartbeat.
* An expired credential is refused (401). The device must be re-enrolled with a new token.

## 3. Legacy shared key (pre-Phase 9)

Devices that registered with the shared `AGENT_INGEST_KEY` belong to the default organization and are
marked `legacy key`. The console and the security dashboard count them. `ALLOW_LEGACY_ENROLLMENT=false`
disables new legacy registrations. The shared key can never re-register a revoked or retired device.
Recommendation: re-enroll legacy devices with tokens, then turn legacy enrollment off.

## 4. Lifecycle

```
PENDING_ENROLLMENT → ACTIVE ⇄ DISABLED
            ⇅        ⇅
        QUARANTINED
ACTIVE / DISABLED / QUARANTINED → REVOKED → (re-enroll with a new token) → ACTIVE
any of the above → RETIRED → DECOMMISSIONED (after data deletion)
```

| State | Telemetry | Remediation | Credential |
|---|---|---|---|
| ACTIVE | accepted | allowed (policy + approval) | valid |
| DISABLED | refused (403 `DEVICE_DISABLED`) | refused | kept |
| QUARANTINED | accepted (for investigation) | **refused** | kept |
| REVOKED | refused | refused | revoked immediately; re-enroll required |
| RETIRED | refused | refused | revoked; data kept until deletion |
| DECOMMISSIONED | refused | refused | gone; telemetry and derived data deleted |

Permissions: disable, quarantine and re-activate need `device.manage`. Revoke and retire need
`device.remove` and a recent sign-in. Data deletion needs `device.remove` + `retention.manage`, a recent
sign-in, a RETIRED device and the device id typed as confirmation.

## 5. Agent version governance

The agent policy sets `minimum_version`, `recommended_version`, `deprecated_versions` and
`blocked_versions`. With `reject_blocked`, telemetry from a blocked version is refused (403
`AGENT_VERSION_BLOCKED`). Compliance marks devices below the minimum or on a blocked version as
`NON_COMPLIANT`, with the reason. Validation shows how many devices a new block would affect before
publishing.
