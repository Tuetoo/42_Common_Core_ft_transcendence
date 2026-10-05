from abc import ABC, abstractmethod
from datetime import date, datetime
from decimal import Decimal
from pydantic import BaseModel, Field


class ProviderError(Exception):
    pass


class ProviderUnavailableError(ProviderError):
    pass


class ProviderRequestError(ProviderError):
    pass


class ProviderQuotaError(ProviderRequestError):
    pass


class PriceQuote(BaseModel):
    ticker: str
    price: Decimal
    currency: str = "USD"
    as_of: datetime
    source: str


class InstrumentMetadata(BaseModel):
    name: str | None = None
    sector: str | None = None
    exchange: str | None = None
    asset_type: str | None = None


class PriceBar(BaseModel):
    ticker: str
    interval: str
    timestamp: datetime
    open: Decimal = Field(allow_inf_nan=True)
    high: Decimal = Field(allow_inf_nan=True)
    low: Decimal = Field(allow_inf_nan=True)
    close: Decimal = Field(allow_inf_nan=True)
    volume: int
    source: str


class FundamentalsSnapshot(BaseModel):
    ticker: str
    pe_ratio: float | None = None
    forward_pe: float | None = None
    pb_ratio: float | None = None
    revenue_growth_yoy: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    as_of: datetime
    source: str
    unavailable_notes: list[str] = Field(default_factory=list)


class NewsArticle(BaseModel):
    title: str
    source_name: str
    url: str
    published_at: datetime
    summary: str | None = None


class MacroDataPoint(BaseModel):
    series_id: str
    label: str
    value: float
    unit: str | None = None
    observed_at: date
    source: str


class WebSearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    published_at: datetime | None = None
    source: str = "web_search"


class MarketDataProvider(ABC):

    @abstractmethod
    async def get_current_price(self, ticker: str) -> PriceQuote:
        ...

    @abstractmethod
    async def get_recent_price_history(
            self, ticker: str, *, interval: str = "1d",
            lookback_days: int = 30) -> list[PriceBar]:
        ...

    @abstractmethod
    async def get_price_history_range(
            self, ticker: str, *, start: date, end: date,
            interval: str = "1d") -> list[PriceBar]:
        ...

    @abstractmethod
    async def get_instrument_metadata(
            self, ticker: str) -> InstrumentMetadata | None:
        ...


class FundamentalDataProvider(ABC):

    @abstractmethod
    async def get_fundamentals(self, ticker: str) -> FundamentalsSnapshot:
        ...


class NewsProvider(ABC):

    @abstractmethod
    async def search_news(
            self, query: str, *, since: datetime | None = None,
            limit: int = 20) -> list[NewsArticle]:
        ...


class MacroDataProvider(ABC):

    @abstractmethod
    async def get_indicator(self, series: str, *,
                            lookback_days: int = 365) -> list[MacroDataPoint]:
        ...


class WebSearchProvider(ABC):

    @abstractmethod
    async def search(self, query: str, *,
                     limit: int = 5) -> list[WebSearchResult]:
        ...
