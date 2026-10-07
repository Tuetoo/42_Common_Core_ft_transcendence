from collections.abc import Sequence

from alembic import op

revision: str = "f1120a000002"
down_revision: str | None = "f1120a000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE episodic_memories "
        "ADD COLUMN IF NOT EXISTS outcome_return DOUBLE PRECISION"
    )


def downgrade() -> None:
    op.drop_column("episodic_memories", "outcome_return")
