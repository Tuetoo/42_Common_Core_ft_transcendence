from datetime import datetime
from uuid import UUID
from pydantic import BaseModel
from shared.enums.notification import NotificationType
from shared.schemas.base import BaseSchema, IDMixin


class NotificationRead(BaseSchema, IDMixin):
    user_id: UUID
    type: NotificationType
    title: str
    content: str | None = None
    read_at: datetime | None = None
    created_at: datetime


class NotificationMarkReadRequest(BaseModel):
    notification_ids: list[UUID]
