from datetime import datetime, UTC
from typing import Generic, TypeVar
from shared.schemas.base import BaseSchema
from pydantic import Field

T = TypeVar("T")


class WebSocketEnvelope(BaseSchema, Generic[T]):
    event: str
    payload: T
    emitted_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC))


class WebSocketErrorMessage(BaseSchema):
    event: str = "error"
    code: str
    message: str
