from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1220a000001"
down_revision: str | None = "f1210a000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "filing_records",
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("filename", sa.String(length=200), nullable=False),
        sa.Column("form", sa.String(length=20), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column(
            "records", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_filing_records")),
        sa.UniqueConstraint(
            "filename", name=op.f("uq_filing_records_filename")))
    op.create_index(
        "ix_filing_records_ticker_filing_date", "filing_records",
        ["ticker", "filing_date"], unique=False)


def downgrade() -> None:
    op.drop_index(
        "ix_filing_records_ticker_filing_date", table_name="filing_records")
    op.drop_table("filing_records")
