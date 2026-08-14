from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from domains.notification.model import Notification
from shared.database import count_query


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def create(self, *, user_id: UUID, type: str, title: str,
                     content: str | None) -> Notification:
        notification = Notification(user_id=user_id, type=type, title=title,
                                    content=content)
        self._db.add(notification)
        await self._db.flush()
        return notification

    async def get_by_id(self, notification_id: UUID) -> Notification | None:
        result = await self._db.execute(
            select(Notification).where(Notification.id == notification_id))
        return result.scalar_one_or_none()

    async def list_for_user(
            self, user_id: UUID, *, unread_only: bool, offset: int, limit: int
            ) -> tuple[list[Notification], int]:
        stmt = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        total = await count_query(self._db, stmt)
        result = await self._db.execute(stmt.order_by(
            Notification.created_at.desc()).offset(offset).limit(limit))
        return list(result.scalars().all()), total
