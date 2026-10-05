from datetime import datetime
from uuid import UUID
from sqlalchemy import (Boolean, ForeignKey, Index, Integer, Numeric, String,
                        Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime
from shared.database import Base, UUIDPKMixin


class InvestmentDecision(Base, UUIDPKMixin):
    __tablename__ = "investment_decisions"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True)
    portfolio_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True)
    recommendation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("recommendations.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    action_taken: Mapped[str] = mapped_column(String(10), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index(
            "ix_investment_decisions_recommendation_id", "recommendation_id"),
        UniqueConstraint(
            "user_id", "recommendation_id",
            name="uq_investment_decisions_user_recommendation"))


class DecisionOutcome(Base, UUIDPKMixin):
    __tablename__ = "decision_outcomes"

    decision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("investment_decisions.id"),
        nullable=False)
    evaluation_days: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy_return: Mapped[float | None] = mapped_column(
        Numeric, nullable=True)
    benchmark_return: Mapped[float | None] = mapped_column(
        Numeric, nullable=True)
    alpha: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    outperformed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_decision_outcomes_decision_id", "decision_id"),
        UniqueConstraint(
            "decision_id", "evaluation_days",
            name="uq_decision_outcomes_decision_window"))


class DecisionReview(Base, UUIDPKMixin):
    __tablename__ = "decision_reviews"

    decision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("investment_decisions.id"),
        nullable=False)
    review_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    lessons_learned: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_decision_reviews_decision_id", "decision_id"), )
