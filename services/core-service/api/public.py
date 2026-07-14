from uuid import UUID
from fastapi import APIRouter

router = APIRouter(prefix="/public", tags=["public-api"])


@router.get("/portfolios/{portfolio_id}")
async def get_portfolio(portfolio_id: UUID):
    # TODO implementations call into domains/portfolio
    raise NotImplementedError


@router.get("/portfolios/{portfolio_id}/holdings")
async def list_holdings(portfolio_id: UUID, page: int = 1,
                        page_size: int = 20):
    # TODO
    raise NotImplementedError


@router.post("/watchlists/{watchlist_id}/items")
async def add_watchlist_item(watchlist_id: UUID, ticker: str):
    # TODO
    raise NotImplementedError


@router.put("/portfolios/{portfolio_id}/holdings/{holding_id}")
async def update_holding(portfolio_id: UUID, holding_id: UUID,
                         quantity: float, average_cost: float):
    # TODO
    raise NotImplementedError


@router.delete("/watchlists/{watchlist_id}/items/{ticker}")
async def remove_watchlist_item(watchlist_id: UUID, ticker: str):
    # TODO
    raise NotImplementedError
