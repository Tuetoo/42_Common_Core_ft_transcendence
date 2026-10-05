from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3b5bfb73477"
down_revision: str | None = "619803f90625"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "analyses", sa.Column("failure_reason", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("analyses", "failure_reason")
