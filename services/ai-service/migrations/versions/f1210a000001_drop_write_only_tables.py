from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "f1210a000001"
down_revision: str | None = "f1170a000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_backtest_trades_backtest_id", table_name="backtest_trades")
    op.drop_table("backtest_trades")
    op.drop_index(
        "ix_backtest_positions_backtest_id", table_name="backtest_positions")
    op.drop_table("backtest_positions")
    op.drop_index(
        "ix_analysis_sources_analysis_id", table_name="analysis_sources")
    op.drop_table("analysis_sources")
    op.drop_index(
        "ix_analysis_citations_analysis_id", table_name="analysis_citations")
    op.drop_table("analysis_citations")


def downgrade() -> None:
    op.create_table(
        "analysis_citations",
        sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_id", sa.UUID(), nullable=True),
        sa.Column("citation_text", sa.Text(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_id"], ["analyses.id"],
            name=op.f("fk_analysis_citations_analysis_id")),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"],
                                name=op.f("fk_analysis_citations_chunk_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_citations")))
    op.create_index(
        "ix_analysis_citations_analysis_id", "analysis_citations",
        ["analysis_id"], unique=False)
    op.create_table(
        "analysis_sources",
        sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"],
                                name=op.f("fk_analysis_sources_analysis_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_sources")))
    op.create_index(
        "ix_analysis_sources_analysis_id", "analysis_sources", ["analysis_id"],
        unique=False)
    op.create_table(
        "backtest_positions",
        sa.Column("backtest_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("entry_price", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["backtest_id"], ["backtests.id"],
            name=op.f("fk_backtest_positions_backtest_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_positions")))
    op.create_index(
        "ix_backtest_positions_backtest_id", "backtest_positions",
        ["backtest_id"], unique=False)
    op.create_table(
        "backtest_trades", sa.Column("backtest_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("price", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("trade_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["backtest_id"], ["backtests.id"],
                                name=op.f("fk_backtest_trades_backtest_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_trades")))
    op.create_index(
        "ix_backtest_trades_backtest_id", "backtest_trades", ["backtest_id"],
        unique=False)
