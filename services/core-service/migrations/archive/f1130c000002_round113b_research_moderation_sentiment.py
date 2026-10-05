from collections.abc import Sequence

from alembic import op

revision: str = "f1130c000002"
down_revision: str | None = "f1130c000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONTENT_TABLES = ("research_notes", "research_comments")

_CONTENT_COLUMNS = (
    ("is_hidden", "BOOLEAN NOT NULL DEFAULT false"),
    ("hidden_by", "UUID"),
    ("hidden_at", "TIMESTAMP WITH TIME ZONE"),
    ("sentiment_label", "VARCHAR(16)"),
    ("sentiment_score", "DOUBLE PRECISION"),
    ("sentiment_model", "VARCHAR(128)"),
    ("sentiment_analyzed_at", "TIMESTAMP WITH TIME ZONE"),
)


def upgrade() -> None:
    for table in _CONTENT_TABLES:
        for column, ddl in _CONTENT_COLUMNS:
            op.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"
            )
    op.execute(
        "ALTER TABLE research_comments "
        "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS title VARCHAR(256)"
    )
    op.execute(
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_url TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS source_url")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS title")
    op.execute(
        "ALTER TABLE research_comments DROP COLUMN IF EXISTS updated_at"
    )
    for table in _CONTENT_TABLES:
        for column, _ in reversed(_CONTENT_COLUMNS):
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
