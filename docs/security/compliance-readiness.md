# Compliance readiness

**This platform is not certified** against SOC 2, ISO/IEC 27001, GDPR, HIPAA or any other standard, and
nothing here should be read as a claim of certification or legal compliance. This page maps the controls
the product implements to common framework areas, so that an organization preparing its own assessment
knows what the product provides and what remains its own responsibility.

| Area (typical framework references) | Provided by the product | Evidence | Customer responsibility |
|---|---|---|---|
| Access control, least privilege (ISO 27001 A.5.15–5.18, A.8.2; SOC 2 CC6.1–6.3) | 10 roles, permission sets, group scope, escalation guards, recent sign-in for sensitive actions | [security-model.md](security-model.md), `test_security_phase9.py` | role assignment, periodic access reviews |
| Authentication, MFA (A.8.5; CC6.1) | sessions with revocation, TOTP, MFA policy, OIDC with MFA assertion | [identity.md](identity.md) | IdP configuration, MFA enforcement choice |
| User lifecycle (A.5.16; CC6.2) | SCIM deprovisioning ends sessions; disable members | SCIM tests | joiner/mover/leaver process |
| Logging and monitoring (A.8.15–8.16; CC7.2) | central hash-chained audit, security dashboard, metrics | [../governance/audit.md](../governance/audit.md) | review, alerting, log retention off-host |
| Segregation of customer data (CC6.1; multi-tenant SaaS expectations) | logical tenant isolation tested across every GET route, WebSocket and background jobs | [tenant-isolation.md](tenant-isolation.md) | — |
| Change management (A.8.32; CC8.1) | policy draft → validate (preview) → publish, versioning and rollback, audited | [../governance/policies.md](../governance/policies.md) | approval process outside the product |
| Endpoint actions (CC8.1, CC7.4) | signed allowlisted remediation, human approval, four-eyes, kill switches | [../remediation.md](../remediation.md) | who approves what |
| Asset inventory (A.5.9) | device registry, hardware inventory, lifecycle | [device-enrollment.md](device-enrollment.md) | completeness of enrollment |
| Configuration / vulnerability management (A.8.8–8.9) | agent version policy, blocked versions, compliance checks (antivirus, firewall, Secure Boot, TPM) | compliance API | patching, image rebuilds |
| Cryptography (A.8.24) | hashed tokens, scrypt passwords, Fernet for MFA seeds, Ed25519 remediation signatures, TLS for agents | [security-model.md §4](security-model.md#4-data-protection) | TLS termination, disk encryption, key custody |
| Data minimisation, privacy by design (GDPR Art. 5(1)(c), 25) | fixed collector set, no content collection, opt-in identifiers, local-only AI | [../governance/privacy.md](../governance/privacy.md) | lawful basis, employee notice, works council |
| Storage limitation, erasure (GDPR Art. 5(1)(e), 17) | retention policies, device data deletion workflow | [../governance/data-retention.md](../governance/data-retention.md) | deciding retention periods, handling requests |
| Backup and recovery (A.8.13; A1.2) | backup/restore tool with integrity verification | [../operations/backup-restore.md](../operations/backup-restore.md) | schedule, off-site copies, restore tests |
| Threat modelling, secure development (A.8.25–8.29) | STRIDE threat model, security test suite, dependency review | [threat-model.md](threat-model.md), [dependency-review.md](dependency-review.md) | independent penetration test before production use |

## Known gaps for an audit

* No independent penetration test or code audit has been performed.
* Quotas and rate limits are per process (multi-replica deployments need the shared-window follow-up).
* One database role for migrations and runtime (threat model RR3).
* SAML sign-in is not available (assertions are refused).
* No built-in SIEM export (audit export is manual or via the API).
