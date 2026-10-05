from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "3c7e97d4a03e"
down_revision: str | None = "ded4e865c2b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("uq_documents_ticker_category_filename"),
        "documents",
        type_="unique",
    )
    op.create_index(
        "uq_documents_org_ticker_category_filename",
        "documents",
        ["organization_id", "ticker", "category", "filename"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"),
    )
    op.create_index(
        "uq_documents_owner_ticker_category_filename",
        "documents",
        ["owner_id", "ticker", "category", "filename"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_documents_owner_ticker_category_filename",
        table_name="documents",
        postgresql_where=sa.text("organization_id IS NULL"),
    )
    op.drop_index(
        "uq_documents_org_ticker_category_filename",
        table_name="documents",
        postgresql_where=sa.text("organization_id IS NOT NULL"),
    )
    op.create_unique_constraint(
        op.f("uq_documents_ticker_category_filename"),
        "documents",
        ["ticker", "category", "filename"],
        postgresql_nulls_not_distinct=False,
    )
