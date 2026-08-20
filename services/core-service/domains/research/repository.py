from datetime import datetime
from uuid import UUID
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlalchemy.exc import IntegrityError
from domains.research.model import (Document, NoteTag, ResearchComment,
                                    ResearchNote, ResearchTag)
from shared.database import count_query
from shared.enums.document import DocumentCategory
from shared.schemas.base import PaginationParams


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get_by_id(self, document_id: UUID) -> Document | None:
        result = await self._db.execute(select(Document)
                                        .where(Document.id == document_id))
        return result.scalar_one_or_none()

    async def get_by_ticker_category_filename(
            self, *, ticker: str | None, category: str, filename: str
            ) -> Document | None:
        stmt = select(Document).where(
            Document.ticker == ticker, Document.category == category,
            Document.filename == filename)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, *, owner_id: UUID, organization_id: UUID | None,
                     filename: str, document_type: str, category: str,
                     ticker: str | None, storage_url: str,
                     content_date: datetime | None = None) -> Document:
        doc = Document(
            owner_id=owner_id, organization_id=organization_id,
            filename=filename, document_type=document_type, category=category,
            ticker=ticker, storage_url=storage_url, status="uploaded",
            content_date=content_date)
        self._db.add(doc)
        try:
            await self._db.flush()
        except IntegrityError:
            await self._db.rollback()
            existing = await self.get_by_ticker_category_filename(
                ticker=ticker, category=category, filename=filename)
            if existing is not None:
                return existing
            raise
        return doc

    async def delete(self, document: Document) -> None:
        await self._db.delete(document)
        await self._db.flush()

    async def list_filenames(self, *, ticker: str | None, category: str
                             ) -> list[str]:
        ticker_condition = (Document.ticker.is_(None) if ticker is None
                            else Document.ticker == ticker.upper())
        result = await self._db.execute(select(Document.filename).where(
            ticker_condition, Document.category == category,
            Document.status != "failed"))
        return list(result.scalars().all())

    async def update_status(self, document_id: UUID, status: str
                            ) -> Document | None:
        document = await self.get_by_id(document_id)
        if document is None:
            return None
        document.status = status
        await self._db.flush()
        return document

    async def list_for_user(
            self, user_id: UUID, allowed_org_ids: list[UUID], *,
            pagination: PaginationParams, ticker: str | None = None,
            category: DocumentCategory | None = None
            ) -> tuple[list[Document], int]:
        scope = [Document.owner_id == user_id]
        if allowed_org_ids:
            scope.append(Document.organization_id.in_(allowed_org_ids))
        stmt = select(Document).where(or_(*scope))
        if ticker is not None:
            stmt = stmt.where(Document.ticker == ticker)
        if category is not None:
            stmt = stmt.where(Document.category == category)
        total = await count_query(self._db, stmt)
        result = await self._db.execute(stmt.order_by(
            Document.uploaded_at.desc()).offset(pagination.offset)
            .limit(pagination.limit))
        return list(result.scalars().all()), total


class ResearchNoteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get_by_id(self, note_id: UUID) -> ResearchNote | None:
        result = await self._db.execute(select(ResearchNote)
                                        .where(ResearchNote.id == note_id))
        return result.scalar_one_or_none()

    async def create(self, *, author_id: UUID, organization_id: UUID | None,
                     title: str, content: str | None) -> ResearchNote:
        note = ResearchNote(
            author_id=author_id, organization_id=organization_id, title=title,
            content=content)
        self._db.add(note)
        await self._db.flush()
        return note

    async def delete(self, note: ResearchNote) -> None:
        await self._db.delete(note)
        await self._db.flush()

    async def search(self, *, viewer_id: UUID, viewer_org_ids: list[UUID],
                     q: str | None, author_id: UUID | None,
                     organization_id: UUID | None, tag_id: UUID | None,
                     sort: str, offset: int, limit: int
                     ) -> tuple[list[ResearchNote], int]:
        scope = [ResearchNote.author_id == viewer_id]
        if viewer_org_ids:
            scope.append(ResearchNote.organization_id.in_(viewer_org_ids))
        stmt = (select(ResearchNote).where(or_(*scope))
                .options(defer(ResearchNote.yjs_state)))
        tsquery = func.plainto_tsquery("english", q) if q else None
        if tsquery is not None:
            stmt = stmt.where(ResearchNote.search_vector.op("@@")(tsquery))
        if author_id is not None:
            stmt = stmt.where(ResearchNote.author_id == author_id)
        if organization_id is not None:
            stmt = stmt.where(ResearchNote.organization_id == organization_id)
        if tag_id is not None:
            stmt = stmt.join(NoteTag, NoteTag.note_id == ResearchNote.id
                             ).where(NoteTag.tag_id == tag_id)
        total = await count_query(self._db, stmt)
        if tsquery is not None:
            rank = func.ts_rank(ResearchNote.search_vector, tsquery)
            secondary = (ResearchNote.title if "title" in sort
                         else ResearchNote.created_at)
            stmt = stmt.order_by(rank.desc(), secondary.desc()
                                 if sort.startswith("-") else secondary.asc())
        else:
            sort_column = (ResearchNote.title if "title" in sort
                           else ResearchNote.created_at)
            stmt = stmt.order_by(sort_column.desc() if sort.startswith("-")
                                 else sort_column.asc())
        result = await self._db.execute(stmt.offset(offset).limit(limit))
        return list(result.scalars().all()), total

    async def get_or_create_tag(self, name: str) -> ResearchTag:
        stmt = pg_insert(ResearchTag).values(name=name).on_conflict_do_nothing(
            index_elements=["name"])
        await self._db.execute(stmt)
        await self._db.flush()
        result = await self._db.execute(select(ResearchTag)
                                        .where(ResearchTag.name == name))
        return result.scalar_one()

    async def get_tag_by_name(self, name: str) -> ResearchTag | None:
        result = await self._db.execute(select(ResearchTag)
                                        .where(ResearchTag.name == name))
        return result.scalar_one_or_none()

    async def set_note_tags(self, note_id: UUID, tag_names: list[str]) -> None:
        await self._db.execute(delete(NoteTag)
                               .where(NoteTag.note_id == note_id))
        for name in tag_names:
            tag = await self.get_or_create_tag(name)
            self._db.add(NoteTag(note_id=note_id, tag_id=tag.id))
        await self._db.flush()

    async def list_tags_for_note(self, note_id: UUID) -> list[str]:
        result = await self._db.execute(select(ResearchTag.name).join(
            NoteTag, NoteTag.tag_id == ResearchTag.id)
            .where(NoteTag.note_id == note_id))
        return list(result.scalars().all())


class ResearchCommentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def create(self, *, note_id: UUID, author_id: UUID, content: str
                     ) -> ResearchComment:
        comment = ResearchComment(note_id=note_id, author_id=author_id,
                                  content=content)
        self._db.add(comment)
        await self._db.flush()
        return comment

    async def list_for_note(self, note_id: UUID) -> list[ResearchComment]:
        result = await self._db.execute(select(ResearchComment).where(
            ResearchComment.note_id == note_id)
            .order_by(ResearchComment.created_at.asc()))
        return list(result.scalars().all())
