from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID
from pydantic import (AfterValidator, BaseModel, ConfigDict, Field,
                      model_validator)
from shared.enums import Currency, TransactionType
from shared.schemas import (BaseSchema, IDMixin, RecommendationRef,
                            TimestampMixin, UserRef)
from shared.tickers import Ticker

PortfolioVisibility = Literal["private", "organization"]

_MAX_DIGITS = 30
_DECIMAL_PLACES = 18
PositiveAmount = Annotated[
    Decimal,
    Field(gt=0, max_digits=_MAX_DIGITS, decimal_places=_DECIMAL_PLACES)]
SignedAmount = Annotated[
    Decimal,
    Field(max_digits=_MAX_DIGITS, decimal_places=_DECIMAL_PLACES)]
NonNegativeAmount = Annotated[
    Decimal,
    Field(ge=0, max_digits=_MAX_DIGITS, decimal_places=_DECIMAL_PLACES)]

CASH_TICKER = "CASH"


def _reject_cash_ticker(value: str) -> str:
    if value == CASH_TICKER:
        raise ValueError(f"'{CASH_TICKER}' is reserved for cash movements.")
    return value


TradableTicker = Annotated[Ticker, AfterValidator(_reject_cash_ticker)]


class PortfolioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    currency: Currency = Currency.USD
    organization_id: UUID | None = None
    visibility: PortfolioVisibility = "private"


class PortfolioUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    visibility: PortfolioVisibility | None = None
    auto_analyze_at_open: bool | None = None


class PortfolioRead(BaseSchema, IDMixin, TimestampMixin):
    name: str
    currency: Currency
    visibility: str
    organization_id: UUID | None = None
    auto_analyze_at_open: bool
    owner: UserRef


class HoldingCreate(BaseModel):
    ticker: TradableTicker
    quantity: PositiveAmount
    average_cost: PositiveAmount


class HoldingUpdate(BaseModel):
    quantity: PositiveAmount | None = None
    average_cost: PositiveAmount | None = None


class HoldingRead(BaseSchema, IDMixin):
    portfolio_id: UUID
    ticker: str
    quantity: Decimal
    average_cost: Decimal
    latest_recommendation: RecommendationRef | None = None


class HoldingAdjustmentRead(HoldingRead):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Response of the holding create, update and delete endpoints. "
                "They record ADJUSTMENT transactions that bring the holding "
                "to "
                "the requested state and return the resulting holding plus "
                "those "
                "transactions. `transaction_id` is the last recorded row "
                "(null "
                "when the request changed nothing); a change of average cost "
                "needs two rows (all shares out, then the new quantity in at "
                "the "
                "new cost), listed in `transaction_ids`. After a delete "
                "`quantity` is 0 and the holding no longer exists.")
        })

    transaction_id: UUID | None = None
    transaction_ids: list[UUID] = Field(default_factory=list)


class TransactionCreate(BaseModel):
    ticker: Ticker
    transaction_type: TransactionType
    quantity: SignedAmount | None = None
    price: NonNegativeAmount | None = None
    dividend_per_share: PositiveAmount | None = None
    split_ratio: PositiveAmount | None = None
    transaction_date: datetime

    @model_validator(mode="after")
    def _check_ticker_and_date(self) -> "TransactionCreate":
        cash_types = (TransactionType.DEPOSIT, TransactionType.WITHDRAWAL)
        if (self.transaction_type not in cash_types
                and self.ticker == CASH_TICKER):
            raise ValueError(
                f"'{CASH_TICKER}' is reserved for deposits and withdrawals.")
        self._check_amounts()
        when = self.transaction_date
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if when > datetime.now(UTC) + timedelta(days=1):
            raise ValueError(
                "transaction_date cannot be more than 1 day in the future.")
        return self

    def _check_amounts(self) -> None:
        if self.transaction_type == TransactionType.ADJUSTMENT:
            if self.quantity is None or self.quantity == 0:
                raise ValueError(
                    "An adjustment requires a non-zero quantity (positive = "
                    "shares in, negative = shares out).")
            if self.quantity > 0 and self.price is None:
                raise ValueError(
                    "An adjustment that moves shares in requires a price "
                    "(the per-share cost basis, 0 or more).")
            if (self.dividend_per_share is not None
                    or self.split_ratio is not None):
                raise ValueError(
                    "dividend_per_share and split_ratio do not apply to an "
                    "adjustment.")
            return
        if self.quantity is not None and self.quantity <= 0:
            raise ValueError("quantity must be greater than 0.")
        if self.price is not None and self.price <= 0:
            raise ValueError("price must be greater than 0.")


class InternalTransactionCreate(TransactionCreate):
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=64)


class TransactionRead(BaseSchema, IDMixin):
    portfolio_id: UUID
    ticker: str
    transaction_type: TransactionType
    quantity: Decimal | None = None
    price: Decimal | None = None
    dividend_per_share: Decimal | None = None
    split_ratio: Decimal | None = None
    transaction_date: datetime


class PortfolioSnapshotRead(BaseSchema, IDMixin):
    portfolio_id: UUID
    snapshot_date: date
    total_value: Decimal
    cash_balance: Decimal
    total_value_converted: Decimal | None = None
    cash_balance_converted: Decimal | None = None
    fx_rate: Decimal | None = None
    display_currency: Currency | None = None


class WatchlistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class WatchlistUpdate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Rename a watchlist (`PATCH /api/v1/watchlists/{id}`).")
        })

    name: str = Field(min_length=1, max_length=128)


class WatchlistItemCreate(BaseModel):
    ticker: TradableTicker


class WatchlistItemRead(BaseSchema, IDMixin):
    watchlist_id: UUID
    ticker: str


class WatchlistRead(BaseSchema, IDMixin):
    name: str
    items: list[WatchlistItemRead] = Field(default_factory=list)
