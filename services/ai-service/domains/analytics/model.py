from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime
from shared.database import Base
from shared.models import UUIDPKMixin


class PortfolioMetric(Base, UUIDPKMixin):
    __tablename__ = "portfolio_metrics"
    portfolio_id: Mapped[PGUUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("portfolios.id"), nullable=False)
    sharpe_ratio: Mapped[float | None] = mapped_column(Numeric)
    sortino_ratio: Mapped[float | None] = mapped_column(Numeric)
    drawdown: Mapped[float | None] = mapped_column(Numeric)


class RecommendationMetric(Base, UUIDPKMixin):
    __tablename__ = "recommendation_metrics"
    recommendation_id: Mapped[PGUUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("recommendations.id"), nullable=False)
    accuracy_score: Mapped[float | None] = mapped_column(Numeric)


class AgentMetric(Base, UUIDPKMixin):
    __tablename__ = "agent_metrics"
    agent_id: Mapped[PGUUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    avg_score: Mapped[float | None] = mapped_column(Numeric)
    success_rate: Mapped[float | None] = mapped_column(Numeric)


class PortfolioEvaluation(Base, UUIDPKMixin):
    __tablename__ = "portfolio_evaluations"
    portfolio_id: Mapped[PGUUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("portfolios.id"), nullable=False)
    benchmark_symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    portfolio_return: Mapped[float | None] = mapped_column(Numeric)
    benchmark_return: Mapped[float | None] = mapped_column(Numeric)
    alpha: Mapped[float | None] = mapped_column(Numeric)
    beta: Mapped[float | None] = mapped_column(Numeric)
    sharpe_ratio: Mapped[float | None] = mapped_column(Numeric)
    maximum_drawdown: Mapped[float | None] = mapped_column(Numeric)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True))
