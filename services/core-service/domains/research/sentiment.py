import asyncio
from datetime import UTC, datetime
from uuid import UUID
import clients.ai_client as ai_client
import structlog
from database import SessionLocal
from sqlalchemy import select, update
from domains.research.model import ResearchComment, ResearchNote

logger = structlog.get_logger(__name__)

_background_tasks: set[asyncio.Task[bool]] = set()


def note_sentiment_text(title: str, content: str | None) -> str:
    return f"{title}\n\n{content or ''}".strip()


async def refresh_note_sentiment(note_id: UUID) -> bool:
    try:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    select(ResearchNote.title, ResearchNote.content).where(
                        ResearchNote.id == note_id))).one_or_none()
        if row is None:
            return False
        text = note_sentiment_text(row.title, row.content)
        result = await ai_client.analyze_sentiment(text, "note")
        if result is None:
            return False
        async with SessionLocal() as session:
            current = (
                await session.execute(
                    select(ResearchNote.title, ResearchNote.content).where(
                        ResearchNote.id == note_id).with_for_update()
                )).one_or_none()
            if (current is None or note_sentiment_text(
                    current.title, current.content) != text):
                return False
            await session.execute(
                update(ResearchNote).where(ResearchNote.id == note_id).values(
                    sentiment_label=result.label, sentiment_score=result.score,
                    sentiment_model=result.model,
                    sentiment_analyzed_at=datetime.now(UTC),
                    updated_at=ResearchNote.updated_at))
            await session.commit()
        return True
    except Exception:
        logger.warning(
            "note_sentiment_refresh_failed", note_id=str(note_id),
            exc_info=True)
        return False


async def refresh_comment_sentiment(comment_id: UUID) -> bool:
    try:
        async with SessionLocal() as session:
            text = (
                await session.execute(
                    select(ResearchComment.content).where(
                        ResearchComment.id == comment_id)
                )).scalar_one_or_none()
        if text is None:
            return False
        result = await ai_client.analyze_sentiment(text, "comment")
        if result is None:
            return False
        async with SessionLocal() as session:
            current = (
                await session.execute(
                    select(ResearchComment.content).where(
                        ResearchComment.id == comment_id).with_for_update()
                )).scalar_one_or_none()
            if current != text:
                return False
            await session.execute(
                update(ResearchComment).where(
                    ResearchComment.id == comment_id).values(
                        sentiment_label=result.label,
                        sentiment_score=result.score,
                        sentiment_model=result.model,
                        sentiment_analyzed_at=datetime.now(UTC)))
            await session.commit()
        return True
    except Exception:
        logger.warning(
            "comment_sentiment_refresh_failed", comment_id=str(comment_id),
            exc_info=True)
        return False


def schedule_note_sentiment(note_id: UUID) -> None:
    task = asyncio.create_task(refresh_note_sentiment(note_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
