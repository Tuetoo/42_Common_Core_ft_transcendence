from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "05942b356f11"
down_revision: str | None = "941da8fcdd01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "price_bars",
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        'UPDATE price_bars SET fetched_at = "timestamp" '
        "WHERE fetched_at IS NULL"
    )
    op.alter_column("price_bars", "fetched_at", nullable=False)


def downgrade() -> None:
    op.drop_column("price_bars", "fetched_at")
