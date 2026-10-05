from collections.abc import Sequence

from alembic import op

revision: str = "f1120c000001"
down_revision: str | None = "e9b078a12928"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHILD_TABLES = ("holdings", "transactions", "portfolio_snapshots")


def _drop_portfolio_fks(table: str) -> None:
    op.execute(
        f"""
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN
                SELECT conname FROM pg_constraint
                WHERE contype = 'f'
                  AND conrelid = '{table}'::regclass
                  AND confrelid = 'portfolios'::regclass
            LOOP
                EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', r.conname);
            END LOOP;
        END $$;
        """  # noqa: E501
    )


def upgrade() -> None:
    for table in _CHILD_TABLES:
        _drop_portfolio_fks(table)
        op.create_foreign_key(
            op.f(f"fk_{table}_portfolio_id"),
            table,
            "portfolios",
            ["portfolio_id"],
            ["id"],
            ondelete="CASCADE",
        )

    op.execute(
        "ALTER TABLE transactions "
        "ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(64)"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_transactions_idempotency_key'
                  AND conrelid = 'transactions'::regclass
            ) THEN
                ALTER TABLE transactions
                    ADD CONSTRAINT uq_transactions_idempotency_key
                    UNIQUE (idempotency_key);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE transactions "
        "DROP CONSTRAINT IF EXISTS uq_transactions_idempotency_key"
    )
    op.execute(
        "ALTER TABLE transactions DROP COLUMN IF EXISTS idempotency_key"
    )

    for table in _CHILD_TABLES:
        _drop_portfolio_fks(table)
        op.create_foreign_key(
            op.f(f"fk_{table}_portfolio_id"),
            table,
            "portfolios",
            ["portfolio_id"],
            ["id"],
        )
