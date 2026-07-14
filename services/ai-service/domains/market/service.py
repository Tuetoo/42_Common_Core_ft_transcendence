from sqlalchemy.ext.ext.asyncio import AsyncSession
from domains.market.repository import InstrumentRepository, PriceBarRepository
from domains.market.schema import InstrumentRead, PriceBarRead


class MarketService:
    def __init__(self, session: AsyncSession):
        self.instruments = InstrumentRepository(session)
        self.price_bars = PriceBarRepository(session)

    async def get_instrument(self, ticker: str) -> InstrumentRead:
        instrument = await self.instruments.get_by_ticker(ticker)
        return InstrumentRead.model_validate(instrument)

    async def get_recent_prices(self, ticker: str, interval: str = "1d"
                                ) -> list[PriceBarRead]:
        instrument = await self.instruments.get_by_ticker(ticker)
        bars = await self.price_bars.list_recent(instrument.id, interval)
        return [PriceBarRead.model_validate(bar) for bar in bars]

    async def refresh_from_external_provider(self, ticker: str) -> None:
        # TODO fetch and cache fresh price data from external market API
        raise NotImplementedError
