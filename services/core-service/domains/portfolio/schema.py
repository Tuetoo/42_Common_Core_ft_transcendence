from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, Field
from shared.enums.market import TransactionType
from shared.schemas.base import BaseSchema, IDMixin, TimestampMixin
from shared.schemas.refs import RecommendationRef, UserRef


class PortfolioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    currency: str = "USD"
    organization_id: UUID | None = None
    visibility: str = "private"


class PortfolioUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    visibility: str | None = None
    auto_analyze_at_open: bool | None = None


class PortfolioRead(BaseSchema, IDMixin, TimestampMixin):
    name: str
    currency: str
    visibility: str
    organization_id: UUID | None = None
    auto_analyze_at_open: bool
    owner: UserRef


class HoldingCreate(BaseModel):
    ticker: str = Field(min_length=1, max_length=16)
    quantity: Decimal = Field(gt=0)
    average_cost: Decimal = Field(gt=0)


class HoldingUpdate(BaseModel):
    quantity: Decimal | None = Field(default=None, gt=0)
    average_cost: Decimal | None = Field(default=None, gt=0)


class HoldingRead(BaseSchema, IDMixin):
    portfolio_id: UUID
    ticker: str
    quantity: Decimal
    average_cost: Decimal
    latest_recommendation: RecommendationRef | None = None


class TransactionCreate(BaseModel):
    ticker: str = Field(min_length=1, max_length=16)
    transaction_type: TransactionType
    quantity: Decimal | None = Field(default=None, gt=0)
    price: Decimal | None = Field(default=None, gt=0)
    dividend_per_share: Decimal | None = Field(default=None, gt=0)
    transaction_date: datetime


class TransactionRead(BaseSchema, IDMixin):
    portfolio_id: UUID
    ticker: str
    transaction_type: TransactionType
    quantity: Decimal | None = None
    price: Decimal | None = None
    dividend_per_share: Decimal | None = None
    transaction_date: datetime


class PortfolioSnapshotRead(BaseSchema, IDMixin):
    portfolio_id: UUID
    snapshot_date: date
    total_value: Decimal
    cash_balance: Decimal


class WatchlistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class WatchlistItemCreate(BaseModel):
    ticker: str = Field(min_length=1, max_length=16)


class WatchlistItemRead(BaseSchema, IDMixin):
    watchlist_id: UUID
    ticker: str


class WatchlistRead(BaseSchema, IDMixin):
    name: str
    items: list[WatchlistItemRead] = Field(default_factory=list)
