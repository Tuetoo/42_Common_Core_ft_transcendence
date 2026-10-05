from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field
from shared.enums import NotificationType
from shared.schemas import BaseSchema, IDMixin


class NotificationRead(BaseSchema, IDMixin):
    user_id: UUID
    type: NotificationType
    title: str
    content: str | None = None
    read_at: datetime | None = None
    created_at: datetime


MAX_BULK_MARK_READ_IDS = 500


class NotificationMarkReadRequest(BaseModel):
    notification_ids: list[UUID] = Field(max_length=MAX_BULK_MARK_READ_IDS)


class NotificationMarkReadResponse(BaseModel):
    marked_count: int
