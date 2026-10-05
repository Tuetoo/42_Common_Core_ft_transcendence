from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID
import clients.auth_client as auth_client
import structlog
from config import settings
from sqlalchemy.ext.asyncio import AsyncSession
from websocket_manager import WebSocketEnvelope, manager
from domains.notification.repository import NotificationRepository
from domains.notification.schema import NotificationRead
from shared.email import send_notification_email
from shared.enums import NotificationChannel, NotificationType
from shared.exceptions import NotFoundError
from shared.schemas import PaginationParams

logger = structlog.get_logger(__name__)


@dataclass
class RetentionResult:
    deleted: int = 0
    batches: int = 0
    failed_batches: int = 0


@dataclass(frozen=True)
class DeliveryPreferences:
    in_app: bool = True
    websocket: bool = True
    email: bool = False
    email_address: str | None = None


async def delivery_preferences(user_id: str) -> DeliveryPreferences:
    try:
        info = await auth_client.get_notification_settings(user_id)
    except Exception:
        logger.warning("notification_settings_lookup_failed", user_id=user_id)
        return DeliveryPreferences()
    channels = info.get("notification_preferences") or {}
    return DeliveryPreferences(
        in_app=bool(channels.get(NotificationChannel.IN_APP.value, True)),
        websocket=bool(
            channels.get(NotificationChannel.WEBSOCKET.value, True)),
        email=bool(channels.get(NotificationChannel.EMAIL.value, False)),
        email_address=info.get("email") or None)


async def send_email_if_enabled(
        user_id: str, preferences: DeliveryPreferences, *, title: str,
        content: str | None) -> None:
    if not (preferences.email and preferences.email_address):
        return
    try:
        await send_notification_email(
            settings=settings, to=preferences.email_address, title=title,
            content=content)
    except Exception:
        logger.warning("notification_email_send_failed", user_id=user_id)


class NotificationService:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self.notifications = NotificationRepository(session)

    async def create_notification(
            self, *, user_id: UUID, type: NotificationType, title: str,
            content: str | None = None) -> NotificationRead:
        notification = await self.notifications.create(
            user_id=user_id, type=type, title=title, content=content)
        return NotificationRead.model_validate(notification)

    @staticmethod
    async def deliver_notification(
            notification: NotificationRead,
            preferences: DeliveryPreferences | None = None) -> None:
        user_id = str(notification.user_id)
        if preferences is None:
            preferences = await delivery_preferences(user_id)
        if preferences.websocket:
            try:
                await manager.broadcast(
                    f"user:{user_id}",
                    WebSocketEnvelope(
                        event="notification",
                        payload=notification.model_dump(mode="json")),
                    cross_replica=True)
            except Exception:
                logger.warning(
                    "notification_websocket_broadcast_failed", user_id=user_id)
        await send_email_if_enabled(
            user_id, preferences, title=notification.title,
            content=notification.content)

    async def list_for_user(
            self, user_id: UUID, *, unread_only: bool,
            pagination: PaginationParams
    ) -> tuple[list[NotificationRead], int]:
        rows, total = await self.notifications.list_for_user(
            user_id, unread_only=unread_only, pagination=pagination)
        return [NotificationRead.model_validate(r) for r in rows], total

    async def mark_read(
            self, notification_id: UUID,
            requester_id: UUID) -> NotificationRead:
        notification = await self.notifications.get_by_id(notification_id)
        if notification is None or notification.user_id != requester_id:
            raise NotFoundError("Notification not found.")
        if notification.read_at is None:
            notification.read_at = datetime.now(UTC)
            await self._db.flush()
        return NotificationRead.model_validate(notification)

    async def mark_read_bulk(
            self, notification_ids: list[UUID], requester_id: UUID) -> int:
        return await self.notifications.mark_read_bulk(
            notification_ids, requester_id)

    async def purge_old_read_notifications(self) -> RetentionResult:
        days = settings.NOTIFICATION_READ_RETENTION_DAYS
        cutoff = datetime.now(UTC) - timedelta(days=days)
        batch_size = settings.NOTIFICATION_PURGE_BATCH_SIZE
        result = RetentionResult()
        while True:
            result.batches += 1
            try:
                deleted = await self.notifications.delete_read_before(
                    cutoff, batch_size)
                await self._db.commit()
            except Exception as exc:
                await self._db.rollback()
                result.failed_batches += 1
                logger.error("notification_retention_failed", error=str(exc))
                break
            result.deleted += deleted
            if deleted < batch_size:
                break
        logger.info(
            "notification_retention_done", deleted=result.deleted,
            batches=result.batches, failed_batches=result.failed_batches,
            retention_days=days)
        return result
