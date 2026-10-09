# Privacy

The platform monitors the health and security of enterprise laptops. It is designed so that it **cannot
be used as employee surveillance software**, and Phase 9 (organizations, roles, audit) keeps it that way.

## 1. What is collected

Device health only: CPU, memory, disk, battery, temperatures, fans, network link quality, OS and update
state, allowlisted services, the security posture (antivirus, firewall, Secure Boot, TPM), application
crash and hang events, and the top processes by resource use (name, resource figures). The full
per-collector list is in [../endpoint-agent.md](../endpoint-agent.md#what-is-collected-and-what-leaves-the-laptop).

## 2. What is never collected

Keystrokes, screenshots, screen recordings, clipboard contents, passwords, file contents or file names
of user documents, browser history, e-mail or chat message contents, microphone or webcam data, window
titles, command lines, and the network peers of connections (only counts). No feature in Phase 9 adds
any of these. The agent's collector set is fixed in code; no policy, administrator or AI component can
extend it remotely.

## 3. Opt-in and minimisation controls

| Control | Default | Effect |
|---|---|---|
| `COLLECT_PROCESS_DETAILS` (agent) | off | process image path (user folder redacted), owner, publisher |
| `INCLUDE_IP_ADDRESSES`, `INCLUDE_MAC_ADDRESSES`, `INCLUDE_SERIAL_NUMBERS` (agent) | off | identifiers in the inventory |
| `INCLUDE_HOSTNAME` (agent) | on | the computer name |
| `TWIN_SHOW_PROCESS_NAMES` (backend) | on | process names in the twin; off shows resource figures only |
| `ANOMALY_PROCESS_CONTEXT` (backend) | on | process names next to anomalies; off shows counts only |
| External AI | off | diagnosis runs locally with rules; no telemetry leaves the platform (Phase 7) |

## 4. Who can see what (Phase 9)

* Data is visible only inside the device's organization, and only to members whose role and group scope
  include the device. See [../security/tenant-isolation.md](../security/tenant-isolation.md).
* **Employees** (role "Employee / Device User") see only the laptops assigned to them.
* The console's data export (CSV/JSON of what a page shows) runs in the browser and contains only data
  the member can already see, with optional anonymisation and process names stripped. The
  `telemetry.export` permission is defined for roles but is not separately enforced: data a member can
  view can always be copied. Audit exports are server-side and are themselves audited.
* Platform administrators can enter an organization for support. Every action they take there is
  audited under that organization.

## 5. Retention and deletion

Retention is limited by platform ceilings and can be shortened per organization. A device's data can be
deleted completely when it leaves service. See [data-retention.md](data-retention.md).

## 6. Recommendations for organizations deploying the platform

1. Tell employees what is collected (section 1) and not collected (section 2). This page can serve as
   the basis of that notice.
2. Keep process names off (`TWIN_SHOW_PROCESS_NAMES=false`, `ANOMALY_PROCESS_CONTEXT=false`) where
   works councils or local law require it.
3. Use group-scoped roles so that support staff see only the devices they support.
4. Set retention to the shortest period that fits operations.
5. Review the audit trail for unusual access (exports, platform administrator activity).

This document describes the product's behaviour. It is not legal advice and does not by itself make a
deployment compliant with GDPR or other law. See [compliance-readiness.md](../security/compliance-readiness.md).
