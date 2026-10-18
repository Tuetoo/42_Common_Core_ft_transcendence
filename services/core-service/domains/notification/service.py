import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
import clients.auth_client as auth_client
import structlog
from config import settings
from database import SessionLocal
from fastapi import BackgroundTasks
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


_email_tasks: set[asyncio.Task[None]] = set()


async def _send_email(
        user_id: str, to: str, title: str, content: str | None) -> None:
    try:
        await send_notification_email(
            settings=settings, to=to, title=title, content=content)
    except Exception:
        logger.warning("notification_email_send_failed", user_id=user_id)


def send_email_if_enabled(
        user_id: str, preferences: DeliveryPreferences, *, title: str,
        content: str | None) -> None:
    if not (preferences.email and preferences.email_address):
        return
    task = asyncio.create_task(
        _send_email(user_id, preferences.email_address, title, content))
    _email_tasks.add(task)
    task.add_done_callback(_email_tasks.discard)


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
        send_email_if_enabled(
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


async def notify_user(
        *, user_id: Any, type: NotificationType, title: str,
        content: str) -> None:
    if not user_id:
        return
    try:
        uid = user_id if isinstance(user_id, UUID) else UUID(str(user_id))
    except ValueError:
        logger.warning(
            "notification.invalid_user_id", user_id=str(user_id),
            type=str(type))
        return
    preferences = await delivery_preferences(str(uid))
    if not preferences.in_app:
        send_email_if_enabled(
            str(uid), preferences, title=title, content=content)
        return
    try:
        async with SessionLocal() as session:
            notification = await NotificationService(
                session).create_notification(
                    user_id=uid, type=type, title=title, content=content)
            await session.commit()
    except Exception as exc:
        logger.warning(
            "notification.create_failed", user_id=str(user_id), type=str(type),
            error=str(exc))
        return
    await NotificationService.deliver_notification(notification, preferences)


async def notify_users(
        user_ids: Iterable[Any], *, type: NotificationType, title: str,
        content: str) -> None:
    seen: set[str] = set()
    for user_id in user_ids:
        key = str(user_id) if user_id else ""
        if not key or key in seen:
            continue
        seen.add(key)
        await notify_user(
            user_id=user_id, type=type, title=title, content=content)


def notify_after_commit(
        background_tasks: BackgroundTasks, user_ids: Iterable[Any], *,
        type: NotificationType, title: str, content: str) -> None:
    background_tasks.add_task(
        notify_users, list(user_ids), type=type, title=title, content=content)
