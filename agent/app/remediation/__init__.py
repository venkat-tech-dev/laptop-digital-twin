"""Phase 8: endpoint-side validation and execution of approved, signed remediation actions.

The agent never trusts the backend alone. Before anything runs it checks, in order: envelope structure,
Ed25519 signature against the pinned platform key, target device, expiry (and not issued in the future),
the action and version against its OWN local allowlist, strictly typed parameters, idempotency (an
execution id runs at most once; a repeat returns the earlier result), nonce replay, and local
preconditions. There is no shell, script, path or registry operation in this package.
"""
