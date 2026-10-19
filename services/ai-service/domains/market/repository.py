from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from domains.market.model import Instrument, PriceBar
from shared.exceptions import NotFoundError

MAX_PRICE_LOOKBACK_DAYS = 3650
CACHED_BARS_READ_LIMIT = MAX_PRICE_LOOKBACK_DAYS + 2


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

    async def upsert(self, ticker: str, **fields: Any) -> Instrument:
        ticker = ticker.upper()
        stmt = pg_insert(Instrument).values(ticker=ticker, **fields)
        stmt = (
            stmt.on_conflict_do_update(index_elements=["ticker"], set_=fields)
            if fields else stmt.on_conflict_do_nothing(
                index_elements=["ticker"]))
        await self.session.execute(stmt)
        await self.session.flush()
        return await self.get_by_ticker(ticker)


class PriceBarRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_recent(
            self, instrument_id: UUID, interval: str, *,
            limit: int = CACHED_BARS_READ_LIMIT) -> list[PriceBar]:
        result = await self.session.execute(
            select(PriceBar).where(
                PriceBar.instrument_id == instrument_id,
                PriceBar.interval == interval).order_by(
                    PriceBar.timestamp.desc()).limit(limit))
        return list(result.scalars().all())

    async def upsert_bar(
            self, instrument_id: UUID, *, interval: str, timestamp: datetime,
            open: Decimal, high: Decimal, low: Decimal, close: Decimal,
            volume: float) -> None:
        values = {
            "instrument_id": instrument_id,
            "interval": interval,
            "timestamp": timestamp,
            "open": open,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "fetched_at": datetime.now(UTC)
        }
        stmt = pg_insert(PriceBar).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "interval", "timestamp"], set_={
                "open": open,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "fetched_at": values["fetched_at"]
            })
        await self.session.execute(stmt)
