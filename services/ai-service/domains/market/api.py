from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from shared.schemas.base import APIResponse
from database import get_db
from domains.market.schema import InstrumentRead, PriceBarRead
from domains.market.service import MarketService

router = APIRouter(tags=["market"])


@router.get("/{ticker}", response_model=APIResponse[InstrumentRead])
async def get_instrument(ticker: str, db: AsyncSession = Depends(get_db)
                         ) -> APIResponse[InstrumentRead]:
    result = await MarketService(db).get_instrument(ticker)
    return APIResponse(data=result)


@router.get("/{ticker}/prices", response_model=APIResponse[list[PriceBarRead]])
async def get_prices(ticker: str, interval: str = Query(default="1d"),
                     db: AsyncSession = Depends(get_db)
                     ) -> APIResponse[list[PriceBarRead]]:
    result = await MarketService(db).get_recent_prices(ticker, interval)
    return APIResponse(data=result)
