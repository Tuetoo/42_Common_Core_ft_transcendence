from fastapi import APIRouter
from api.public import router as public_router
from api.apikeys_api import router as apikeys_router
from domains.portfolio.api import watchlist_router,  router as portfolio_router
from domains.research.api import router as research_router
from domains.notification.api import router as notification_router

api_router = APIRouter()
api_router.include_router(public_router)
api_router.include_router(apikeys_router)
api_router.include_router(portfolio_router, prefix="/portfolios")
api_router.include_router(watchlist_router, prefix="/watchlists")
api_router.include_router(research_router, prefix="/research")
api_router.include_router(notification_router, prefix="/notifications")
