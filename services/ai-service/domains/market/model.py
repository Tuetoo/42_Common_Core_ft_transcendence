from datetime import datetime
from decimal import Decimal
from uuid import UUID
from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime
from shared.database import Base, UUIDPKMixin


class Instrument(Base, UUIDPKMixin):
    __tablename__ = "instruments"
    ticker: Mapped[str] = mapped_column(
        String(10), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    asset_type: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD")
    sector: Mapped[str | None] = mapped_column(String(100))
    exchange: Mapped[str | None] = mapped_column(String(50))
    price_bars: Mapped[list["PriceBar"]] = relationship(
        back_populates="instrument")


class PriceBar(Base, UUIDPKMixin):
    __tablename__ = "price_bars"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "interval", "timestamp",
            name="uq_price_bars_instrument_interval_timestamp"), )
    instrument_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False)
    interval: Mapped[str] = mapped_column(String(5), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    volume: Mapped[float] = mapped_column(Numeric, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    instrument: Mapped["Instrument"] = relationship(
        back_populates="price_bars")
