from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "f1250c000001"
down_revision: str | None = "f1200c000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "status_changed_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False))
    op.execute("UPDATE documents SET status_changed_at = uploaded_at")


def downgrade() -> None:
    op.drop_column("documents", "status_changed_at")
