from uuid import UUID
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Numeric, String,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from shared.database import Base
from shared.models import TimestampColumnsMixin, UUIDPKMixin


class Portfolio(Base, UUIDPKMixin, TimestampColumnsMixin):
    __tablename__ = "portfolios"
    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True)
    organization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    currency: Mapped[str] = mapped_column(
        String, nullable=False, default="USD")
    visibility: Mapped[str] = mapped_column(
        String, nullable=False, default="private")
    auto_analyze_at_open: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)


class Holding(Base, UUIDPKMixin):
    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("portfolio_id", "ticker",
                                       name="uq_holding_portfolio_ticker"),)
    portfolio_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("portfolios.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    average_cost: Mapped[Decimal] = mapped_column(Numeric, nullable=False)


class Transaction(Base, UUIDPKMixin):
    __tablename__ = "transactions"
    portfolio_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("portfolios.id"), nullable=False,
        index=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    transaction_type: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    dividend_per_share: Mapped[Decimal | None] = mapped_column(
        Numeric, nullable=True)
    split_ratio: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    transaction_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)


class PortfolioSnapshot(Base, UUIDPKMixin):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "snapshot_date",
                         name="uq_snapshot_portfolio_date"),)
    portfolio_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("portfolios.id"), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_value: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(Numeric, nullable=False)


class Watchlist(Base, UUIDPKMixin):
    __tablename__ = "watchlists"
    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)


class WatchlistItem(Base, UUIDPKMixin):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint(
        "watchlist_id", "ticker", name="uq_watchlist_item_watchlist_ticker"),)
    watchlist_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("watchlists.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
