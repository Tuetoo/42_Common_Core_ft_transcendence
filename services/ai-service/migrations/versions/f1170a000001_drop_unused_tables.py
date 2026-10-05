from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1170a000001"
down_revision: str | None = "f1160a000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "memory_references", "retrieval_logs", "technical_indicator_cache",
    "rag_collections")


def upgrade() -> None:
    connection = op.get_bind()
    non_empty = [
        f"{table} ({count} rows)" for table in _TABLES if (
            count := connection.execute(
                sa.text(f"SELECT count(*) FROM {table}")).scalar_one())
    ]
    if non_empty:
        raise RuntimeError(
            "f1170a000001: these tables were expected to be empty and are "
            "not: "
            f'{", ".join(non_empty)}. Nothing was changed; check the rows (or '
            "back them up) and empty the tables before starting the service.")
    op.drop_index(
        "ix_memory_references_analysis_id", table_name="memory_references")
    op.drop_table("memory_references")
    op.drop_index("ix_retrieval_logs_analysis_id", table_name="retrieval_logs")
    op.drop_table("retrieval_logs")
    op.drop_table("technical_indicator_cache")
    op.drop_table("rag_collections")


def downgrade() -> None:
    op.create_table(
        "rag_collections",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rag_collections")),
        sa.UniqueConstraint("name", name=op.f("uq_rag_collections_name")))
    op.create_table(
        "technical_indicator_cache",
        sa.Column("instrument_id", sa.UUID(), nullable=False),
        sa.Column("indicator", sa.String(length=20), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.id"],
            name=op.f("fk_technical_indicator_cache_instrument_id")),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_technical_indicator_cache")))
    op.create_table(
        "retrieval_logs", sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("retrieved_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"],
                                name=op.f("fk_retrieval_logs_analysis_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retrieval_logs")))
    op.create_index(
        "ix_retrieval_logs_analysis_id", "retrieval_logs", ["analysis_id"],
        unique=False)
    op.create_table(
        "memory_references", sa.Column("memory_id", sa.UUID(), nullable=False),
        sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"],
                                name=op.f("fk_memory_references_analysis_id")),
        sa.ForeignKeyConstraint(["memory_id"], ["agent_memories.id"],
                                name=op.f("fk_memory_references_memory_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_references")))
    op.create_index(
        "ix_memory_references_analysis_id", "memory_references",
        ["analysis_id"], unique=False)
