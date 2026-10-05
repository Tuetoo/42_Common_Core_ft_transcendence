from collections.abc import Iterable
from typing import Any
from uuid import UUID
import structlog
from database import SessionLocal
from fastapi import BackgroundTasks
from domains.notification.service import (NotificationService,
                                          delivery_preferences,
                                          send_email_if_enabled)
from shared.enums import NotificationType

logger = structlog.get_logger(__name__)


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
        await send_email_if_enabled(
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
