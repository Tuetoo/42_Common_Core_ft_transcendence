from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1200c000001"
down_revision: str | None = "f1170c000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_outbox",
        sa.Column("event_name", sa.String(length=100), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempts", sa.Integer(), server_default=sa.text("0"),
            nullable=False), sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_outbox")))
    op.create_index(
        "ix_event_outbox_unpublished", "event_outbox", ["created_at"],
        unique=False, postgresql_where=sa.text("published_at IS NULL"))


def downgrade() -> None:
    op.drop_index(
        "ix_event_outbox_unpublished", table_name="event_outbox",
        postgresql_where=sa.text("published_at IS NULL"))
    op.drop_table("event_outbox")
