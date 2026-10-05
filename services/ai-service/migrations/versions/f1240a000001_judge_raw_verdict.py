from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "f1240a000001"
down_revision: str | None = "f1230a000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recommendations",
        sa.Column("judge_leaning", sa.String(length=10), nullable=True))
    op.add_column(
        "recommendations",
        sa.Column(
            "judge_confidence", sa.Numeric(asdecimal=False), nullable=True))


def downgrade() -> None:
    op.drop_column("recommendations", "judge_confidence")
    op.drop_column("recommendations", "judge_leaning")
