import structlog
from shared.events import CrossServiceSubscriber
from shared.events_registry import EventName
from shared.schemas.websocket import WebSocketEnvelope
from config import settings
from websocket_manager import manager

logger = structlog.get_logger(__name__)


async def _handle_analysis_completed(payload: dict) -> None:
    analysis_id = payload.get("analysis_id") or payload.get("id")
    await manager.broadcast(f"analysis: {analysis_id}", WebSocketEnvelope(
        event=EventName.ANALYSIS_COMPLETED, payload=payload))
    # TODO create a persisted notification via domains/notification


async def _handle_recommendation_ready(payload: dict) -> None:
    analysis_id = payload.get("analysis_id")
    await manager.broadcast(f"analysis: {analysis_id}", WebSocketEnvelope(
        event=EventName.RECOMMENDATION_READY, payload=payload))


async def _handle_document_status_changed(payload: dict) -> None:
    document_id = payload.get("document_id") or payload.get("id")
    await manager.broadcast(f"document: {document_id}", WebSocketEnvelope(
        event=EventName.DOCUMENT_STATUS_CHANGED, payload=payload))


def build_subscriber() -> CrossServiceSubscriber:
    subscriber = CrossServiceSubscriber(settings)
    subscriber.on(EventName.ANALYSIS_COMPLETED, _handle_analysis_completed)
    subscriber.on(EventName.RECOMMENDATION_READY, _handle_recommendation_ready)
    subscriber.on(EventName.DOCUMENT_STATUS_CHANGED,
                  _handle_document_status_changed)
    return subscriber
