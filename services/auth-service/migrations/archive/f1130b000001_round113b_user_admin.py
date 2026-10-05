from collections.abc import Sequence

from alembic import op

revision: str = "f1130b000001"
down_revision: str | None = "f1120b000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at "
        "TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "UPDATE organization_invitations SET expires_at = now() "
        "WHERE role = 'owner' AND accepted_at IS NULL AND expires_at > now()"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS deleted_at")
