from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1160c000001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys", sa.Column("owner_user_id", sa.UUID(), nullable=True),
        sa.Column("owner_org_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column(
            "scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.CheckConstraint(
            "owner_user_id IS NOT NULL OR owner_org_id IS NOT NULL",
            name=op.f("ck_api_keys_ck_api_keys_has_owner")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")))
    op.create_index(
        op.f("ix_api_keys_owner_user_id"), "api_keys", ["owner_user_id"],
        unique=False)
    op.create_table(
        "documents", sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("document_type", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=True),
        sa.Column("storage_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column("content_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")))
    op.create_index(
        op.f("ix_documents_organization_id"), "documents", ["organization_id"],
        unique=False)
    op.create_index(
        op.f("ix_documents_owner_id"), "documents", ["owner_id"], unique=False)
    op.create_index(
        "uq_documents_org_ticker_category_filename", "documents",
        ["organization_id", "ticker", "category", "filename"], unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"))
    op.create_index(
        "uq_documents_owner_ticker_category_filename", "documents",
        ["owner_id", "ticker", "category", "filename"], unique=True,
        postgresql_where=sa.text("organization_id IS NULL"))
    op.create_table(
        "notifications", sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")))
    op.create_index(
        op.f("ix_notifications_user_id"), "notifications", ["user_id"],
        unique=False)
    op.create_table(
        "portfolios", sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("visibility", sa.String(), nullable=False),
        sa.Column("auto_analyze_at_open", sa.Boolean(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolios")))
    op.create_index(
        op.f("ix_portfolios_organization_id"), "portfolios",
        ["organization_id"], unique=False)
    op.create_index(
        op.f("ix_portfolios_owner_id"), "portfolios", ["owner_id"],
        unique=False)
    op.create_table(
        "research_notes", sa.Column("author_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column(
            "search_vector", postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', title || ' ' || coalesce(content, "
                "''))", persisted=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column("yjs_state", sa.LargeBinary(), nullable=True),
        sa.Column(
            "is_hidden", sa.Boolean(), server_default=sa.text("false"),
            nullable=False), sa.Column("hidden_by", sa.UUID(), nullable=True),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sentiment_label", sa.String(length=16), nullable=True),
        sa.Column("sentiment_score", sa.Float(), nullable=True),
        sa.Column("sentiment_model", sa.String(length=128), nullable=True),
        sa.Column(
            "sentiment_analyzed_at", sa.DateTime(timezone=True),
            nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_notes")))
    op.create_index(
        op.f("ix_research_notes_author_id"), "research_notes", ["author_id"],
        unique=False)
    op.create_index(
        op.f("ix_research_notes_organization_id"), "research_notes",
        ["organization_id"], unique=False)
    op.create_index(
        "ix_research_notes_search_vector", "research_notes", ["search_vector"],
        unique=False, postgresql_using="gin")
    op.create_table(
        "research_tags", sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_tags")),
        sa.UniqueConstraint("name", name=op.f("uq_research_tags_name")))
    op.create_table(
        "watchlists", sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlists")))
    op.create_index(
        op.f("ix_watchlists_owner_id"), "watchlists", ["owner_id"],
        unique=False)
    op.create_table(
        "holdings", sa.Column("portfolio_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("average_cost", sa.Numeric(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"],
                                name=op.f("fk_holdings_portfolio_id"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_holdings")),
        sa.UniqueConstraint(
            "portfolio_id", "ticker", name="uq_holding_portfolio_ticker"))
    op.create_table(
        "note_tags", sa.Column("note_id", sa.UUID(), nullable=False),
        sa.Column("tag_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["research_notes.id"],
                                name=op.f("fk_note_tags_note_id"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["research_tags.id"],
                                name=op.f("fk_note_tags_tag_id"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "note_id", "tag_id", name=op.f("pk_note_tags")))
    op.create_table(
        "portfolio_snapshots",
        sa.Column("portfolio_id", sa.UUID(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("total_value", sa.Numeric(), nullable=False),
        sa.Column("cash_balance", sa.Numeric(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["portfolio_id"], ["portfolios.id"],
            name=op.f("fk_portfolio_snapshots_portfolio_id"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolio_snapshots")),
        sa.UniqueConstraint(
            "portfolio_id", "snapshot_date",
            name="uq_snapshot_portfolio_date"))
    op.create_table(
        "research_comments", sa.Column("note_id", sa.UUID(), nullable=False),
        sa.Column("author_id", sa.UUID(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_hidden", sa.Boolean(), server_default=sa.text("false"),
            nullable=False), sa.Column("hidden_by", sa.UUID(), nullable=True),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sentiment_label", sa.String(length=16), nullable=True),
        sa.Column("sentiment_score", sa.Float(), nullable=True),
        sa.Column("sentiment_model", sa.String(length=128), nullable=True),
        sa.Column(
            "sentiment_analyzed_at", sa.DateTime(timezone=True),
            nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["research_notes.id"],
                                name=op.f("fk_research_comments_note_id"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_comments")))
    op.create_index(
        op.f("ix_research_comments_note_id"), "research_comments", ["note_id"],
        unique=False)
    op.create_table(
        "transactions", sa.Column("portfolio_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("transaction_type", sa.String(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=True),
        sa.Column("price", sa.Numeric(), nullable=True),
        sa.Column("dividend_per_share", sa.Numeric(), nullable=True),
        sa.Column("split_ratio", sa.Numeric(), nullable=True),
        sa.Column(
            "transaction_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"],
                                name=op.f("fk_transactions_portfolio_id"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_transactions_idempotency_key")))
    op.create_index(
        op.f("ix_transactions_portfolio_id"), "transactions", ["portfolio_id"],
        unique=False)
    op.create_table(
        "watchlist_items",
        sa.Column("watchlist_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["watchlist_id"], ["watchlists.id"],
                                name=op.f("fk_watchlist_items_watchlist_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlist_items")),
        sa.UniqueConstraint(
            "watchlist_id", "ticker",
            name="uq_watchlist_item_watchlist_ticker"))


def downgrade() -> None:
    op.drop_table("watchlist_items")
    op.drop_index(
        op.f("ix_transactions_portfolio_id"), table_name="transactions")
    op.drop_table("transactions")
    op.drop_index(
        op.f("ix_research_comments_note_id"), table_name="research_comments")
    op.drop_table("research_comments")
    op.drop_table("portfolio_snapshots")
    op.drop_table("note_tags")
    op.drop_table("holdings")
    op.drop_index(op.f("ix_watchlists_owner_id"), table_name="watchlists")
    op.drop_table("watchlists")
    op.drop_table("research_tags")
    op.drop_index(
        "ix_research_notes_search_vector", table_name="research_notes",
        postgresql_using="gin")
    op.drop_index(
        op.f("ix_research_notes_organization_id"), table_name="research_notes")
    op.drop_index(
        op.f("ix_research_notes_author_id"), table_name="research_notes")
    op.drop_table("research_notes")
    op.drop_index(op.f("ix_portfolios_owner_id"), table_name="portfolios")
    op.drop_index(
        op.f("ix_portfolios_organization_id"), table_name="portfolios")
    op.drop_table("portfolios")
    op.drop_index(op.f("ix_notifications_user_id"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_index(
        "uq_documents_owner_ticker_category_filename", table_name="documents",
        postgresql_where=sa.text("organization_id IS NULL"))
    op.drop_index(
        "uq_documents_org_ticker_category_filename", table_name="documents",
        postgresql_where=sa.text("organization_id IS NOT NULL"))
    op.drop_index(op.f("ix_documents_owner_id"), table_name="documents")
    op.drop_index(op.f("ix_documents_organization_id"), table_name="documents")
    op.drop_table("documents")
    op.drop_index(op.f("ix_api_keys_owner_user_id"), table_name="api_keys")
    op.drop_table("api_keys")
