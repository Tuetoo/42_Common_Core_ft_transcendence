from uuid import UUID
from shared.schemas.base import BaseSchema


class PortfolioMetricRead(BaseSchema):
    portfolio_id: UUID
    sharpe_ratio: float | None
    sortino_ratio: float | None
    drawdown: float | None


class RecommendationAccuracyRead(BaseSchema):
    recommendation_id: UUID
    accuracy_score: float | None


class AgentPerformanceRead(BaseSchema):
    agent_id: UUID
    avg_score: float | None
    success_rate: float | None


class PortfolioEvaluationRead(BaseSchema):
    portfolio_id: UUID
    benchmark_symbol: str
    portfolio_return: float | None
    benchmark_return: float | None
    alpha: float | None
    beta: float | None
    sharpe_ratio: float | None
    sortino_ratio: float | None
    maximum_drawdown: float | None
