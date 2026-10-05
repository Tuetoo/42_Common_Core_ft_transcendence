from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID
from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from domains.notification.model import Notification
from shared.database import count_query
from shared.schemas import PaginationParams


class NotificationRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def create(
            self, *, user_id: UUID, type: str, title: str,
            content: str | None) -> Notification:
        notification = Notification(
            user_id=user_id, type=type, title=title, content=content)
        self._db.add(notification)
        await self._db.flush()
        return notification

    async def get_by_id(self, notification_id: UUID) -> Notification | None:
        result = await self._db.execute(
            select(Notification).where(Notification.id == notification_id))
        return result.scalar_one_or_none()

    async def list_for_user(
            self, user_id: UUID, *, unread_only: bool,
            pagination: PaginationParams) -> tuple[list[Notification], int]:
        stmt = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        total = await count_query(self._db, stmt)
        result = await self._db.execute(
            stmt.order_by(
                Notification.created_at.desc(), Notification.id.desc()).offset(
                    pagination.offset).limit(pagination.limit))
        return list(result.scalars().all()), total

    async def mark_read_bulk(
            self, notification_ids: list[UUID], user_id: UUID) -> int:
        result = await self._db.execute(
            update(Notification).where(
                Notification.id.in_(notification_ids),
                Notification.user_id == user_id,
                Notification.read_at.is_(None)).values(
                    read_at=datetime.now(UTC)))
        await self._db.flush()
        return cast(CursorResult[Any], result).rowcount

    async def delete_read_before(self, cutoff: datetime, limit: int) -> int:
        batch = (
            select(Notification.id).where(
                Notification.read_at.is_not(None), Notification.created_at
                < cutoff).limit(limit).scalar_subquery())
        result = await self._db.execute(
            delete(Notification).where(
                Notification.id.in_(batch)).execution_options(
                    synchronize_session=False))
        return cast(CursorResult[Any], result).rowcount
