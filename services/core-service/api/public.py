from uuid import UUID
from decimal import Decimal
from typing import Any, cast
from fastapi import APIRouter, Depends, Query, Request
from database import get_db
from domains.portfolio.schema import (
    HoldingRead, HoldingUpdate, PortfolioRead, WatchlistItemCreate,
    WatchlistItemRead)
from domains.portfolio.service import PortfolioService
from sqlalchemy.ext.asyncio import AsyncSession
from shared.exceptions import ValidationAppError
from shared.schemas.base import (APIResponse, PaginatedResponse,
                                 PaginationParams)

router = APIRouter(prefix="/public", tags=["public-api"])


def _owner_user_id(request: Request) -> UUID:
    owner = getattr(request.state, "api_key_owner_user_id", None)
    if owner is None:
        raise ValidationAppError(
            "This API key is scoped to an organization, not a user -- "
            "organization-scoped keys are not yet supported by this endpoint.")
    return cast(UUID, owner)


@router.get("/portfolios/{portfolio_id}",
            response_model=APIResponse[PortfolioRead])
async def get_portfolio(portfolio_id: UUID, request: Request,
                        db: AsyncSession = Depends(get_db)
                        ) -> APIResponse[PortfolioRead]:
    owner_id = _owner_user_id(request)
    result = await PortfolioService(db).get(portfolio_id, owner_id)
    return APIResponse(data=result)


@router.get("/portfolios/{portfolio_id}/holdings",
            response_model=PaginatedResponse[HoldingRead])
async def list_holdings(
    portfolio_id: UUID, request: Request, page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db)
) -> PaginatedResponse[HoldingRead]:
    owner_id = _owner_user_id(request)
    holdings = await PortfolioService(db).list_holdings(portfolio_id, owner_id)
    pagination = PaginationParams(page=page, page_size=page_size)
    page_items = holdings[pagination.offset:pagination.offset
                          + pagination.limit]
    return PaginatedResponse(
        items=page_items, total=len(holdings), page=pagination.page,
        page_size=pagination.page_size)


@router.post("/watchlists/{watchlist_id}/items",
             response_model=APIResponse[WatchlistItemRead])
async def add_watchlist_item(watchlist_id: UUID, ticker: str, request: Request,
                             db: AsyncSession = Depends(get_db)
                             ) -> APIResponse[WatchlistItemRead]:
    owner_id = _owner_user_id(request)
    result = await PortfolioService(db).add_watchlist_item(
        watchlist_id, owner_id, WatchlistItemCreate(ticker=ticker))
    return APIResponse(data=result)


@router.put("/portfolios/{portfolio_id}/holdings/{holding_id}",
            response_model=APIResponse[HoldingRead])
async def update_holding(
    portfolio_id: UUID, holding_id: UUID, quantity: Decimal,
    average_cost: Decimal, request: Request, db: AsyncSession = Depends(get_db)
) -> APIResponse[HoldingRead]:
    owner_id = _owner_user_id(request)
    result = await PortfolioService(db).update_holding(
        portfolio_id, owner_id, holding_id,
        HoldingUpdate(quantity=quantity, average_cost=average_cost))
    return APIResponse(data=result)


@router.delete("/watchlists/{watchlist_id}/items/{ticker}")
async def remove_watchlist_item(
    watchlist_id: UUID, ticker: str, request: Request,
    db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    owner_id = _owner_user_id(request)
    await PortfolioService(db).remove_watchlist_item(
        watchlist_id, owner_id, ticker)
    return {"removed": True}
