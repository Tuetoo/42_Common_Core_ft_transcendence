from collections.abc import Sequence

from alembic import op

revision: str = "f1120a000001"
down_revision: str | None = "c3b5bfb73477"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DECISION_UQ = "uq_investment_decisions_user_recommendation"
_OUTCOME_UQ = "uq_decision_outcomes_decision_window"
_METRIC_UQ = "uq_recommendation_metrics_recommendation_id"

_STALE_DECISIONS = """
    SELECT d.id FROM (
        SELECT d.id,
               row_number() OVER (
                   PARTITION BY r.analysis_id, d.user_id
                   ORDER BY d.created_at DESC, d.id DESC
               ) AS rn
        FROM investment_decisions d
        JOIN recommendations r ON r.id = d.recommendation_id
    ) d WHERE d.rn > 1
"""


def _add_unique_if_missing(name: str, table: str, columns: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_namespace n ON n.oid = c.connamespace
                WHERE c.conname = '{name}' AND n.nspname = current_schema()
            ) THEN
                ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE ({columns});
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    op.execute(
        f"DELETE FROM decision_outcomes WHERE decision_id IN "
        f"({_STALE_DECISIONS})"
    )
    op.execute(
        f"DELETE FROM decision_reviews WHERE decision_id IN "
        f"({_STALE_DECISIONS})"
    )
    op.execute(
        f"DELETE FROM investment_decisions WHERE id IN ({_STALE_DECISIONS})"
    )
    _add_unique_if_missing(
        _DECISION_UQ, "investment_decisions", "user_id, recommendation_id"
    )

    op.execute(
        """
        DELETE FROM decision_outcomes WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY decision_id, evaluation_days
                    ORDER BY evaluated_at DESC NULLS LAST, id DESC
                ) AS rn FROM decision_outcomes
            ) o WHERE o.rn > 1
        )
        """
    )
    _add_unique_if_missing(
        _OUTCOME_UQ, "decision_outcomes", "decision_id, evaluation_days"
    )

    op.execute(
        """
        DELETE FROM recommendation_metrics WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY recommendation_id ORDER BY id
                ) AS rn FROM recommendation_metrics
            ) m WHERE m.rn > 1
        )
        """
    )
    _add_unique_if_missing(
        _METRIC_UQ, "recommendation_metrics", "recommendation_id"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE recommendation_metrics DROP CONSTRAINT IF EXISTS "
        f"{_METRIC_UQ}"
    )
    op.execute(
        f"ALTER TABLE decision_outcomes DROP CONSTRAINT IF EXISTS "
        f"{_OUTCOME_UQ}"
    )
    op.execute(
        f"ALTER TABLE investment_decisions DROP CONSTRAINT IF EXISTS "
        f"{_DECISION_UQ}"
    )
