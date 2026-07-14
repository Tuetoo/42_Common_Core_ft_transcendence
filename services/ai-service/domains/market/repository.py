from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from shared.exceptions import NotFoundError
from domains.market.model import Instrument, PriceBar


class InstrumentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_ticker(self, ticker: str) -> Instrument:
        result = await self.session.execute(
            select(Instrument).where(Instrument.ticker == ticker.upper()))
        instrument = result.scalar_one_or_none()
        if instrument is None:
            raise NotFoundError(f"Instrument {ticker} not found")
        return instrument

    async def upsert(self, ticker: str, **fields) -> Instrument:
        try:
            instrument = await self.get_by_ticker(ticker)
            for key, value in fields.items():
                setattr(instrument, key, value)
        except NotFoundError:
            instrument = Instrument(ticker=ticker.upper(), **fields)
            self.session.add(instrument)
        await self.session.flush()
        return instrument


class PriceBarRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_recent(self, instrument_id, interval: str, limit: int = 100
                          ) -> list[PriceBar]:
        result = await self.session.execute(
            select(PriceBar)
            .where(PriceBar.instrument_id == instrument_id,
                   PriceBar.interval == interval)
            .order_by(PriceBar.timestamp.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def insert_bar(self, instrument_id, **fields) -> PriceBar:
        bar = PriceBar(instrument_id=instrument_id, **fields)
        self.session.add(bar)
        await self.session.flush()
        return bar
