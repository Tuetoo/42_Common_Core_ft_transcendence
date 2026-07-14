from datetime import datetime, timezone
from typing import Generic, TypeVar
from shared.schemas.base import BaseSchema
from pydantic import Field

T = TypeVar("T")


class WebSocketEnvelope(BaseSchema, Generic[T]):
    event: str
    payload: T
    emitted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc))


class WebSocketErrorMessage(BaseSchema):
    event: str = "error"
    code: str
    message: str
