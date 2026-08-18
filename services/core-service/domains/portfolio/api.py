from uuid import UUID
from database import get_db
from deps import get_current_user
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from domains.portfolio.schema import (
    HoldingCreate, HoldingRead, HoldingUpdate, PortfolioCreate, PortfolioRead,
    PortfolioUpdate, TransactionCreate, TransactionRead, WatchlistCreate,
    WatchlistItemCreate, WatchlistItemRead, WatchlistRead)
from domains.portfolio.service import PortfolioService
from shared.schemas.base import (APIResponse, PaginatedResponse,
                                 PaginationParams)
from shared.security import CurrentUser

router = APIRouter(tags=["portfolios"])
watchlist_router = APIRouter(tags=["watchlists"])


@router.post("", status_code=status.HTTP_201_CREATED,
             response_model=APIResponse[PortfolioRead])
async def create_portfolio(
    data: PortfolioCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[PortfolioRead]:
    result = await PortfolioService(db).create(current_user.user_id, data)
    return APIResponse(data=result)


@router.get("", response_model=PaginatedResponse[PortfolioRead])
async def list_portfolios(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> PaginatedResponse[PortfolioRead]:
    pagination = PaginationParams(page=page, page_size=page_size)
    items, total = await PortfolioService(db).list_mine(
        current_user.user_id, pagination=pagination)
    return PaginatedResponse(
        items=items, total=total, page=pagination.page,
        page_size=pagination.page_size)


@router.get("/{portfolio_id}", response_model=APIResponse[PortfolioRead])
async def get_portfolio(portfolio_id: UUID,
                        current_user: CurrentUser = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)
                        ) -> APIResponse[PortfolioRead]:
    result = await PortfolioService(db).get(portfolio_id, current_user.user_id)
    return APIResponse(data=result)


@router.patch("/{portfolio_id}", response_model=APIResponse[PortfolioRead])
async def update_portfolio(
    portfolio_id: UUID, data: PortfolioUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[PortfolioRead]:
    result = await PortfolioService(db).update(
        portfolio_id, current_user.user_id, data)
    return APIResponse(data=result)


@router.delete("/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_portfolio(
    portfolio_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> None:
    await PortfolioService(db).delete(portfolio_id, current_user.user_id)


@router.post("/{portfolio_id}/holdings", status_code=status.HTTP_201_CREATED,
             response_model=APIResponse[HoldingRead])
async def add_holding(portfolio_id: UUID, data: HoldingCreate,
                      current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)
                      ) -> APIResponse[HoldingRead]:
    result = await PortfolioService(db).add_holding(
        portfolio_id, current_user.user_id, data)
    return APIResponse(data=result)


@router.get("/{portfolio_id}/holdings",
            response_model=APIResponse[list[HoldingRead]])
async def list_holdings(
    portfolio_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[list[HoldingRead]]:
    result = await PortfolioService(db).list_holdings(
        portfolio_id, current_user.user_id)
    return APIResponse(data=result)


@router.patch("/{portfolio_id}/holdings/{holding_id}",
              response_model=APIResponse[HoldingRead])
async def update_holding(
    portfolio_id: UUID, holding_id: UUID, data: HoldingUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[HoldingRead]:
    result = await PortfolioService(db).update_holding(
        portfolio_id, current_user.user_id, holding_id, data)
    return APIResponse(data=result)


@router.delete("/{portfolio_id}/holdings/{holding_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def delete_holding(portfolio_id: UUID, holding_id: UUID,
                         current_user: CurrentUser = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> None:
    await PortfolioService(db).delete_holding(
        portfolio_id, current_user.user_id, holding_id)


@router.post("/{portfolio_id}/transactions",
             status_code=status.HTTP_201_CREATED,
             response_model=APIResponse[TransactionRead])
async def record_transaction(
    portfolio_id: UUID, data: TransactionCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[TransactionRead]:
    result = await PortfolioService(db).record_transaction(
        portfolio_id, current_user.user_id, data)
    return APIResponse(data=result)


@router.get("/{portfolio_id}/transactions",
            response_model=PaginatedResponse[TransactionRead])
async def list_transactions(
    portfolio_id: UUID, page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> PaginatedResponse[TransactionRead]:
    pagination = PaginationParams(page=page, page_size=page_size)
    items, total = await PortfolioService(db).list_transactions(
        portfolio_id, current_user.user_id, pagination=pagination)
    return PaginatedResponse(
        items=items, total=total, page=page, page_size=page_size)


@watchlist_router.post("", status_code=status.HTTP_201_CREATED,
                       response_model=APIResponse[WatchlistRead])
async def create_watchlist(
    data: WatchlistCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[WatchlistRead]:
    result = await PortfolioService(db).create_watchlist(
        current_user.user_id, data)
    return APIResponse(data=result)


@watchlist_router.get("", response_model=APIResponse[list[WatchlistRead]])
async def list_watchlists(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[list[WatchlistRead]]:
    result = await PortfolioService(db).list_my_watchlists(
        current_user.user_id)
    return APIResponse(data=result)


@watchlist_router.post("/{watchlist_id}/items",
                       status_code=status.HTTP_201_CREATED,
                       response_model=APIResponse[WatchlistItemRead])
async def add_watchlist_item(
    watchlist_id: UUID, data: WatchlistItemCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[WatchlistItemRead]:
    result = await PortfolioService(db).add_watchlist_item(
        watchlist_id, current_user.user_id, data)
    return APIResponse(data=result)


@watchlist_router.delete("/{watchlist_id}/items/{ticker}",
                         status_code=status.HTTP_204_NO_CONTENT)
async def remove_watchlist_item(
    watchlist_id: UUID, ticker: str,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> None:
    await PortfolioService(db).remove_watchlist_item(
        watchlist_id, current_user.user_id, ticker)
