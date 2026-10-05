from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1160b000001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users", sa.Column("email", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("system_role", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")))
    op.create_index(
        "uq_users_email_lower", "users", [sa.literal_column("lower(email)")],
        unique=True)
    op.create_index(
        "uq_users_username_lower", "users",
        [sa.literal_column("lower(username)")], unique=True)
    op.create_table(
        "connections", sa.Column("requester_id", sa.UUID(), nullable=False),
        sa.Column("addressee_id", sa.UUID(), nullable=False),
        sa.Column(
            "pair_key", sa.Text(),
            sa.Computed(
                "LEAST(requester_id, addressee_id)::text || ':' || "
                "GREATEST(requester_id, addressee_id)::text", persisted=True),
            nullable=False), sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_by", sa.UUID(), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["addressee_id"], ["users.id"],
                                name=op.f("fk_connections_addressee_id")),
        sa.ForeignKeyConstraint(["blocked_by"], ["users.id"],
                                name=op.f("fk_connections_blocked_by"),
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requester_id"], ["users.id"],
                                name=op.f("fk_connections_requester_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connections")),
        sa.UniqueConstraint("pair_key", name="uq_connection_pair_key"),
        sa.UniqueConstraint(
            "requester_id", "addressee_id",
            name="uq_connection_requester_addressee"))
    op.create_index(
        op.f("ix_connections_addressee_id"), "connections", ["addressee_id"],
        unique=False)
    op.create_index(
        op.f("ix_connections_requester_id"), "connections", ["requester_id"],
        unique=False)
    op.create_table(
        "oauth_accounts", sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_user_id", sa.String(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_oauth_accounts_user_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_accounts")),
        sa.UniqueConstraint(
            "provider", "provider_user_id", name="uq_oauth_provider_user"))
    op.create_table(
        "organizations", sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"],
                                name=op.f("fk_organizations_owner_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")))
    op.create_table(
        "two_factor_secrets", sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_used_step", sa.BigInteger(), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_two_factor_secrets_user_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_two_factor_secrets")),
        sa.UniqueConstraint(
            "user_id", name=op.f("uq_two_factor_secrets_user_id")))
    op.create_table(
        "user_profiles", sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("first_name", sa.String(), nullable=True),
        sa.Column("last_name", sa.String(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("timezone", sa.String(), nullable=True),
        sa.Column("country", sa.String(), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_user_profiles_user_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_profiles")),
        sa.UniqueConstraint("user_id", name=op.f("uq_user_profiles_user_id")))
    op.create_table(
        "user_sessions", sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("refresh_token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("device_info", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_user_sessions_user_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")))
    op.create_index(
        op.f("ix_user_sessions_user_id"), "user_sessions", ["user_id"],
        unique=False)
    op.create_index(
        "uq_user_sessions_refresh_token_hash", "user_sessions",
        ["refresh_token_hash"], unique=True)
    op.create_table(
        "user_settings", sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("preferred_currency", sa.String(), nullable=False),
        sa.Column("theme", sa.String(), nullable=False),
        sa.Column("default_model", sa.String(), nullable=True),
        sa.Column(
            "notification_preferences",
            postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_user_settings_user_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_settings")),
        sa.UniqueConstraint("user_id", name=op.f("uq_user_settings_user_id")))
    op.create_table(
        "organization_invitations",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("invited_by", sa.UUID(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["invited_by"], ["users.id"],
            name=op.f("fk_organization_invitations_invited_by")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_organization_invitations_organization_id"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_organization_invitations")))
    op.create_table(
        "organization_members",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_organization_members_organization_id"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_organization_members_user_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_members")),
        sa.UniqueConstraint(
            "organization_id", "user_id", name="uq_org_member_org_user"))
    op.create_index(
        op.f("ix_organization_members_user_id"), "organization_members",
        ["user_id"], unique=False)
    op.create_table(
        "organization_roles",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("role_name", sa.String(), nullable=False),
        sa.Column(
            "permissions", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_organization_roles_organization_id"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_roles")))
    op.create_index(
        op.f("ix_organization_roles_organization_id"), "organization_roles",
        ["organization_id"], unique=False)


def downgrade() -> None:
    op.drop_index(
        op.f("ix_organization_roles_organization_id"),
        table_name="organization_roles")
    op.drop_table("organization_roles")
    op.drop_index(
        op.f("ix_organization_members_user_id"),
        table_name="organization_members")
    op.drop_table("organization_members")
    op.drop_table("organization_invitations")
    op.drop_table("user_settings")
    op.drop_index(
        "uq_user_sessions_refresh_token_hash", table_name="user_sessions")
    op.drop_index(op.f("ix_user_sessions_user_id"), table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_table("user_profiles")
    op.drop_table("two_factor_secrets")
    op.drop_table("organizations")
    op.drop_table("oauth_accounts")
    op.drop_index(
        op.f("ix_connections_requester_id"), table_name="connections")
    op.drop_index(
        op.f("ix_connections_addressee_id"), table_name="connections")
    op.drop_table("connections")
    op.drop_index("uq_users_username_lower", table_name="users")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_table("users")
