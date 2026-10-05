from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from sqlalchemy import Boolean, ForeignKey, Index, Numeric, String, Text, case
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime
from shared.database import Base, UUIDPKMixin

UNAVAILABLE_ARGUMENT_LIKE = "[% argument unavailable%"


class Analysis(Base, UUIDPKMixin):
    __tablename__ = "analyses"

    user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True)
    portfolio_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True)

    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    recommendations: "Mapped[list[Recommendation]]" = relationship(
        back_populates="analysis", cascade="all, delete-orphan",
        order_by="(Recommendation.created_at, Recommendation.id)")
    reflections: "Mapped[list[Reflection]]" = relationship(
        back_populates="analysis", cascade="all, delete-orphan",
        order_by="(Reflection.round_number, Reflection.id)")
    agent_runs: "Mapped[list[AgentRun]]" = relationship(
        back_populates="analysis", cascade="all, delete-orphan",
        order_by="(AgentRun.started_at, AgentRun.id)")
    debate_sessions: "Mapped[list[DebateSession]]" = relationship(
        back_populates="analysis", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_analyses_user_id_created_at", "user_id", "created_at"),
        Index("ix_analyses_ticker_created_at", "ticker", "created_at"))


class Recommendation(Base, UUIDPKMixin):
    __tablename__ = "recommendations"

    analysis_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("analyses.id"), nullable=False)
    recommendation: Mapped[str] = mapped_column(String(10), nullable=False)
    confidence: Mapped[float] = mapped_column(
        Numeric(asdecimal=False), nullable=False)
    judge_leaning: Mapped[str | None] = mapped_column(
        String(10), nullable=True)
    judge_confidence: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_adjustment_note: Mapped[str | None] = mapped_column(
        Text, nullable=True)
    tier_generation_note: Mapped[str | None] = mapped_column(
        Text, nullable=True)
    debate_converged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True)
    window_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    analysis: Mapped[Analysis] = relationship(back_populates="recommendations")
    tiers: "Mapped[list[RecommendationTier]]" = relationship(
        back_populates="recommendation", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_recommendations_analysis_id", "analysis_id"), )


class RecommendationTier(Base, UUIDPKMixin):
    __tablename__ = "recommendation_tiers"

    recommendation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("recommendations.id"), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    target_price: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending_acceptance")
    triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    triggered_price: Mapped[Decimal | None] = mapped_column(
        Numeric, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    recommendation: Mapped[Recommendation] = relationship(
        back_populates="tiers")

    __table_args__ = (
        Index(
            "ix_recommendation_tiers_recommendation_id", "recommendation_id"),
        Index("ix_recommendation_tiers_status", "status"))


class Reflection(Base, UUIDPKMixin):
    __tablename__ = "reflections"

    analysis_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("analyses.id"), nullable=False)
    round_number: Mapped[int] = mapped_column(nullable=False, default=0)
    verified: Mapped[bool] = mapped_column(nullable=False)
    issues: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    retry_agents: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True)

    analysis: Mapped[Analysis] = relationship(back_populates="reflections")

    __table_args__ = (Index("ix_reflections_analysis_id", "analysis_id"), )


class Agent(Base, UUIDPKMixin):
    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str] = mapped_column(
        String(20), nullable=False, default="1.0.0")
    active: Mapped[bool] = mapped_column(nullable=False, default=True)


class AgentRun(Base, UUIDPKMixin):
    __tablename__ = "agent_runs"

    analysis_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("analyses.id"), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)

    analysis: Mapped[Analysis] = relationship(back_populates="agent_runs")
    agent: Mapped[Agent] = relationship()
    result: "Mapped[AgentResult | None]" = relationship(
        back_populates="agent_run", uselist=False,
        cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_agent_runs_analysis_id",
              "analysis_id"), Index("ix_agent_runs_agent_id", "agent_id"))


class AgentResult(Base, UUIDPKMixin):
    __tablename__ = "agent_results"

    agent_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=False,
        unique=True)
    score: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    agent_run: Mapped[AgentRun] = relationship(back_populates="result")

    __table_args__ = (Index("ix_agent_results_agent_run_id", "agent_run_id"), )


class DebateSession(Base, UUIDPKMixin):
    __tablename__ = "debate_sessions"

    analysis_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("analyses.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    analysis: Mapped[Analysis] = relationship(back_populates="debate_sessions")
    arguments: "Mapped[list[DebateArgument]]" = relationship(
        back_populates="debate_session", cascade="all, delete-orphan",
        order_by=lambda: [
            DebateArgument.round_number, DebateArgument.side,
            case((DebateArgument.content.like(UNAVAILABLE_ARGUMENT_LIKE), 0),
                 else_=1), DebateArgument.id
        ])

    __table_args__ = (Index("ix_debate_sessions_analysis_id", "analysis_id"), )


class DebateArgument(Base, UUIDPKMixin):
    __tablename__ = "debate_arguments"

    debate_session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("debate_sessions.id"), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    round_number: Mapped[int] = mapped_column(nullable=False, default=0)
    citations: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)

    debate_session: Mapped[DebateSession] = relationship(
        back_populates="arguments")

    __table_args__ = (
        Index("ix_debate_arguments_debate_session_id", "debate_session_id"), )
