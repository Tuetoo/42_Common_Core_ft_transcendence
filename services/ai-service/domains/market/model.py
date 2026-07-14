from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime
from shared.database import Base
from shared.models import UUIDPKMixin


class Instrument(Base, UUIDPKMixin):
    __tablename__ = "instruments"
    ticker: Mapped[str] = mapped_column(String(10), unique=True,
                                        nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    asset_type: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False,
                                          default="USD")
    sector: Mapped[str | None] = mapped_column(String(100))
    exchange: Mapped[str | None] = mapped_column(String(50))
    price_bars: Mapped[list["PriceBar"]] = relationship(
        back_populates="instrument")


class PriceBar(Base, UUIDPKMixin):
    __tablename__ = "price_bars"
    instrument_id: Mapped[PGUUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False)
    interval: Mapped[str] = mapped_column(String(5), nullable=False)
    timestamp: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                nullable=False)
    open: Mapped[float] = mapped_column(Numeric, nullable=False)
    high: Mapped[float] = mapped_column(Numeric, nullable=False)
    low: Mapped[float] = mapped_column(Numeric, nullable=False)
    close: Mapped[float] = mapped_column(Numeric, nullable=False)
    volume: Mapped[float] = mapped_column(Numeric, nullable=False)
    instrument: Mapped["Instrument"] = relationship(
        back_populates="price_bars")


class TechnicalIndicatorCache(Base, UUIDPKMixin):
    __tablename__ = "technical_indicator_cache"
    instrument_id: Mapped[PGUUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False)
    indicator: Mapped[str] = mapped_column(String(20), nullable=False)
    computed_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                  nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
