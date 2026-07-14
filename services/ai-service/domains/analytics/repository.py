from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from domains.analytics.model import (AgentMetric, PortfolioEvaluation,
                                     PortfolioMetric)


class AnaticsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_portfolio_metrics(self, portfolio_id: UUID
                                    ) -> PortfolioMetric | None:
        result = await self.session.execute(select(PortfolioMetric).where(
            PortfolioMetric.portfolio_id == portfolio_id))
        return result.scalar_one_or_none()

    async def list_agent_metrics(self) -> list[AgentMetric]:
        result = await self.session.execute(select(AgentMetric))
        return list(result.scalars().all())

    async def get_latest_portfolio_evaluation(self, portfolio_id: UUID
                                              ) -> PortfolioEvaluation | None:
        result = await self.session.execute(
            select(PortfolioEvaluation)
            .where(PortfolioEvaluation.portfolio_id == portfolio_id)
            .order_by(PortfolioEvaluation.created_at.desc())
            .limit(1))
        return result.scalar_one_or_none()
