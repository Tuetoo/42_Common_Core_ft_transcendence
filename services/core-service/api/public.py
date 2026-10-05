from typing import cast
from uuid import UUID
from database import get_db
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from domains.portfolio.api import (notify_holding_adjustment,
                                   notify_watchlist_item)
from domains.portfolio.schema import (HoldingAdjustmentRead, HoldingRead,
                                      HoldingUpdate, PortfolioRead,
                                      WatchlistItemCreate, WatchlistItemRead)
from domains.portfolio.service import PortfolioService
from shared.schemas import (MAX_PAGE, APIResponse, PaginatedResponse,
                            PaginationParams)

router = APIRouter(prefix="/public", tags=["public-api"])


def _owner_user_id(request: Request) -> UUID:
    return cast(UUID, request.state.api_key_owner_user_id)


@router.get(
    "/portfolios/{portfolio_id}", response_model=APIResponse[PortfolioRead])
async def get_portfolio(
    portfolio_id: UUID, request: Request,
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[PortfolioRead]:
    owner_id = _owner_user_id(request)
    result = await PortfolioService(db).get(portfolio_id, owner_id)
    return APIResponse(data=result)


@router.get(
    "/portfolios/{portfolio_id}/holdings",
    response_model=PaginatedResponse[HoldingRead])
async def list_holdings(
    portfolio_id: UUID, request: Request,
    page: int = Query(default=1, ge=1, le=MAX_PAGE),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db, scope="function")
) -> PaginatedResponse[HoldingRead]:
    owner_id = _owner_user_id(request)
    holdings = await PortfolioService(db).list_holdings(portfolio_id, owner_id)
    pagination = PaginationParams(page=page, page_size=page_size)
    page_items = holdings[pagination.offset:pagination.offset +
                          pagination.limit]
    return PaginatedResponse(
        items=page_items, total=len(holdings), page=pagination.page,
        page_size=pagination.page_size)


@router.post(
    "/watchlists/{watchlist_id}/items", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[WatchlistItemRead])
async def add_watchlist_item(
    watchlist_id: UUID, data: WatchlistItemCreate, request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[WatchlistItemRead]:
    owner_id = _owner_user_id(request)
    service = PortfolioService(db)
    result = await service.add_watchlist_item(watchlist_id, owner_id, data)
    notify_watchlist_item(
        background_tasks, owner_id, await
        service.get_watchlist_name(watchlist_id), result.ticker, added=True)
    return APIResponse(data=result)


@router.put(
    "/portfolios/{portfolio_id}/holdings/{holding_id}",
    response_model=APIResponse[HoldingAdjustmentRead])
async def update_holding(
    portfolio_id: UUID, holding_id: UUID, data: HoldingUpdate,
    request: Request, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[HoldingAdjustmentRead]:
    owner_id = _owner_user_id(request)
    service = PortfolioService(db)
    result = await service.update_holding(
        portfolio_id, owner_id, holding_id, data)
    notify_holding_adjustment(
        background_tasks, owner_id, await service.portfolio_name(portfolio_id),
        result.ticker, await
        service.transaction_quantities(result.transaction_ids),
        source=" (API key)")
    return APIResponse(data=result)


@router.delete(
    "/watchlists/{watchlist_id}/items/{ticker}",
    status_code=status.HTTP_204_NO_CONTENT)
async def remove_watchlist_item(
    watchlist_id: UUID, ticker: str, request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    owner_id = _owner_user_id(request)
    service = PortfolioService(db)
    await service.remove_watchlist_item(watchlist_id, owner_id, ticker)
    notify_watchlist_item(
        background_tasks, owner_id, await
        service.get_watchlist_name(watchlist_id), ticker.upper(), added=False)
