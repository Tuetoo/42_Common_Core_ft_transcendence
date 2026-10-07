from typing import Any
from uuid import UUID
import structlog
from config import settings
from shared.events import DurableEventSubscriber
from shared.events_registry import EventName

logger = structlog.get_logger(__name__)

_UPLOADED_REQUIRED_FIELDS = (
    "document_id", "storage_bucket", "storage_key", "document_type",
    "category")


async def _handle_document_uploaded(payload: dict[str, Any]) -> None:
    missing = [
        field for field in _UPLOADED_REQUIRED_FIELDS
        if not isinstance(payload.get(field), str) or not payload[field]
    ]
    if missing:
        logger.warning(
            "document_uploaded_malformed", missing_fields=missing,
            payload=payload)
        return
    document_id: str = payload["document_id"]
    logger.info("document_uploaded_received", document_id=document_id)
    from ai.workflows.rag_ingestion_workflow import run_rag_ingestion_workflow

    await run_rag_ingestion_workflow(
        document_id=document_id, storage_bucket=payload["storage_bucket"],
        storage_key=payload["storage_key"],
        document_type=payload["document_type"], category=payload["category"],
        ticker=payload.get("ticker"), content_date=payload.get("content_date"),
        filename=payload.get("filename"), owner_id=payload.get("owner_id"),
        organization_id=payload.get("organization_id"))


async def _handle_document_deleted(payload: dict[str, Any]) -> None:
    document_id = payload.get("document_id")
    if not isinstance(document_id, str):
        logger.warning("document_deleted_missing_document_id", payload=payload)
        return
    logger.info("document_deleted_received", document_id=document_id)

    from ai.rag.indexing import delete_document_completely
    from database import SessionLocal

    async with SessionLocal() as session:
        await delete_document_completely(document_id, session)
        await session.commit()


async def _handle_portfolio_deleted(payload: dict[str, Any]) -> None:
    raw_portfolio_id = payload.get("portfolio_id")
    try:
        portfolio_id = UUID(str(raw_portfolio_id))
    except ValueError:
        logger.warning("portfolio_deleted_bad_portfolio_id", payload=payload)
        return
    from ai_events import (TIER_CANCEL_REASON_PORTFOLIO_DELETED,
                           publish_events, tier_cancelled_events)
    from database import SessionLocal
    from domains.analysis.repository import RecommendationTierRepository

    async with SessionLocal() as session:
        closed = await RecommendationTierRepository(
            session).cancel_unfilled_tiers_for_portfolio(
                portfolio_id, note=(
                    "Tier cancelled: the portfolio was deleted "
                    "(portfolio_deleted)."))
        await session.commit()
    logger.info(
        "portfolio_deleted_tiers_cancelled", portfolio_id=str(portfolio_id),
        count=len(closed))
    await publish_events(
        tier_cancelled_events(
            closed, reason=TIER_CANCEL_REASON_PORTFOLIO_DELETED,
            user_id_fallback=payload.get("user_id") or None))


def build_durable_subscriber() -> DurableEventSubscriber:
    subscriber = DurableEventSubscriber(
        settings, consumer_name=settings.SERVICE_NAME)
    subscriber.on(EventName.DOCUMENT_UPLOADED, _handle_document_uploaded)
    subscriber.on(EventName.DOCUMENT_DELETED, _handle_document_deleted)
    subscriber.on(EventName.PORTFOLIO_DELETED, _handle_portfolio_deleted)
    return subscriber
