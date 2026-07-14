from uuid import UUID
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from shared.schemas.base import APIResponse
from database import get_db
from domains.analytics.schema import AgentPerformanceRead, PortfolioMetricRead
from domains.analytics.service import AnalyticsService

router = APIRouter(tags=["analytics"])


@router.get("/portfolio/{portfolio_id}",
            response_model=APIResponse[PortfolioMetricRead])
async def get_portfolio_analytics(
    portfolio_id: UUID, db: AsyncSession = Depends(get_db)
        ) -> APIResponse[PortfolioMetricRead]:
    result = await AnalyticsService(db).get_portfolio_metrics(portfolio_id)
    return APIResponse(data=result)


@router.get("/agents", response_model=APIResponse[list[AgentPerformanceRead]])
async def get_agent_analytics(db: AsyncSession = Depends(get_db)
                              ) -> APIResponse[list[AgentPerformanceRead]]:
    result = await AnalyticsService(db).get_agent_performance()
    return APIResponse(data=result)
