from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1210b000001"
down_revision: str | None = "f1190b000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        op.f("ix_organization_roles_organization_id"),
        table_name="organization_roles")
    op.drop_table("organization_roles")


def downgrade() -> None:
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
