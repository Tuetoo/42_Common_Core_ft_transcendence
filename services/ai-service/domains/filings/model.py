from datetime import date, datetime
from typing import Any
from sqlalchemy import Date, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime
from shared.database import Base, UUIDPKMixin


class FilingRecord(Base, UUIDPKMixin):
    __tablename__ = "filing_records"

    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    filename: Mapped[str] = mapped_column(
        String(200), nullable=False, unique=True)
    form: Mapped[str] = mapped_column(String(20), nullable=False)
    filing_date: Mapped[date] = mapped_column(Date, nullable=False)
    report_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    records: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_filing_records_ticker_filing_date", "ticker",
              "filing_date"), )
