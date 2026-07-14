from fastapi import APIRouter
from api.internal import router as internal_router
from ai.api import router as analysis_router
from domains.market.api import router as market_router
from domains.analytics.api import router as analytics_router

api_router = APIRouter()
api_router.include_router(internal_router)
api_router.include_router(analysis_router, prefix="/analyses")
api_router.include_router(market_router, prefix="/market")
api_router.include_router(analytics_router, prefix="/analytics")
