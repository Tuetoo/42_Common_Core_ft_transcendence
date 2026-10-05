import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
import structlog
from config import settings
from database import SessionLocal
from outbox_model import EventOutbox
from prometheus_client import Gauge
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from shared.events import publish_durable_event
from shared.metrics import get_or_create

logger = structlog.get_logger(__name__)

OUTBOX_DISPATCH_INTERVAL_SECONDS = 5.0
OUTBOX_GRACE_SECONDS = 10.0
OUTBOX_BATCH_SIZE = 100
OUTBOX_RETENTION_DAYS = 7
_PURGE_EVERY_SECONDS = 3600.0

OUTBOX_UNPUBLISHED = get_or_create(
    Gauge, "alphamind_outbox_unpublished_events",
    "Durable events written to core.event_outbox and not yet published to "
    "Redis (updated by the outbox dispatcher).")


@dataclass(frozen=True)
class OutboxEntry:
    id: UUID
    event_name: str
    payload: dict[str, Any]


async def enqueue(
        session: AsyncSession, event_name: str,
        payload: dict[str, Any]) -> OutboxEntry:
    row = EventOutbox(id=uuid4(), event_name=str(event_name), payload=payload)
    session.add(row)
    await session.flush()
    return OutboxEntry(row.id, row.event_name, payload)


async def _mark_published(ids: list[UUID]) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(EventOutbox).where(
                EventOutbox.id.in_(ids),
                EventOutbox.published_at.is_(None)).values(
                    published_at=func.now(),
                    attempts=EventOutbox.attempts + 1))
        await session.commit()


async def publish_now(entries: list[OutboxEntry]) -> None:
    published: list[UUID] = []
    for entry in entries:
        try:
            await publish_durable_event(
                settings, entry.event_name, entry.payload)
        except Exception as exc:
            logger.warning(
                "outbox_publish_deferred", event_name=entry.event_name,
                outbox_id=str(entry.id), error=str(exc))
            break
        published.append(entry.id)
    if published:
        try:
            await _mark_published(published)
        except Exception as exc:
            logger.warning("outbox_mark_published_failed", error=str(exc))


async def dispatch_pending(*, now: datetime | None = None) -> int:
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(seconds=OUTBOX_GRACE_SECONDS)
    published = 0
    async with SessionLocal() as session:
        rows = ((
            await session.execute(
                select(EventOutbox).where(
                    EventOutbox.published_at.is_(None), EventOutbox.created_at
                    < cutoff).order_by(EventOutbox.created_at).limit(
                        OUTBOX_BATCH_SIZE).with_for_update(skip_locked=True)
            )).scalars().all())
        for row in rows:
            row.attempts += 1
            try:
                await publish_durable_event(
                    settings, row.event_name, row.payload)
            except Exception as exc:
                row.last_error = str(exc)[:500]
                logger.warning(
                    "outbox_publish_failed", event_name=row.event_name,
                    outbox_id=str(row.id), attempts=row.attempts,
                    error=str(exc))
                break
            row.published_at = moment
            row.last_error = None
            published += 1
        await session.commit()
    if published:
        logger.info("outbox_published_pending", events=published)
    return published


async def purge_published(*, now: datetime | None = None) -> int:
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=OUTBOX_RETENTION_DAYS)
    async with SessionLocal() as session:
        result = await session.execute(
            delete(EventOutbox).where(EventOutbox.published_at < cutoff))
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def unpublished_count() -> int:
    async with SessionLocal() as session:
        return int((
            await session.execute(
                select(func.count()).select_from(EventOutbox).where(
                    EventOutbox.published_at.is_(None)))).scalar_one())


async def run_outbox_dispatcher() -> None:
    last_purge = 0.0
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(OUTBOX_DISPATCH_INTERVAL_SECONDS)
        try:
            await dispatch_pending()
            OUTBOX_UNPUBLISHED.set(await unpublished_count())
            if loop.time() - last_purge >= _PURGE_EVERY_SECONDS:
                removed = await purge_published()
                last_purge = loop.time()
                if removed:
                    logger.info("outbox_purged", rows=removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("outbox_dispatch_loop_failed")
