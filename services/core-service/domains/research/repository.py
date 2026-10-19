from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
import structlog
from deps import day_start
from sqlalchemy import Date, and_, cast, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from domains.research.model import (Document, NoteTag, ResearchComment,
                                    ResearchNote, ResearchTag)
from shared.database import count_query
from shared.enums import DocumentCategory, DocumentStatus
from shared.schemas import PaginationParams

STUCK_DOCUMENT_AFTER = timedelta(minutes=10)
STUCK_PROCESSING_AFTER = timedelta(minutes=60)
FINAL_DOCUMENT_STATUSES = frozenset(
    {DocumentStatus.READY, DocumentStatus.FAILED})


def stuck_after(status: str) -> timedelta:
    if status == DocumentStatus.UPLOADED:
        return STUCK_DOCUMENT_AFTER
    return STUCK_PROCESSING_AFTER


def _day_end_exclusive(day: date) -> datetime:
    return day_start(day + timedelta(days=1))


logger = structlog.get_logger(__name__)

_DOCUMENT_STATUS_RANK: dict[str, int] = {
    DocumentStatus.UPLOADED.value: 0,
    DocumentStatus.PARSING.value: 1,
    DocumentStatus.CHUNKING.value: 2,
    DocumentStatus.EMBEDDING.value: 3,
    DocumentStatus.READY.value: 4,
    DocumentStatus.FAILED.value: 4
}


def is_forward_status_transition(current: str, incoming: str) -> bool:
    if incoming not in _DOCUMENT_STATUS_RANK:
        return False
    if current == DocumentStatus.FAILED:
        return incoming == DocumentStatus.PARSING
    if current == DocumentStatus.READY:
        return False
    current_rank = _DOCUMENT_STATUS_RANK.get(current, 0)
    return _DOCUMENT_STATUS_RANK[incoming] > current_rank


class DocumentRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get_by_id(self, document_id: UUID) -> Document | None:
        result = await self._db.execute(
            select(Document).where(Document.id == document_id))
        return result.scalar_one_or_none()

    async def get_by_ticker_category_filename(
            self, *, owner_id: UUID, organization_id: UUID | None,
            ticker: str | None, category: str,
            filename: str) -> Document | None:
        stmt = select(Document).where(
            Document.ticker == ticker, Document.category == category,
            Document.filename == filename)
        if organization_id is not None:
            stmt = stmt.where(Document.organization_id == organization_id)
        else:
            stmt = stmt.where(
                Document.organization_id.is_(None),
                Document.owner_id == owner_id)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
            self, *, owner_id: UUID, organization_id: UUID | None,
            filename: str, document_type: str, category: str,
            ticker: str | None, storage_url: str,
            content_date: datetime | None = None, title: str | None = None,
            source_url: str | None = None) -> Document:
        doc = Document(
            owner_id=owner_id, organization_id=organization_id,
            filename=filename, document_type=document_type, category=category,
            ticker=ticker, storage_url=storage_url, status="uploaded",
            content_date=content_date, title=title, source_url=source_url)
        try:
            async with self._db.begin_nested():
                self._db.add(doc)
                await self._db.flush()
        except IntegrityError:
            existing = await self.get_by_ticker_category_filename(
                owner_id=owner_id, organization_id=organization_id,
                ticker=ticker, category=category, filename=filename)
            if existing is not None:
                return existing
            raise
        return doc

    async def delete(self, document: Document) -> None:
        await self._db.delete(document)
        await self._db.flush()

    async def list_filenames(self, *, ticker: str | None, category: str,
                             failed_retry_days: int) -> list[str]:
        ticker_condition = (
            Document.ticker.is_(None)
            if ticker is None else Document.ticker == ticker.upper())
        now = datetime.now(UTC)
        result = await self._db.execute(
            select(Document.filename).where(
                ticker_condition, Document.category == category,
                or_(
                    Document.status == DocumentStatus.READY,
                    and_(
                        Document.status == DocumentStatus.FAILED,
                        Document.uploaded_at
                        < now - timedelta(days=failed_retry_days)),
                    and_(
                        Document.status == DocumentStatus.UPLOADED,
                        Document.status_changed_at
                        >= now - STUCK_DOCUMENT_AFTER),
                    and_(
                        Document.status.not_in([
                            DocumentStatus.READY, DocumentStatus.UPLOADED,
                            DocumentStatus.FAILED
                        ]), Document.status_changed_at
                        >= now - STUCK_PROCESSING_AFTER))))
        return list(result.scalars().all())

    async def transition_status(self, document_id: UUID,
                                status: str) -> tuple[Document | None, bool]:
        result = await self._db.execute(
            select(Document).where(
                Document.id == document_id).with_for_update())
        document = result.scalar_one_or_none()
        if document is None:
            return None, False
        if not is_forward_status_transition(document.status, status):
            logger.info(
                "document_status_transition_ignored",
                document_id=str(document_id), current=document.status,
                incoming=status)
            return document, False
        document.status = status
        document.status_changed_at = datetime.now(UTC)
        await self._db.flush()
        return document, True

    async def list_for_user(
        self, user_id: UUID, allowed_org_ids: list[UUID], *,
        pagination: PaginationParams, ticker: str | None = None,
        category: DocumentCategory | None = None, status: str | None = None,
        created_from: date | None = None, created_to: date | None = None,
        content_from: date | None = None, content_to: date | None = None,
        sort: str = "-created_at", include_system_owner: UUID | None = None
    ) -> tuple[list[Document], int]:
        scope = [Document.owner_id == user_id]
        if allowed_org_ids:
            scope.append(Document.organization_id.in_(allowed_org_ids))
        if include_system_owner is not None:
            scope.append(Document.owner_id == include_system_owner)
        stmt = select(Document).where(or_(*scope))
        if ticker is not None:
            stmt = stmt.where(Document.ticker == ticker)
        if category is not None:
            stmt = stmt.where(Document.category == category)
        if status is not None:
            stmt = stmt.where(Document.status == status)
        if created_from is not None:
            stmt = stmt.where(Document.uploaded_at >= day_start(created_from))
        if created_to is not None:
            stmt = stmt.where(
                Document.uploaded_at < _day_end_exclusive(created_to))
        if content_from is not None:
            stmt = stmt.where(Document.content_date >= day_start(content_from))
        if content_to is not None:
            stmt = stmt.where(
                Document.content_date < _day_end_exclusive(content_to))
        total = await count_query(self._db, stmt)
        descending = sort.startswith("-")
        key = sort.lstrip("-")
        column: Any = {
            "created_at": Document.uploaded_at,
            "content_date": Document.content_date,
            "title": func.lower(
                func.coalesce(Document.title, Document.filename)),
            "status": Document.status
        }[key]
        order = (
            column.desc().nulls_last()
            if descending else column.asc().nulls_last())
        tie = Document.id.desc() if descending else Document.id.asc()
        result = await self._db.execute(
            stmt.order_by(order, tie).offset(pagination.offset).limit(
                pagination.limit))
        return list(result.scalars().all()), total


class ResearchNoteRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get_by_id(
            self, note_id: UUID, *,
            for_update: bool = False) -> ResearchNote | None:
        stmt = select(ResearchNote).where(ResearchNote.id == note_id)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
            self, *, author_id: UUID, organization_id: UUID | None, title: str,
            content: str | None) -> ResearchNote:
        note = ResearchNote(
            author_id=author_id, organization_id=organization_id, title=title,
            content=content)
        self._db.add(note)
        await self._db.flush()
        return note

    async def delete(self, note: ResearchNote) -> None:
        await self._db.delete(note)
        await self._db.flush()

    async def search(
            self, *, viewer_id: UUID, viewer_org_ids: list[UUID],
            q: str | None, author_id: UUID | None,
            organization_id: UUID | None, tag_id: UUID | None, sort: str,
            offset: int, limit: int,
            include_hidden: bool = False) -> tuple[list[ResearchNote], int]:
        scope = [ResearchNote.author_id == viewer_id]
        if viewer_org_ids:
            scope.append(ResearchNote.organization_id.in_(viewer_org_ids))
        stmt = (
            select(ResearchNote).where(or_(*scope)).options(
                defer(ResearchNote.yjs_state)))
        if not include_hidden:
            stmt = stmt.where(
                or_(
                    ResearchNote.is_hidden.is_(False),
                    ResearchNote.author_id == viewer_id))

        tsquery = func.plainto_tsquery("english", q) if q else None
        if tsquery is not None:
            stmt = stmt.where(ResearchNote.search_vector.op("@@")(tsquery))
        if author_id is not None:
            stmt = stmt.where(ResearchNote.author_id == author_id)
        if organization_id is not None:
            stmt = stmt.where(ResearchNote.organization_id == organization_id)
        if tag_id is not None:
            stmt = stmt.join(NoteTag,
                             NoteTag.note_id == ResearchNote.id).where(
                                 NoteTag.tag_id == tag_id)

        total = await count_query(self._db, stmt)

        if tsquery is not None:
            rank = func.ts_rank(ResearchNote.search_vector, tsquery)
            secondary = (
                ResearchNote.title
                if "title" in sort else ResearchNote.created_at)
            stmt = stmt.order_by(
                rank.desc(),
                secondary.desc() if sort.startswith("-") else secondary.asc(),
                ResearchNote.id.asc())
        else:
            sort_column = (
                ResearchNote.title
                if "title" in sort else ResearchNote.created_at)
            stmt = stmt.order_by((
                sort_column.desc()
                if sort.startswith("-") else sort_column.asc()),
                                 ResearchNote.id.asc())
        result = await self._db.execute(stmt.offset(offset).limit(limit))
        return list(result.scalars().all()), total

    @staticmethod
    def _normalize_tag_name(name: str) -> str:
        return name.strip().lower()

    async def get_or_create_tag(self, name: str) -> ResearchTag:
        name = self._normalize_tag_name(name)
        stmt = (
            pg_insert(ResearchTag).values(name=name).on_conflict_do_nothing(
                index_elements=["name"]))
        await self._db.execute(stmt)
        await self._db.flush()
        result = await self._db.execute(
            select(ResearchTag).where(ResearchTag.name == name))
        return result.scalar_one()

    async def get_tag_by_name(self, name: str) -> ResearchTag | None:
        result = await self._db.execute(
            select(ResearchTag).where(
                ResearchTag.name == self._normalize_tag_name(name)))
        return result.scalar_one_or_none()

    async def set_note_tags(self, note_id: UUID, tag_names: list[str]) -> None:
        await self._db.execute(
            delete(NoteTag).where(NoteTag.note_id == note_id))
        seen_tag_ids: set[UUID] = set()
        for name in tag_names:
            tag = await self.get_or_create_tag(name)
            if tag.id in seen_tag_ids:
                continue
            seen_tag_ids.add(tag.id)
            self._db.add(NoteTag(note_id=note_id, tag_id=tag.id))
        await self._db.flush()

    async def list_tags_for_notes(
            self, note_ids: list[UUID]) -> dict[UUID, list[str]]:
        tags: dict[UUID, list[str]] = {note_id: [] for note_id in note_ids}
        if not note_ids:
            return tags
        result = await self._db.execute(
            select(NoteTag.note_id, ResearchTag.name).join(
                ResearchTag, NoteTag.tag_id == ResearchTag.id).where(
                    NoteTag.note_id.in_(note_ids)).order_by(ResearchTag.name))
        for note_id, name in result.all():
            tags.setdefault(note_id, []).append(name)
        return tags

    async def list_tags_for_note(self, note_id: UUID) -> list[str]:
        result = await self._db.execute(
            select(ResearchTag.name).join(
                NoteTag, NoteTag.tag_id == ResearchTag.id).where(
                    NoteTag.note_id == note_id))
        return list(result.scalars().all())


class ResearchCommentRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def create(
            self, *, note_id: UUID, author_id: UUID,
            content: str) -> ResearchComment:
        comment = ResearchComment(
            note_id=note_id, author_id=author_id, content=content)
        self._db.add(comment)
        await self._db.flush()
        return comment

    async def get_by_id(self, comment_id: UUID) -> ResearchComment | None:
        result = await self._db.execute(
            select(ResearchComment).where(ResearchComment.id == comment_id))
        return result.scalar_one_or_none()

    async def delete(self, comment: ResearchComment) -> None:
        await self._db.delete(comment)
        await self._db.flush()

    @staticmethod
    def _visible(
            note_id: UUID, viewer_id: UUID | None,
            include_hidden: bool) -> Any:
        stmt = select(ResearchComment).where(
            ResearchComment.note_id == note_id)
        if not include_hidden:
            visible: list[Any] = [ResearchComment.is_hidden.is_(False)]
            if viewer_id is not None:
                visible.append(ResearchComment.author_id == viewer_id)
            stmt = stmt.where(or_(*visible))
        return stmt

    async def list_for_note(
            self, note_id: UUID, *, offset: int = 0, limit: int | None = None,
            viewer_id: UUID | None = None,
            include_hidden: bool = True) -> list[ResearchComment]:
        stmt = (
            self._visible(note_id, viewer_id, include_hidden).order_by(
                ResearchComment.created_at.asc(),
                ResearchComment.id.asc()).offset(offset))
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def count_for_note(
            self, note_id: UUID, *, viewer_id: UUID | None = None,
            include_hidden: bool = True) -> int:
        return await count_query(
            self._db, self._visible(note_id, viewer_id, include_hidden))


class SentimentRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    @staticmethod
    def _visible_notes(
            viewer_id: UUID, viewer_org_ids: list[UUID], include_hidden: bool,
            ticker: str | None) -> Any:
        scope = [ResearchNote.author_id == viewer_id]
        if viewer_org_ids:
            scope.append(ResearchNote.organization_id.in_(viewer_org_ids))
        conditions: list[Any] = [or_(*scope)]
        if not include_hidden:
            conditions.append(
                or_(
                    ResearchNote.is_hidden.is_(False),
                    ResearchNote.author_id == viewer_id))
        if ticker is not None:
            tagged = (
                select(NoteTag.note_id).join(
                    ResearchTag, NoteTag.tag_id == ResearchTag.id).where(
                        ResearchTag.name == ticker.lower()))
            conditions.append(
                or_(
                    ResearchNote.id.in_(tagged),
                    ResearchNote.search_vector.op("@@")(
                        func.plainto_tsquery("english", ticker))))
        return select(ResearchNote.id).where(and_(*conditions))

    async def daily_counts(
            self, *, viewer_id: UUID, viewer_org_ids: list[UUID],
            include_hidden: bool, ticker: str | None, date_from: date | None,
            date_to: date | None) -> list[tuple[str, date, str, int, float]]:
        notes = self._visible_notes(
            viewer_id, viewer_org_ids, include_hidden, ticker)
        rows: list[tuple[str, date, str, int, float]] = []
        models: tuple[tuple[str, Any], ...] = (("note", ResearchNote),
                                               ("comment", ResearchComment))
        for kind, model in models:
            day = cast(func.timezone("UTC", model.created_at), Date)
            score_sum = func.sum(model.sentiment_score)
            stmt = select(day, model.sentiment_label, func.count(),
                          score_sum).where(model.sentiment_label.is_not(None))
            if model is ResearchNote:
                stmt = stmt.where(ResearchNote.id.in_(notes))
            else:
                stmt = stmt.where(ResearchComment.note_id.in_(notes))
                if not include_hidden:
                    stmt = stmt.where(
                        or_(
                            ResearchComment.is_hidden.is_(False),
                            ResearchComment.author_id == viewer_id))
            if date_from is not None:
                stmt = stmt.where(model.created_at >= day_start(date_from))
            if date_to is not None:
                stmt = stmt.where(
                    model.created_at < _day_end_exclusive(date_to))
            stmt = stmt.group_by(day, model.sentiment_label)
            result = await self._db.execute(stmt)
            for row_day, label, count, total in result.all():
                rows.append((
                    kind, row_day, str(label), int(count), float(total
                                                                 or 0.0)))
        return rows
