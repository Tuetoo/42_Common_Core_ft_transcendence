from datetime import datetime
from uuid import UUID
from sqlalchemy import (Boolean, Computed, DateTime, Float, ForeignKey, Index,
                        LargeBinary, String, Text, func, text)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from shared.database import Base, UUIDPKMixin


class Document(Base, UUIDPKMixin):
    __tablename__ = "documents"

    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True)
    organization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    document_type: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(10), nullable=True)
    storage_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="uploaded")
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    content_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "uq_documents_org_ticker_category_filename", "organization_id",
            "ticker", "category", "filename", unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("organization_id IS NOT NULL")),
        Index(
            "uq_documents_owner_ticker_category_filename", "owner_id",
            "ticker", "category", "filename", unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("organization_id IS NULL")))


class ModeratedContentMixin:
    is_hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"))
    hidden_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True)
    hidden_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    sentiment_label: Mapped[str | None] = mapped_column(
        String(16), nullable=True)
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    sentiment_model: Mapped[str | None] = mapped_column(
        String(128), nullable=True)
    sentiment_analyzed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class ResearchNote(ModeratedContentMixin, Base, UUIDPKMixin):
    __tablename__ = "research_notes"
    __table_args__ = (
        Index(
            "ix_research_notes_search_vector", "search_vector",
            postgresql_using="gin"), )

    author_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True)
    organization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', title || ' ' || coalesce(content, ''))",
            persisted=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False)
    yjs_state: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class ResearchComment(ModeratedContentMixin, Base, UUIDPKMixin):
    __tablename__ = "research_comments"

    note_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("research_notes.id", ondelete="CASCADE"), nullable=False,
        index=True)
    author_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class ResearchTag(Base, UUIDPKMixin):
    __tablename__ = "research_tags"

    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class NoteTag(Base):
    __tablename__ = "note_tags"

    note_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("research_notes.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("research_tags.id", ondelete="CASCADE"), primary_key=True)
