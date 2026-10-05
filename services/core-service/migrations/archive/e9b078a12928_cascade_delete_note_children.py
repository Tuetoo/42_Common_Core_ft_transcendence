from collections.abc import Sequence

from alembic import op


revision: str = "e9b078a12928"
down_revision: str | None = "3c7e97d4a03e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("fk_note_tags_tag_id"), "note_tags", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_note_tags_note_id"), "note_tags", type_="foreignkey"
    )
    op.create_foreign_key(
        op.f("fk_note_tags_note_id"),
        "note_tags",
        "research_notes",
        ["note_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_note_tags_tag_id"),
        "note_tags",
        "research_tags",
        ["tag_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        op.f("fk_research_comments_note_id"),
        "research_comments",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_research_comments_note_id"),
        "research_comments",
        "research_notes",
        ["note_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_research_comments_note_id"),
        "research_comments",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_research_comments_note_id"),
        "research_comments",
        "research_notes",
        ["note_id"],
        ["id"],
    )
    op.drop_constraint(
        op.f("fk_note_tags_tag_id"), "note_tags", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_note_tags_note_id"), "note_tags", type_="foreignkey"
    )
    op.create_foreign_key(
        op.f("fk_note_tags_note_id"),
        "note_tags",
        "research_notes",
        ["note_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_note_tags_tag_id"),
        "note_tags",
        "research_tags",
        ["tag_id"],
        ["id"],
    )
