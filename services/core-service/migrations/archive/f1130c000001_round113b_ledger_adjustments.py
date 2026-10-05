from collections.abc import Sequence

revision: str = "f1130c000001"
down_revision: str | None = "f1120c000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
