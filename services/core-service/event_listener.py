from uuid import UUID
from typing import Any
import structlog
from database import SessionLocal
from domains.notification.service import NotificationService
from domains.research.repository import DocumentRepository
from shared.enums.notification import NotificationType
from shared.events import CrossServiceSubscriber, DurableEventSubscriber
from shared.events_registry import EventName
from shared.schemas.websocket import WebSocketEnvelope
from config import settings
from websocket_manager import manager

logger = structlog.get_logger(__name__)


async def _handle_analysis_completed(payload: dict[str, Any]) -> None:
    analysis_id = payload.get("analysis_id") or payload.get("id")
    await manager.broadcast(f"analysis: {analysis_id}", WebSocketEnvelope(
        event=EventName.ANALYSIS_COMPLETED, payload=payload))
    user_id = payload.get("user_id")
    if user_id is None:
        logger.warning("analysis_completed_missing_user_id",
                       analysis_id=analysis_id)
        return
    async with SessionLocal() as session:
        await NotificationService(session).create_notification(
            user_id=user_id, type=NotificationType.ANALYSIS_COMPLETED,
            title="Your analysis is ready",
            content=f"Analysis {analysis_id} has completed.")
        await session.commit()


async def _handle_recommendation_ready(payload: dict[str, Any]) -> None:
    analysis_id = payload.get("analysis_id")
    await manager.broadcast(f"analysis:{analysis_id}", WebSocketEnvelope(
        event=EventName.RECOMMENDATION_READY, payload=payload))
    user_id = payload.get("user_id")
    if user_id is None:
        logger.warning("recommendation_ready_missing_user_id",
                       analysis_id=analysis_id)
        return
    async with SessionLocal() as session:
        await NotificationService(session).create_notification(
            user_id=user_id, type=NotificationType.RECOMMENDATION_READY,
            title="A new recommendation is ready",
            content=f"Analysis {analysis_id} produced a recommendation.")
        await session.commit()


async def _persist_document_status(payload: dict[str, Any]) -> None:
    document_id = payload.get("document_id") or payload.get("id")
    if document_id is None:
        logger.warning("document_status_changed_missing_document_id",
                       payload=payload)
        return
    try:
        document_uuid = UUID(document_id)
    except ValueError:
        logger.warning("document_status_changed_invalid_document_id",
                       document_id=document_id)
        return
    status = payload.get("status")
    if not isinstance(status, str):
        logger.warning("document_status_changed_missing_status",
                       document_id=document_id)
        return
    async with SessionLocal() as session:
        updated = await DocumentRepository(session).update_status(
            document_id=document_uuid, status=status)
        if updated is None:
            logger.warning("document_status_changed_unknown_document",
                           document_id=document_id)
        await session.commit()


async def _broadcast_document_status_changed(payload: dict[str, Any]) -> None:
    document_id = payload.get("document_id") or payload.get("id")
    await manager.broadcast(f"document:{document_id}", WebSocketEnvelope(
        event=EventName.DOCUMENT_STATUS_CHANGED, payload=payload))


async def _handle_organization_member_joined(payload: dict[str, Any]) -> None:
    user_id = payload.get("user_id")
    organization_id = payload.get("organization_id")
    if user_id is None:
        logger.warning("organization_member_joined_missing_user_id")
        return
    async with SessionLocal() as session:
        await NotificationService(session).create_notification(
            user_id=user_id, type=NotificationType.ORGANIZATION_INVITE,
            title="Welcome to the organization",
            content=f"You joined organization {organization_id}.")
        await session.commit()


async def _handle_user_presence_changed(payload: dict[str, Any]) -> None:
    user_id = payload.get("user_id")
    if user_id is None:
        logger.warning("user_presence_changed_missing_user_id")
        return
    await manager.broadcast(f"presence:{user_id}", WebSocketEnvelope(
        event=EventName.USER_PRESENCE_CHANGED, payload=payload))


def build_subscriber() -> CrossServiceSubscriber:
    subscriber = CrossServiceSubscriber(settings)
    subscriber.on(EventName.ANALYSIS_COMPLETED, _handle_analysis_completed)
    subscriber.on(EventName.RECOMMENDATION_READY, _handle_recommendation_ready)
    subscriber.on(EventName.DOCUMENT_STATUS_CHANGED,
                  _broadcast_document_status_changed)
    subscriber.on(EventName.ORGANIZATION_MEMBER_JOINED,
                  _handle_organization_member_joined)
    subscriber.on(EventName.USER_PRESENCE_CHANGED,
                  _handle_user_presence_changed)
    return subscriber


def build_durable_subscriber() -> DurableEventSubscriber:
    subscriber = DurableEventSubscriber(
        settings, consumer_name=settings.SERVICE_NAME)
    subscriber.on(EventName.DOCUMENT_STATUS_CHANGED, _persist_document_status)
    return subscriber
