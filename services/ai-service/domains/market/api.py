from datetime import UTC, datetime
from uuid import uuid4
from ai.providers.base import ProviderError
from ai.tools.market_data_cache import get_bars_cache_first
from deps import get_current_user
from fastapi import APIRouter, Depends, Query
from domains.market.repository import MAX_PRICE_LOOKBACK_DAYS
from domains.market.schema import PriceBarRead
from domains.market.service import NoPriceDataError
from shared.exceptions import (NotFoundError, UpstreamServiceError,
                               ValidationAppError)
from shared.schemas import APIResponse, normalize_ticker
from shared.security import CurrentUser

router = APIRouter(tags=["market"])

_ALLOWED_INTERVALS = ("1d", "1wk", "1mo")


def _validated_ticker(ticker: str) -> str:
    try:
        return normalize_ticker(ticker)
    except ValueError as exc:
        raise ValidationAppError(str(exc)) from exc


@router.get(
    "/{ticker}/prices", response_model=APIResponse[list[PriceBarRead]],
    description=(
        "Price bars of an instrument, newest first. Login required (the "
        "call can trigger a live provider fetch)."))
async def get_prices(
    ticker: str, interval: str = Query(default="1d"),
    lookback_days: int = Query(
        default=5, ge=1, le=MAX_PRICE_LOOKBACK_DAYS, description=(
            "Calendar days to cover, counted back from now. The default of "
            "5 still returns a bar after a weekend or a market holiday.")),
    user: CurrentUser = Depends(get_current_user)
) -> APIResponse[list[PriceBarRead]]:
    return await price_bars_response(ticker, interval, lookback_days)


async def price_bars_response(
        ticker: str, interval: str,
        lookback_days: int) -> APIResponse[list[PriceBarRead]]:
    ticker = _validated_ticker(ticker)
    if interval not in _ALLOWED_INTERVALS:
        raise ValidationAppError(
            f"Invalid interval {interval!r}; allowed: "
            f"{', '.join(_ALLOWED_INTERVALS)}")
    try:
        bars = await get_bars_cache_first(
            ticker, interval=interval, lookback_days=lookback_days)
    except NoPriceDataError:
        raise NotFoundError(f"No price data for ticker {ticker}") from None
    except ProviderError as exc:
        raise UpstreamServiceError(
            "Market data provider unavailable") from exc
    bars = list(reversed(bars))
    results: list[PriceBarRead] = []
    for bar in bars:
        if isinstance(bar, PriceBarRead):
            results.append(bar)
        else:
            results.append(
                PriceBarRead(
                    id=uuid4(), interval=bar.interval, timestamp=bar.timestamp,
                    open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                    volume=bar.volume, fetched_at=datetime.now(UTC)))
    return APIResponse(data=results)
