"""Phase 9: organisations (tenants), structure, membership, permissions, device registry and lifecycle.

Platform -> Organization -> business units / departments / teams -> device groups -> devices -> agents.
Every user action is evaluated in a TenantContext (organisation, role, permissions, scope) resolved on the
server from the authenticated identity; tenant or device ids sent by a client are never trusted alone.
"""
