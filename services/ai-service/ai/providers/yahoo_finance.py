import asyncio
import math
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
import structlog
import yfinance as yf  # type: ignore[import-untyped]
from ai.providers.base import (FundamentalDataProvider, FundamentalsSnapshot,
                               InstrumentMetadata, MarketDataProvider,
                               PriceBar, PriceQuote, ProviderRequestError)
from ai.providers.retry import call_with_retry, retry_everything

logger = structlog.get_logger(__name__)

_SOURCE = "yahoo_finance"

_CENTS = Decimal("0.01")

_EMPTY_PRICE_MAX_ATTEMPTS = 3
_EMPTY_PRICE_RETRY_DELAY_SECONDS = 1.5


def _clean_price(raw: float) -> Decimal:
    return Decimal(str(round(raw, 2))).quantize(_CENTS, rounding=ROUND_HALF_UP)


def _dataframe_to_bars(history: Any, *, ticker: str,
                       interval: str) -> list[PriceBar]:
    if history is None or history.empty:
        logger.warning(
            "yahoo_finance.empty_history", ticker=ticker, interval=interval)
        return []

    bars: list[PriceBar] = []
    dropped = 0
    for timestamp, row in history.iterrows():
        open_, high, low, close = (
            float(row["Open"]), float(row["High"]), float(row["Low"]),
            float(row["Close"]))
        if not (math.isfinite(open_) and math.isfinite(high)
                and math.isfinite(low) and math.isfinite(close)):
            dropped += 1
            continue
        bars.append(
            PriceBar(
                ticker=ticker, interval=interval,
                timestamp=timestamp.to_pydatetime(), open=_clean_price(open_),
                high=_clean_price(high), low=_clean_price(low),
                close=_clean_price(close), volume=int(row["Volume"]),
                source=_SOURCE))
    if dropped:
        logger.warning(
            "yahoo_finance.dropped_non_finite_bars", ticker=ticker,
            interval=interval, dropped=dropped, total_rows=dropped + len(bars))
    return bars


def _sync_get_info(ticker: str) -> dict[str, Any]:
    return yf.Ticker(ticker).info or {}


def _sync_get_history(
        ticker: str, *, interval: str, lookback_days: int) -> Any:
    return yf.Ticker(ticker).history(
        period=f"{max(lookback_days, 1)}d", interval=interval)


def _sync_get_history_range(
        ticker: str, *, start: date, end: date, interval: str) -> Any:
    return yf.Ticker(ticker).history(
        start=start, end=end + timedelta(days=1), interval=interval)


class YahooFinanceProvider(MarketDataProvider, FundamentalDataProvider):

    async def get_current_price(self, ticker: str) -> PriceQuote:
        info: dict[str, Any] = {}
        price_raw = None
        last_info_exc: Exception | None = None
        for attempt in range(_EMPTY_PRICE_MAX_ATTEMPTS):
            try:
                info = await call_with_retry(
                    lambda: asyncio.to_thread(_sync_get_info, ticker),
                    op_name=f"yahoo_finance.get_info[{ticker}]",
                    should_retry=retry_everything)
            except Exception as exc:
                last_info_exc = exc
                break
            price_raw = info.get("currentPrice") or info.get(
                "regularMarketPrice")
            if price_raw is not None:
                break
            if attempt < _EMPTY_PRICE_MAX_ATTEMPTS - 1:
                logger.info(
                    "yahoo_finance.empty_price_retrying", ticker=ticker,
                    attempt=attempt + 1)
                await asyncio.sleep(_EMPTY_PRICE_RETRY_DELAY_SECONDS)

        if price_raw is None:
            if last_info_exc is not None:
                raise ProviderRequestError(
                    f"yfinance info lookup failed for {ticker}"
                ) from last_info_exc
            try:
                recent_history = await asyncio.to_thread(
                    _sync_get_history, ticker, interval="1d", lookback_days=5)
                has_recent_bars = (
                    recent_history is not None and not recent_history.empty)
            except Exception:
                has_recent_bars = False
            if has_recent_bars:
                raise ProviderRequestError(
                    f"yfinance returned no current-price field for {ticker!r} "
                    f"after {_EMPTY_PRICE_MAX_ATTEMPTS} attempts, despite "
                    f"recent "
                    "price history existing -- likely a transient data gap, "
                    "not "
                    "an invalid/delisted ticker")
            raise ProviderRequestError(
                f"yfinance returned no price for {ticker!r} after "
                f"{_EMPTY_PRICE_MAX_ATTEMPTS} attempts, and no recent price "
                "history either -- likely an invalid/delisted ticker, though "
                "this cannot be confirmed with full certainty (yfinance gives "
                "no explicit signal distinguishing the two cases)")
        price = float(price_raw)
        if not math.isfinite(price):
            raise ProviderRequestError(
                f"yfinance returned a non-finite price for {ticker!r}")

        return PriceQuote(
            ticker=ticker, price=_clean_price(price),
            currency=info.get("currency",
                              "USD"), as_of=datetime.now(UTC), source=_SOURCE)

    async def get_recent_price_history(
            self, ticker: str, *, interval: str = "1d",
            lookback_days: int = 30) -> list[PriceBar]:
        try:
            history = await call_with_retry(
                lambda: asyncio.to_thread(
                    _sync_get_history, ticker, interval=interval,
                    lookback_days=lookback_days),
                op_name=f"yahoo_finance.get_history[{ticker}]",
                should_retry=retry_everything)
        except Exception as exc:
            raise ProviderRequestError(
                f"yfinance history lookup failed for {ticker}") from exc
        return _dataframe_to_bars(history, ticker=ticker, interval=interval)

    async def get_price_history_range(
            self, ticker: str, *, start: date, end: date,
            interval: str = "1d") -> list[PriceBar]:
        try:
            history = await call_with_retry(
                lambda: asyncio.to_thread(
                    _sync_get_history_range, ticker, start=start, end=end,
                    interval=interval),
                op_name=f"yahoo_finance.get_history_range[{ticker}]",
                should_retry=retry_everything)
        except Exception as exc:
            raise ProviderRequestError(
                f"yfinance history lookup failed for {ticker}") from exc
        return _dataframe_to_bars(history, ticker=ticker, interval=interval)

    async def get_instrument_metadata(
            self, ticker: str) -> InstrumentMetadata | None:
        try:
            info = await call_with_retry(
                lambda: asyncio.to_thread(_sync_get_info, ticker),
                op_name=f"yahoo_finance.get_info[{ticker}]",
                should_retry=retry_everything)
        except Exception as exc:
            raise ProviderRequestError(
                f"yfinance info lookup failed for {ticker}") from exc
        if not info:
            return None
        return InstrumentMetadata(
            name=info.get("longName") or info.get("shortName"),
            sector=info.get("sector"), exchange=info.get("fullExchangeName")
            or info.get("exchange"), asset_type=info.get("quoteType"))

    async def get_fundamentals(self, ticker: str) -> FundamentalsSnapshot:
        try:
            info = await call_with_retry(
                lambda: asyncio.to_thread(_sync_get_info, ticker),
                op_name=f"yahoo_finance.get_info[{ticker}]",
                should_retry=retry_everything)
        except Exception as exc:
            raise ProviderRequestError(
                f"yfinance info lookup failed for {ticker}") from exc

        if not info:
            raise ProviderRequestError(
                f"yfinance returned no data for {ticker!r}")

        return FundamentalsSnapshot(
            ticker=ticker, pe_ratio=info.get("trailingPE"),
            forward_pe=info.get("forwardPE"), pb_ratio=info.get("priceToBook"),
            revenue_growth_yoy=info.get("revenueGrowth"),
            gross_margin=info.get("grossMargins"),
            operating_margin=info.get("operatingMargins"),
            net_margin=info.get("profitMargins"), as_of=datetime.now(UTC),
            source=_SOURCE)
