from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from shared.exceptions import NotFoundError
from domains.analytics.repository import AnaticsRepository
from domains.analytics.schema import (
    AgentPerformanceRead, PortfolioMetricRead, PortfolioEvaluationRead)


class AnalyticsService:
    def __init__(self, session: AsyncSession):
        self.repo = AnaticsRepository(session)

    async def get_portfolio_metrics(self, portfolio_id: UUID
                                    ) -> PortfolioMetricRead:
        metrics = await self.repo.get_portfolio_metrics(portfolio_id)
        if metrics is None:
            raise NotFoundError("No metrics computed yet for portfolio "
                                f"{portfolio_id}")
        return PortfolioMetricRead.model_validate(metrics)

    async def get_agent_performance(self) -> list[AgentPerformanceRead]:
        metrics = await self.repo.list_agent_metrics()
        return [AgentPerformanceRead.model_validate(m) for m in metrics]

    async def get_portfolio_evaluation(self, portfolio_id: UUID
                                       ) -> PortfolioEvaluationRead:
        evaluation = await self.repo.get_latest_portfolio_evaluation(
            portfolio_id)
        if evaluation is None:
            raise NotFoundError("No evaluation computed yet for portfolio "
                                f"{portfolio_id}")
        return PortfolioEvaluationRead.model_validate(evaluation)
