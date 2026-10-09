"""Phase 8: safe, human-approved remediation (pure domain: catalog, model, policy, envelope, verification).

DETECT -> DIAGNOSE -> RECOMMEND -> RISK ASSESS -> AUTHORIZE -> APPROVE -> VALIDATE -> EXECUTE
-> VERIFY -> AUDIT.

Only actions in the Action Catalog exist. There is no generic command, script, path, URL or registry
operation anywhere in this package or its API; an action is an id plus strongly typed parameters that
the endpoint agent validates again against its own local allowlist before doing anything.
"""
