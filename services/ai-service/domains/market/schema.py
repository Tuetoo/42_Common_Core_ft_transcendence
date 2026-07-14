from datetime import datetime
from shared.enums.market import AssetType
from shared.schemas.base import BaseSchema, IDMixin


class InstrumentRead(BaseSchema, IDMixin):
    ticker: str
    name: str | None
    asset_type: AssetType
    currency: str
    sector: str | None
    exchange: str | None


class PriceBarRead(BaseSchema, IDMixin):
    interval: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class TechnicalIndicatorRead(BaseSchema):
    indicator: str
    computed_at: datetime
    value: dict
