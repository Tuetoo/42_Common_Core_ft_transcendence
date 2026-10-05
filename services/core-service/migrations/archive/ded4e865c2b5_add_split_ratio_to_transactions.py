from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "ded4e865c2b5"
down_revision: str | None = "abe4d5b13e9e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transactions", sa.Column("split_ratio", sa.Numeric(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("transactions", "split_ratio")
