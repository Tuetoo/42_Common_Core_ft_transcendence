from datetime import datetime
from uuid import UUID
from sqlalchemy import Index, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime
from shared.database import Base, UUIDPKMixin


class DocumentChunk(Base, UUIDPKMixin):
    __tablename__ = "document_chunks"

    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False)
    chunk_index: Mapped[int] = mapped_column(nullable=False)
    token_count: Mapped[int] = mapped_column(nullable=False)
    qdrant_point_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True)

    __table_args__ = (Index("ix_document_chunks_document_id", "document_id"), )


class EmbeddingJob(Base, UUIDPKMixin):
    __tablename__ = "embedding_jobs"

    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_embedding_jobs_document_id", "document_id"), )
