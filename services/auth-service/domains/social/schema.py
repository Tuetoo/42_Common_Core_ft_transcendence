from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field
from domains.auth.schema import UserPublicRead
from shared.enums.connection import ConnectionStatus
from shared.schemas.base import BaseSchema, IDMixin


class ConnectionRequestCreate(BaseModel):
    addressee_username: str = Field(min_length=3, max_length=32)


class ConnectionRead(BaseSchema, IDMixin):
    requester_id: UUID
    addressee_id: UUID
    status: ConnectionStatus
    created_at: datetime
    responded_at: datetime | None = None
    other_user: UserPublicRead