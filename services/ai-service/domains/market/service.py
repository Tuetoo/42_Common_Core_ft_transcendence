from typing import cast
import structlog
from ai.providers.base import MarketDataProvider, PriceBar, ProviderError
from ai.providers.registry import get_market_data_provider
from sqlalchemy.ext.asyncio import AsyncSession
from domains.market.repository import InstrumentRepository, PriceBarRepository
from domains.market.schema import PriceBarRead
from shared.enums import AssetType
from shared.exceptions import NotFoundError

logger = structlog.get_logger(__name__)

_PROVIDER_ASSET_TYPE_MAP = {
    "EQUITY": AssetType.EQUITY,
    "ETF": AssetType.ETF,
    "CRYPTOCURRENCY": AssetType.CRYPTO
}


class NoPriceDataError(ProviderError):
    pass


def _map_provider_asset_type(raw: str | None) -> str:
    if raw is None:
        return AssetType.EQUITY.value
    return _PROVIDER_ASSET_TYPE_MAP.get(raw.upper(), AssetType.EQUITY).value


class MarketService:

    def __init__(self, session: AsyncSession):
        self.instruments = InstrumentRepository(session)
        self.price_bars = PriceBarRepository(session)

    async def get_recent_prices(self, ticker: str,
                                interval: str = "1d") -> list[PriceBarRead]:
        instrument = await self.instruments.get_by_ticker(ticker)
        bars = await self.price_bars.list_recent(instrument.id, interval)
        return [PriceBarRead.model_validate(bar) for bar in bars]

    async def refresh_from_external_provider(
            self, ticker: str, *, interval: str = "1d",
            lookback_days: int = 30,
            provider: MarketDataProvider | None = None) -> int:
        provider = provider or get_market_data_provider()
        bars = await provider.get_recent_price_history(
            ticker, interval=interval, lookback_days=lookback_days)
        if not bars:
            try:
                await self.instruments.get_by_ticker(ticker)
            except NotFoundError:
                raise NoPriceDataError(
                    f"No price data for ticker {ticker!r}") from None
            return 0

        try:
            instrument = await self.instruments.get_by_ticker(ticker)
        except NotFoundError:
            instrument = await self.instruments.upsert(
                ticker, asset_type=AssetType.EQUITY.value)

        if instrument.name is None:
            try:
                metadata = await provider.get_instrument_metadata(ticker)
            except ProviderError as exc:
                logger.info(
                    "market.instrument_metadata_lookup_failed", ticker=ticker,
                    error=str(exc))
                metadata = None
            if metadata is not None:
                fields = {
                    k: v
                    for k, v in metadata.model_dump().items() if v is not None
                }
                if fields:
                    fields["asset_type"] = _map_provider_asset_type(
                        metadata.asset_type)
                    instrument = await self.instruments.upsert(
                        ticker, **fields)

        if interval == "1d":
            import ai.tools.market_data_cache as cache

            bars = cast(
                list[PriceBar],
                cache.completed_daily_bars(bars, cache._utcnow()))
        for bar in bars:
            await self.price_bars.upsert_bar(
                instrument.id, interval=bar.interval, timestamp=bar.timestamp,
                open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                volume=bar.volume)
        return len(bars)
