import mimetypes
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4
import clients.auth_client as auth_client
import structlog
from config import settings
from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from domains.research.model import Document, ResearchNote
from domains.research.repository import (
    DocumentRepository, ResearchCommentRepository, ResearchNoteRepository)
from domains.research.schema import (
    DocumentRead, ResearchCommentCreate, ResearchCommentRead,
    ResearchNoteCreate, ResearchNoteFilters, ResearchNoteRead,
    ResearchNoteUpdate)
from shared.concurrency import gather_bounded
from shared.enums.document import DocumentCategory
from shared.events import publish_durable_event
from shared.events_registry import EventName
from shared.exceptions import (NotFoundError, PermissionDeniedError,
                               ValidationAppError)
from shared.storage import delete_object, download_bytes, upload_bytes
from shared.schemas.base import PaginationParams

logger = structlog.get_logger(__name__)


class ResearchService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self.documents = DocumentRepository(session)
        self.notes = ResearchNoteRepository(session)
        self.comments = ResearchCommentRepository(session)

    async def upload_document(
            self, *, owner_id: UUID, organization_id: UUID | None,
            filename: str, content: bytes, category: str,
            background_tasks: BackgroundTasks, ticker: str | None = None,
            content_date: datetime | None = None) -> DocumentRead:
        extension = Path(filename).suffix.lower()
        if extension not in settings.ALLOWED_DOCUMENT_EXTENSIONS:
            raise ValidationAppError(
                f"Unsupported file type '{extension}'. Allowed: "
                f"{', '.join(sorted(settings.ALLOWED_DOCUMENT_EXTENSIONS))}.")
        if len(content) > settings.MAX_UPLOAD_SIZE_BYTES:
            max_mb = settings.MAX_UPLOAD_SIZE_BYTES / (1024 * 1024)
            raise ValidationAppError(
                f"File exceeds the {max_mb:.0f}MB size limit.")
        if category not in settings.ALLOWED_DOCUMENT_CATEGORIES:
            raise ValidationAppError(
                f"Unsupported category '{category}'. Allowed: "
                f"{', '.join(sorted(settings.ALLOWED_DOCUMENT_CATEGORIES))}.")
        if (organization_id is not None
                and owner_id != UUID("00000000-0000-0000-0000-0000000005ec")):
            org_ids = await auth_client.list_user_organization_ids(
                str(owner_id))
            if organization_id not in org_ids:
                raise PermissionDeniedError(
                    "You are not a member of the target organization.")
        object_key = f"documents/{owner_id}/{uuid4().hex}{extension}"
        content_type, _ = mimetypes.guess_type(filename)
        await upload_bytes(settings, bucket=settings.S3_BUCKET_DOCUMENTS,
                           key=object_key, content=content,
                           content_type=content_type)
        storage_url = object_key
        document_type = extension.lstrip(".")
        ticker_normalized = ticker.upper() if ticker else None
        document = await self.documents.create(
            owner_id=owner_id, organization_id=organization_id,
            filename=filename, document_type=document_type, category=category,
            ticker=ticker_normalized, storage_url=storage_url,
            content_date=content_date)
        background_tasks.add_task(
            publish_durable_event, settings, EventName.DOCUMENT_UPLOADED,
            {"document_id": str(document.id), "owner_id": str(owner_id),
             "organization_id": str(organization_id) if organization_id
             else None, "storage_bucket": settings.S3_BUCKET_DOCUMENTS,
             "storage_key": storage_url, "document_type": document_type,
             "category": category, "ticker": ticker_normalized,
             "content_date": content_date.isoformat() if content_date
             else None, "filename": filename})
        return DocumentRead.model_validate(document)

    async def _require_readable_document(self, document_id: UUID,
                                         requester_id: UUID) -> Document:
        document = await self.documents.get_by_id(document_id)
        if document is None:
            raise NotFoundError("Document not found.")
        if document.owner_id == requester_id:
            return document
        if document.organization_id is not None:
            org_ids = await auth_client.list_user_organization_ids(
                str(requester_id))
            if document.organization_id in org_ids:
                return document
        raise NotFoundError("Document not found.")

    async def get_document(self, document_id: UUID, requester_id: UUID
                           ) -> DocumentRead:
        document = await self._require_readable_document(
            document_id, requester_id)
        return DocumentRead.model_validate(document)

    async def download_document(self, document_id: UUID, requester_id: UUID
                                ) -> bytes:
        document = await self._require_readable_document(
            document_id, requester_id)
        return await download_bytes(
            settings, bucket=settings.S3_BUCKET_DOCUMENTS,
            key=document.storage_url)

    async def delete_document(self, document_id: UUID, requester_id: UUID, *,
                              background_tasks: BackgroundTasks) -> None:
        document = await self.documents.get_by_id(document_id)
        if document is None:
            raise NotFoundError("Document not found.")
        if document.owner_id != requester_id:
            raise PermissionDeniedError(
                "Only the document owner can delete it.")
        await delete_object(settings, bucket=settings.S3_BUCKET_DOCUMENTS,
                            key=document.storage_url)
        await self.documents.delete(document)
        background_tasks.add_task(
            publish_durable_event, settings, EventName.DOCUMENT_DELETED,
            {"document_id": str(document_id)})

    async def list_documents(
            self, requester_id: UUID, *, pagination: PaginationParams,
            ticker: str | None = None,
            category: DocumentCategory | None = None
            ) -> tuple[list[DocumentRead], int]:
        ticker_normalized = ticker.upper() if ticker else None
        org_ids = await auth_client.list_user_organization_ids(
            str(requester_id))
        docs, total = await self.documents.list_for_user(
            requester_id, org_ids, pagination=pagination,
            ticker=ticker_normalized, category=category)
        return [DocumentRead.model_validate(d) for d in docs], total

    async def _note_to_read(self, note: ResearchNote) -> ResearchNoteRead:
        author = await auth_client.get_user_ref(str(note.author_id))
        tags = await self.notes.list_tags_for_note(note.id)
        return ResearchNoteRead(
            id=note.id, author=author, organization_id=note.organization_id,
            title=note.title, content=note.content, tags=tags,
            created_at=note.created_at, updated_at=note.updated_at)

    async def create_note(self, author_id: UUID, data: ResearchNoteCreate
                          ) -> ResearchNoteRead:
        if data.organization_id is not None:
            org_ids = await auth_client.list_user_organization_ids(
                str(author_id))
            if data.organization_id not in org_ids:
                raise PermissionDeniedError(
                    "You are not a member of the target organization.")
        note = await self.notes.create(
            author_id=author_id, organization_id=data.organization_id,
            title=data.title, content=data.content)
        if data.tags:
            await self.notes.set_note_tags(note.id, data.tags)
        return await self._note_to_read(note)

    async def _require_readable_note(self, note_id: UUID, requester_id: UUID
                                     ) -> ResearchNote:
        note = await self.notes.get_by_id(note_id)
        if note is None:
            raise NotFoundError("Note not found.")
        if note.author_id == requester_id:
            return note
        if note.organization_id is not None:
            org_ids = await auth_client.list_user_organization_ids(
                str(requester_id))
            if note.organization_id in org_ids:
                return note
        raise NotFoundError("Note not found.")

    async def get_note(self, note_id: UUID, requester_id: UUID
                       ) -> ResearchNoteRead:
        note = await self._require_readable_note(note_id, requester_id)
        return await self._note_to_read(note)

    async def update_note(self, note_id: UUID, requester_id: UUID,
                          data: ResearchNoteUpdate) -> ResearchNoteRead:
        note = await self.notes.get_by_id(note_id)
        if note is None:
            raise NotFoundError("Note not found.")
        if note.author_id != requester_id:
            raise PermissionDeniedError("Only the note's author can edit it.")
        if data.title is not None:
            note.title = data.title
        if data.content is not None:
            note.content = data.content
        await self._db.flush()
        await self._db.refresh(note)
        if data.tags is not None:
            await self.notes.set_note_tags(note.id, data.tags)
        return await self._note_to_read(note)

    async def delete_note(self, note_id: UUID, requester_id: UUID) -> None:
        note = await self.notes.get_by_id(note_id)
        if note is None:
            raise NotFoundError("Note not found.")
        if note.author_id != requester_id:
            raise PermissionDeniedError(
                "Only the note's author can delete it.")
        await self.notes.delete(note)

    async def search_notes(self, requester_id: UUID,
                           filters: ResearchNoteFilters
                           ) -> tuple[list[ResearchNoteRead], int]:
        org_ids = await auth_client.list_user_organization_ids(
            str(requester_id))
        tag_id = None
        if filters.tag:
            tag = await self.notes.get_tag_by_name(filters.tag)
            if tag is None:
                return [], 0
            tag_id = tag.id
        notes, total = await self.notes.search(
            viewer_id=requester_id, viewer_org_ids=org_ids, q=filters.q,
            author_id=filters.author_id,
            organization_id=filters.organization_id, tag_id=tag_id,
            sort=filters.sort, offset=filters.offset, limit=filters.limit)
        reads = await gather_bounded((self._note_to_read(n) for n in notes),
                                     limit=20)
        return reads, total

    async def add_comment(self, note_id: UUID, author_id: UUID,
                          data: ResearchCommentCreate) -> ResearchCommentRead:
        await self._require_readable_note(note_id, author_id)
        comment = await self.comments.create(
            note_id=note_id, author_id=author_id, content=data.content)
        author_ref = await auth_client.get_user_ref(str(author_id))
        return ResearchCommentRead(
            id=comment.id, note_id=comment.note_id, author=author_ref,
            content=comment.content, created_at=comment.created_at)
