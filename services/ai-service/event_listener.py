import structlog
from shared.events import CrossServiceSubscriber
from shared.events_registry import EventName
from config import settings

logger = structlog.get_logger(__name__)


async def _handle_document_upload(payload: dict) -> None:
    document_id = payload.get("document_id") or payload.get("id")
    logger.info("document_uploaded_received", document_id=document_id)
    # Real implementation: run ai/rag pipeline,
    # then publish EventName.DOCUMENt_STATUS_CHANGED
    # TODO do it later


def build_subscriber() -> CrossServiceSubscriber:
    subscriber = CrossServiceSubscriber(settings)
    subscriber.on(EventName.DOCUMENT_STATUS_CHANGED, _handle_document_upload)
    return subscriber
