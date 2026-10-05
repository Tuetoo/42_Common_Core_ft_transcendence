from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "f1190b000001"
down_revision: str | None = "f1160b000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_sessions",
        sa.Column("revoked_reason", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("user_sessions", "revoked_reason")
