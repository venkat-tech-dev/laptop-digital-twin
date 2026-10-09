"""Phase-9: organisations, structure, device groups, memberships, device registry / lifecycle, enrollment
tokens, versioned policies, enterprise audit (append-only), sessions, identity providers, MFA, SCIM tokens.

Additive and backward compatible: every existing user, device and credential is attached to the organisation
``default`` (the current single-organisation deployment keeps working unchanged).
  * users: + platform_admin (the earliest administrator becomes platform super-admin)
  * organization_members: existing users with mapped roles (admin -> org_owner, operator -> it_operator,
    viewer -> read_only, employee -> employee)
  * device_registry: existing devices ACTIVE in ``default`` (enrollment "legacy")
  * device_credentials: + expires_at, backfilled to now + 90 days (agents >= 1.7 rotate before it)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("quotas", JSONB, nullable=False, server_default="{}"),
        sa.Column("settings", JSONB, nullable=False, server_default="{}"),
    )
    op.create_table(
        "org_units",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(64), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("parent_id", sa.String(36)),
        sa.Column("status", sa.String(16), nullable=False),
    )
    op.create_index("ix_org_units_org", "org_units", ["org_id"])
    op.create_table(
        "device_groups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(64), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("unit_id", sa.String(36)),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("tags", JSONB, nullable=False, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_device_groups_org_name"),
    )
    op.create_table(
        "device_group_members",
        sa.Column("group_id", sa.String(36), sa.ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("device_id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=False),
    )
    op.create_index("ix_device_group_members_device", "device_group_members", ["device_id"])
    op.create_table(
        "organization_members",
        sa.Column("org_id", sa.String(64), sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("username", sa.String(64), primary_key=True),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("group_scope", JSONB, nullable=False, server_default="[]"),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_organization_members_username", "organization_members", ["username"])
    op.create_table(
        "device_registry",
        sa.Column("device_id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("lifecycle", sa.String(24), nullable=False),
        sa.Column("enrolled_at", TS),
        sa.Column("enrollment_id", sa.String(36)),
        sa.Column("updated_at", TS),
        sa.Column("updated_by", sa.String(128)),
        sa.Column("reason", sa.String(300)),
    )
    op.create_index("ix_device_registry_org_lifecycle", "device_registry", ["org_id", "lifecycle"])
    op.create_table(
        "enrollment_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(64), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("max_uses", sa.Integer, nullable=False),
        sa.Column("uses", sa.Integer, nullable=False),
        sa.Column("group_id", sa.String(36)),
        sa.Column("revoked_at", TS),
        sa.Column("label", sa.String(120), nullable=False, server_default=""),
    )
    op.create_index("ix_enrollment_tokens_org_created", "enrollment_tokens", ["org_id", "created_at"])
    op.create_table(
        "policies",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("policy_id", sa.String(36), nullable=False),
        sa.Column("org_id", sa.String(64), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("body", JSONB, nullable=False),
        sa.Column("locked", JSONB, nullable=False, server_default="[]"),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_by", sa.String(128)),
        sa.Column("updated_at", TS),
        sa.Column("effective_from", TS),
        sa.Column("effective_until", TS),
        sa.Column("note", sa.String(300), nullable=False, server_default=""),
        sa.UniqueConstraint("policy_id", "version", name="uq_policies_version"),
    )
    op.create_index("ix_policies_org_kind_status", "policies", ["org_id", "kind", "status"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(36), nullable=False, unique=True),
        sa.Column("at", TS, nullable=False),
        sa.Column("org_id", sa.String(64)),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("resource_type", sa.String(32)),
        sa.Column("resource_id", sa.String(128)),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("request_id", sa.String(64)),
        sa.Column("ip", sa.String(64)),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
    )
    for name, cols in (
        ("ix_audit_events_org_at", ["org_id", "at"]),
        ("ix_audit_events_org_action_at", ["org_id", "action", "at"]),
        ("ix_audit_events_org_actor_at", ["org_id", "actor_id", "at"]),
        ("ix_audit_events_org_resource", ["org_id", "resource_type", "resource_id"]),
    ):
        op.create_index(name, "audit_events", cols)
    op.execute(
        "CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'audit_events is append-only'; END; $$ LANGUAGE plpgsql;"
    )
    op.execute(
        "CREATE TRIGGER audit_events_no_change BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();"
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("last_seen_at", TS),
        sa.Column("revoked_at", TS),
        sa.Column("revoked_reason", sa.String(120)),
        sa.Column("auth_method", sa.String(16), nullable=False),
        sa.Column("mfa", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("ip", sa.String(64)),
        sa.Column("user_agent", sa.String(200)),
    )
    op.create_index("ix_user_sessions_username", "user_sessions", ["username"])
    op.create_index("ix_user_sessions_expires", "user_sessions", ["expires_at"])
    op.create_table(
        "identity_providers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(64), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS),
    )
    op.create_index("ix_identity_providers_org", "identity_providers", ["org_id"])
    op.create_table(
        "user_mfa",
        sa.Column("username", sa.String(64), primary_key=True),
        sa.Column("secret_enc", sa.String(512), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_step", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.create_table(
        "scim_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(64), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("revoked_at", TS),
    )
    op.add_column("users", sa.Column("platform_admin", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("device_credentials", sa.Column("expires_at", TS))

    # ---------------------------------------------------------------- backfill: the existing deployment
    op.execute(
        "INSERT INTO organizations (id, name, status, created_at, quotas, settings) "
        "VALUES ('default', 'Default organization', 'ACTIVE', now(), '{}', '{}')"
    )
    op.execute(
        "INSERT INTO organization_members (org_id, username, role, status, group_scope, source, created_at) "
        "SELECT 'default', username, CASE role WHEN 'admin' THEN 'org_owner' WHEN 'operator' THEN 'it_operator' "
        "WHEN 'viewer' THEN 'read_only' ELSE 'employee' END, CASE WHEN disabled THEN 'DISABLED' ELSE 'ACTIVE' END, "
        "'[]', 'local', created_at FROM users"
    )
    op.execute(
        "UPDATE users SET platform_admin = true WHERE id = (SELECT id FROM users WHERE role = 'admin' "
        "AND NOT disabled ORDER BY created_at LIMIT 1)"
    )
    op.execute(
        "INSERT INTO device_registry (device_id, org_id, lifecycle, enrolled_at, enrollment_id, updated_at, updated_by) "
        "SELECT id, 'default', 'ACTIVE', first_seen, 'legacy', now(), 'migration-0012' FROM devices"
    )
    op.execute("UPDATE device_credentials SET expires_at = now() + interval '90 days' WHERE NOT revoked")


def downgrade() -> None:
    op.drop_column("device_credentials", "expires_at")
    op.drop_column("users", "platform_admin")
    for t in ("scim_tokens", "user_mfa", "identity_providers", "user_sessions"):
        op.drop_table(t)
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_change ON audit_events;")
    op.execute("DROP FUNCTION IF EXISTS audit_events_append_only();")
    for t in ("audit_events", "policies", "enrollment_tokens", "device_registry", "organization_members",
              "device_group_members", "device_groups", "org_units", "organizations"):  # fmt: skip
        op.drop_table(t)
