from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "f1230a000001"
down_revision: str | None = "f1220a000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "kg_relations", sa.Column("kind", sa.String(length=10), nullable=True))
    op.add_column("kg_relations", sa.Column("as_of", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("kg_relations", "as_of")
    op.drop_column("kg_relations", "kind")
