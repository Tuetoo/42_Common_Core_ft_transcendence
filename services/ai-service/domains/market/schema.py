from datetime import datetime
from decimal import Decimal
from shared.schemas import BaseSchema, IDMixin


class PriceBarRead(BaseSchema, IDMixin):
    interval: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: float
    fetched_at: datetime
