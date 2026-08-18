from datetime import UTC, datetime
from uuid import UUID
import clients.auth_client as auth_client
import structlog
from config import settings
from sqlalchemy.ext.asyncio import AsyncSession
from websocket_manager import manager
from domains.notification.repository import NotificationRepository
from domains.notification.schema import NotificationRead
from shared.email import send_notification_email
from shared.enums.notification import NotificationType, NotificationChannel
from shared.exceptions import NotFoundError, PermissionDeniedError
from shared.schemas.websocket import WebSocketEnvelope
from shared.schemas.base import PaginationParams

logger = structlog.get_logger(__name__)


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self.notifications = NotificationRepository(session)

    async def create_notification(
            self, *, user_id: UUID, type: NotificationType, title: str,
            content: str | None = None) -> NotificationRead:
        notification = await self.notifications.create(
            user_id=user_id, type=type, title=title, content=content)
        read_model = NotificationRead.model_validate(notification)
        try:
            info = await auth_client.get_notification_settings(str(user_id))
        except Exception:
            logger.warning("notification_settings_lookup_failed",
                           user_id=str(user_id))
            return read_model
        preferences = info.get("notification_preferences") or {}
        if preferences.get(NotificationChannel.WEBSOCKET.value):
            try:
                await manager.broadcast(f"user:{user_id}", WebSocketEnvelope(
                    event="notification",
                    payload=read_model.model_dump(mode="json")))
            except Exception:
                logger.warning("notification_websocket_broadcast_falied",
                               user_id=str(user_id))
        if (preferences.get(NotificationChannel.EMAIL.value)
                and info.get("email")):
            try:
                await send_notification_email(
                    settings=settings, to=info["email"], title=title,
                    content=content)
            except Exception:
                logger.warning("notification_email_send_failed",
                               user_id=str(user_id))
        return read_model

    async def list_for_user(
            self, user_id: UUID, *, unread_only: bool,
            pagination: PaginationParams
            ) -> tuple[list[NotificationRead], int]:
        rows, total = await self.notifications.list_for_user(
            user_id, unread_only=unread_only, pagination=pagination)
        return [NotificationRead.model_validate(r) for r in rows], total

    async def mark_read(self, notification_id: UUID, requester_id: UUID
                        ) -> NotificationRead:
        notification = await self.notifications.get_by_id(notification_id)
        if notification is None:
            raise NotFoundError("Notification not found.")
        if notification.user_id != requester_id:
            raise PermissionDeniedError(
                "This notification does not belong to you.")
        if notification.read_at is None:
            notification.read_at = datetime.now(UTC)
            await self._db.flush()
        return NotificationRead.model_validate(notification)

    async def mark_read_bulk(self, notification_ids: list[UUID],
                             requester_id: UUID) -> int:
        return await self.notifications.mark_read_bulk(
            notification_ids, requester_id)
