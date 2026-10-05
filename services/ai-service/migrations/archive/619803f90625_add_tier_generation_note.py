from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "619803f90625"
down_revision: str | None = "05942b356f11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("portfolio_metrics")
    op.drop_table("portfolio_evaluations")
    op.add_column(
        "recommendations",
        sa.Column("tier_generation_note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recommendations", "tier_generation_note")
    op.create_table(
        "portfolio_evaluations",
        sa.Column(
            "portfolio_id", sa.UUID(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "benchmark_symbol",
            sa.VARCHAR(length=10),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "portfolio_return",
            sa.NUMERIC(),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column(
            "benchmark_return",
            sa.NUMERIC(),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column("alpha", sa.NUMERIC(), autoincrement=False, nullable=True),
        sa.Column("beta", sa.NUMERIC(), autoincrement=False, nullable=True),
        sa.Column(
            "sharpe_ratio", sa.NUMERIC(), autoincrement=False, nullable=True
        ),
        sa.Column(
            "maximum_drawdown",
            sa.NUMERIC(),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolio_evaluations")),
    )
    op.create_table(
        "portfolio_metrics",
        sa.Column(
            "portfolio_id", sa.UUID(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "sharpe_ratio", sa.NUMERIC(), autoincrement=False, nullable=True
        ),
        sa.Column(
            "sortino_ratio", sa.NUMERIC(), autoincrement=False, nullable=True
        ),
        sa.Column(
            "drawdown", sa.NUMERIC(), autoincrement=False, nullable=True
        ),
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolio_metrics")),
    )
