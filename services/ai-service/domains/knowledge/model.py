from datetime import date, datetime
from uuid import UUID
from sqlalchemy import (Date, Float, ForeignKey, Index, String, Text,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime
from shared.database import Base, UUIDPKMixin


class AgentReport(Base, UUIDPKMixin):
    __tablename__ = "agent_reports"

    analysis_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("analyses.id"), nullable=False)
    agent_role: Mapped[str] = mapped_column(String(30), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(10), nullable=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_agent_reports_analysis_id",
              "analysis_id"), Index("ix_agent_reports_ticker", "ticker"))


class KgEntity(Base, UUIDPKMixin):
    __tablename__ = "kg_entities"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "normalized_name", "entity_type",
            name="uq_kg_entities_norm_type"), )


class KgRelation(Base, UUIDPKMixin):
    __tablename__ = "kg_relations"

    source_entity_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("kg_entities.id"), nullable=False)
    target_entity_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("kg_entities.id"), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source_report_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_reports.id"), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_kg_relations_source_entity_id", "source_entity_id"),
        Index("ix_kg_relations_target_entity_id", "target_entity_id"))


class TickerName(Base, UUIDPKMixin):
    __tablename__ = "ticker_names"

    ticker: Mapped[str] = mapped_column(
        String(10), nullable=False, unique=True)
    official_name: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
