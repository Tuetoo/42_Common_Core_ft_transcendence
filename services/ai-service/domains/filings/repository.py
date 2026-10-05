from datetime import date
from typing import Any
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from domains.filings.model import FilingRecord


class FilingRecordRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(
            self, *, ticker: str, filename: str, form: str, filing_date: date,
            report_date: date | None, records: dict[str, Any]) -> None:
        stmt = insert(FilingRecord).values(
            ticker=ticker.upper(), filename=filename, form=form,
            filing_date=filing_date, report_date=report_date, records=records)
        stmt = stmt.on_conflict_do_update(
            index_elements=[FilingRecord.filename], set_={
                "ticker": stmt.excluded.ticker,
                "form": stmt.excluded.form,
                "filing_date": stmt.excluded.filing_date,
                "report_date": stmt.excluded.report_date,
                "records": stmt.excluded.records
            })
        await self.session.execute(stmt)
        await self.session.flush()

    async def latest_for_ticker(self, ticker: str,
                                limit: int) -> list[FilingRecord]:
        result = await self.session.execute(
            select(FilingRecord).where(
                FilingRecord.ticker == ticker.upper()).order_by(
                    FilingRecord.filing_date.desc(),
                    FilingRecord.filename.desc()).limit(limit))
        return list(result.scalars().all())

    async def filenames_for_ticker(self, ticker: str) -> set[str]:
        result = await self.session.execute(
            select(FilingRecord.filename).where(
                FilingRecord.ticker == ticker.upper()))
        return set(result.scalars().all())
