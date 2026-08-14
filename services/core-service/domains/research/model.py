from uuid import UUID
from datetime import datetime
from sqlalchemy import (Computed, DateTime, ForeignKey, Index, LargeBinary,
                        String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from shared.database import Base
from shared.models import UUIDPKMixin


class Document(Base, UUIDPKMixin):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint(
        "ticker", "category", "filename",
        name="uq_documents_ticker_category_filename"),)
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
        DateTime(timezone=True), server_default="now()", nullable=False)
    content_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class ResearchNote(Base, UUIDPKMixin):
    __tablename__ = "research_notes"
    __table_args__ = (Index("ix_research_notes_search_vector", "search_vector",
                            postgresql_using="gin"),)
    author_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True)
    organization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_vector: Mapped[str] = mapped_column(TSVECTOR, Computed(
        "to_tsvector('english', title || ' ' || coalesce(content, ''))",
        persisted=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False)
    yjs_state: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class ResearchComment(Base, UUIDPKMixin):
    __tablename__ = "research_comments"
    note_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_notes.id"), nullable=False,
        index=True)
    author_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False)


class ResearchTag(Base, UUIDPKMixin):
    __tablename__ = "research_tags"
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class NoteTag(Base):
    __tablename__ = "note_tags"
    note_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_notes.id"),
        primary_key=True)
    tag_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_tags.id"), primary_key=True)
