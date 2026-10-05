from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1170c000001"
down_revision: str | None = "f1160c000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_INDEX = "uq_documents_org_ticker_category_filename"
_OWNER_INDEX = "uq_documents_owner_ticker_category_filename"
_ORG_WHERE = "organization_id IS NOT NULL"
_OWNER_WHERE = "organization_id IS NULL"

_DUPLICATES_SQL = """
SELECT 'organization' AS scope, organization_id::text AS tenant, ticker,
       category, filename, count(*) AS copies
FROM documents WHERE organization_id IS NOT NULL
GROUP BY organization_id, ticker, category, filename HAVING count(*) > 1
UNION ALL
SELECT 'owner', owner_id::text, ticker, category, filename, count(*)
FROM documents WHERE organization_id IS NULL
GROUP BY owner_id, ticker, category, filename HAVING count(*) > 1
ORDER BY 1, 2, 4, 5
"""


def _recreate_document_indexes(*, nulls_not_distinct: bool) -> None:
    op.drop_index(
        _OWNER_INDEX, table_name="documents",
        postgresql_where=sa.text(_OWNER_WHERE))
    op.drop_index(
        _ORG_INDEX, table_name="documents",
        postgresql_where=sa.text(_ORG_WHERE))
    op.create_index(
        _ORG_INDEX, "documents",
        ["organization_id", "ticker", "category", "filename"], unique=True,
        postgresql_where=sa.text(_ORG_WHERE),
        postgresql_nulls_not_distinct=nulls_not_distinct)
    op.create_index(
        _OWNER_INDEX, "documents",
        ["owner_id", "ticker", "category", "filename"], unique=True,
        postgresql_where=sa.text(_OWNER_WHERE),
        postgresql_nulls_not_distinct=nulls_not_distinct)


def upgrade() -> None:
    connection = op.get_bind()
    duplicates = connection.execute(sa.text(_DUPLICATES_SQL)).fetchall()
    if duplicates:
        listing = "\n".join(
            f"  {row.scope} {row.tenant} ticker={row.ticker} "
            f"category={row.category} filename={row.filename!r}: {row.copies} "
            f"copies" for row in duplicates)
        raise RuntimeError(
            "f1170c000001: core.documents has duplicate documents that the "
            "new "
            "NULLS NOT DISTINCT unique indexes would reject; nothing was "
            "changed. "
            "Delete the extra copies (keep one per group) and start the "
            "service "
            f"again:\n{listing}")
    ownerless = connection.execute(
        sa.text("SELECT count(*) FROM api_keys WHERE owner_user_id IS NULL")
    ).scalar_one()
    if ownerless:
        raise RuntimeError(
            f"f1170c000001: {ownerless} api_keys row(s) have no "
            f"owner_user_id; "
            "owner_user_id cannot become NOT NULL. Nothing was changed.")

    _recreate_document_indexes(nulls_not_distinct=True)

    op.drop_constraint(
        op.f("ck_api_keys_ck_api_keys_has_owner"), "api_keys", type_="check")
    op.drop_column("api_keys", "owner_org_id")
    op.drop_column("api_keys", "scopes")
    op.alter_column(
        "api_keys", "owner_user_id", existing_type=sa.UUID(), nullable=False)


def downgrade() -> None:
    op.alter_column(
        "api_keys", "owner_user_id", existing_type=sa.UUID(), nullable=True)
    op.add_column(
        "api_keys",
        sa.Column(
            "scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column(
        "api_keys", sa.Column("owner_org_id", sa.UUID(), nullable=True))
    op.create_check_constraint(
        op.f("ck_api_keys_ck_api_keys_has_owner"), "api_keys",
        "owner_user_id IS NOT NULL OR owner_org_id IS NOT NULL")
    _recreate_document_indexes(nulls_not_distinct=False)
