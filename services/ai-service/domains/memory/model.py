from datetime import datetime
from typing import Any
from uuid import UUID
from sqlalchemy import Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime
from shared.database import Base, UUIDPKMixin


class EpisodicMemory(Base, UUIDPKMixin):
    __tablename__ = "episodic_memories"

    user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True)
    analysis_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("analyses.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    recommendation: Mapped[str] = mapped_column(String(10), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    outcome_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index(
            "ix_episodic_memories_ticker_created_at", "ticker",
            "created_at"), )


class AgentMemory(Base, UUIDPKMixin):
    __tablename__ = "agent_memories"

    agent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(10), nullable=True)
    user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True)
    memory_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default="trajectory")
    score: Mapped[float] = mapped_column(Float, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_agent_memories_agent_id_score", "agent_id", "score"), )
