import mimetypes
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import NamedTuple
from uuid import UUID, uuid4
import clients.auth_client as auth_client
import structlog
import yjs_relay
from config import settings
from deps import check_date_range
from fastapi import BackgroundTasks
from outbox import OutboxEntry, enqueue, publish_now
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from domains.research.model import Document, ResearchComment, ResearchNote
from domains.research.repository import (FINAL_DOCUMENT_STATUSES,
                                         DocumentRepository,
                                         ResearchCommentRepository,
                                         ResearchNoteRepository,
                                         SentimentRepository, stuck_after)
from domains.research.schema import (DocumentRead, ResearchCommentCreate,
                                     ResearchCommentRead,
                                     ResearchCommentUpdate, ResearchNoteCreate,
                                     ResearchNoteFilters, ResearchNoteRead,
                                     ResearchNoteUpdate, SentimentCounts,
                                     SentimentDay, SentimentSummary)
from shared.enums import DocumentCategory
from shared.events import EventName
from shared.exceptions import (ConflictError, NotFoundError,
                               PermissionDeniedError, ValidationAppError)
from shared.http_client import gather_bounded
from shared.permissions import Permission, has_permission
from shared.schemas import (PaginationParams, SYSTEM_DOCUMENT_OWNER_ID,
                            UserRef, normalize_ticker)
from shared.security import CurrentUser
from shared.storage import delete_object, download_bytes, upload_bytes

logger = structlog.get_logger(__name__)


class NoteAccess(NamedTuple):
    can_read: bool
    can_write: bool


@dataclass(frozen=True)
class UploadOutcome:
    document: DocumentRead
    duplicate: bool = False
    retried: bool = False
    republished: bool = False


@dataclass(frozen=True)
class NoteChange:
    note: ResearchNoteRead
    author_id: UUID
    text_changed: bool = True
    state_changed: bool = True


@dataclass(frozen=True)
class CommentChange:
    comment: ResearchCommentRead
    comment_author_id: UUID
    note_author_id: UUID
    note_title: str
    text_changed: bool = True
    state_changed: bool = True


DOCUMENT_KEY_ROOT = "documents/"


def document_key_prefix(owner_id: UUID) -> str:
    return f"{DOCUMENT_KEY_ROOT}{owner_id}/"


def can_moderate(user: CurrentUser) -> bool:
    return has_permission(user.role, Permission.CONTENT_MODERATE)


def _validate_file_content(extension: str, content: bytes) -> None:
    if not content:
        raise ValidationAppError("The file is empty.")
    if extension == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise ValidationAppError("The file is not a valid PDF.")
    elif extension in (".txt", ".md"):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationAppError(
                "Text files must be UTF-8 encoded.") from exc
        if "\x00" in text:
            raise ValidationAppError("Text files must not contain NUL bytes.")


async def delete_document_row(session: AsyncSession,
                              document: Document) -> tuple[str, OutboxEntry]:
    document_id = document.id
    storage_key = document.storage_url
    await DocumentRepository(session).delete(document)
    entry = await enqueue(
        session, EventName.DOCUMENT_DELETED, {"document_id": str(document_id)})
    return storage_key, entry


def _upload_ticker(ticker: str | None) -> str | None:
    if ticker is None or not ticker.strip():
        return None
    try:
        return normalize_ticker(ticker)
    except ValueError as exc:
        raise ValidationAppError(str(exc)) from exc


def _body_state(note_id: UUID, state: bytes | None, text: str) -> bytes:
    if state:
        try:
            return yjs_relay.replace_text(bytes(state), text)
        except yjs_relay.YjsDecodeError as exc:
            logger.warning(
                "note_yjs_state_reseeded", note_id=str(note_id),
                error=str(exc))
    return yjs_relay.seed_state(text)


def _looks_stuck(document: Document) -> bool:
    changed_at = document.status_changed_at
    if changed_at.tzinfo is None:
        changed_at = changed_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - changed_at > stuck_after(document.status)


class ResearchService:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self.documents = DocumentRepository(session)
        self.notes = ResearchNoteRepository(session)
        self.comments = ResearchCommentRepository(session)

    async def upload(
            self, *, owner_id: UUID, organization_id: UUID | None,
            filename: str, content: bytes, category: str,
            background_tasks: BackgroundTasks, ticker: str | None = None,
            content_date: datetime | None = None, title: str | None = None,
            source_url: str | None = None) -> UploadOutcome:
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
        _validate_file_content(extension, content)
        if (organization_id is not None
                and owner_id != SYSTEM_DOCUMENT_OWNER_ID):
            await auth_client.ensure_organization_writer(
                owner_id, organization_id)

        document_type = extension.lstrip(".")
        ticker_normalized = _upload_ticker(ticker)
        content_type, _ = mimetypes.guess_type(filename)

        existing = await self.documents.get_by_ticker_category_filename(
            owner_id=owner_id, organization_id=organization_id,
            ticker=ticker_normalized, category=category, filename=filename)
        if existing is not None:
            stuck = existing.status not in FINAL_DOCUMENT_STATUSES and (
                _looks_stuck(existing))
            if stuck:
                # Lock and re-read: a status update from ai-service or a
                # parallel republish may have moved the row on meanwhile.
                await self._db.refresh(existing, with_for_update=True)
                stuck = existing.status not in FINAL_DOCUMENT_STATUSES and (
                    _looks_stuck(existing))
            if stuck:
                logger.warning(
                    "document_upload_republished",
                    document_id=str(existing.id), status=existing.status)
                existing.status = "uploaded"
                existing.status_changed_at = datetime.now(UTC)
                await self._db.flush()
                await self._publish_uploaded(background_tasks, existing)
            if existing.status != "failed":
                return UploadOutcome(
                    DocumentRead.model_validate(existing), duplicate=True,
                    republished=stuck)
            if existing.owner_id != owner_id:
                raise ConflictError(
                    "A document with this name already exists in the "
                    "organization.")
            await upload_bytes(
                settings, bucket=settings.S3_BUCKET_DOCUMENTS,
                key=existing.storage_url, content=content,
                content_type=content_type)
            existing.status = "uploaded"
            existing.status_changed_at = datetime.now(UTC)
            existing.content_date = content_date or existing.content_date
            existing.title = title or existing.title
            existing.source_url = source_url or existing.source_url
            await self._db.flush()
            await self._publish_uploaded(background_tasks, existing)
            return UploadOutcome(
                DocumentRead.model_validate(existing), retried=True)

        object_key = (
            f"{document_key_prefix(owner_id)}{uuid4().hex}{extension}")
        await upload_bytes(
            settings, bucket=settings.S3_BUCKET_DOCUMENTS, key=object_key,
            content=content, content_type=content_type)

        try:
            document = await self.documents.create(
                owner_id=owner_id, organization_id=organization_id,
                filename=filename, document_type=document_type,
                category=category, ticker=ticker_normalized,
                storage_url=object_key, content_date=content_date, title=title,
                source_url=source_url)
        except BaseException:
            await self._delete_object_quietly(object_key)
            raise
        if document.storage_url != object_key:
            await self._delete_object_quietly(object_key)
            return UploadOutcome(
                DocumentRead.model_validate(document), duplicate=True)

        await self._publish_uploaded(background_tasks, document)
        return UploadOutcome(DocumentRead.model_validate(document))

    async def _publish_uploaded(
            self, background_tasks: BackgroundTasks,
            document: Document) -> None:
        entry = await enqueue(
            self._db, EventName.DOCUMENT_UPLOADED, {
                "document_id": str(document.id),
                "owner_id": str(document.owner_id),
                "organization_id": (
                    str(document.organization_id)
                    if document.organization_id else None),
                "storage_bucket": settings.S3_BUCKET_DOCUMENTS,
                "storage_key": document.storage_url,
                "document_type": document.document_type,
                "category": document.category,
                "ticker": document.ticker,
                "content_date": (
                    document.content_date.isoformat()
                    if document.content_date else None),
                "filename": document.filename
            })
        background_tasks.add_task(publish_now, [entry])

    async def _delete_object_quietly(self, key: str) -> None:
        try:
            await delete_object(
                settings, bucket=settings.S3_BUCKET_DOCUMENTS, key=key)
        except Exception:
            logger.warning("document_orphan_cleanup_failed", storage_key=key)

    async def get_document(
            self, document_id: UUID, requester_id: UUID) -> DocumentRead:
        document = await self._require_readable_document(
            document_id, requester_id)
        return DocumentRead.model_validate(document)

    async def download_document(
            self, document_id: UUID,
            requester_id: UUID) -> tuple[DocumentRead, bytes]:
        document = await self.get_document(document_id, requester_id)
        return document, await self.document_content(document)

    @staticmethod
    async def document_content(document: DocumentRead) -> bytes:
        return await download_bytes(
            settings, bucket=settings.S3_BUCKET_DOCUMENTS,
            key=document.storage_url)

    async def delete_document(
            self, document_id: UUID, requester_id: UUID, *,
            background_tasks: BackgroundTasks) -> DocumentRead:
        document = await self._require_readable_document(
            document_id, requester_id)
        if document.owner_id != requester_id:
            raise PermissionDeniedError(
                "Only the document owner can delete it.")
        deleted = DocumentRead.model_validate(document)
        storage_key, entry = await delete_document_row(self._db, document)
        background_tasks.add_task(publish_now, [entry])
        background_tasks.add_task(self._delete_object_quietly, storage_key)
        return deleted

    async def update_document(
            self, document_id: UUID, requester_id: UUID, *,
            title: str | None) -> DocumentRead:
        document = await self._require_readable_document(
            document_id, requester_id)
        if document.owner_id != requester_id:
            raise PermissionDeniedError("Only the document owner can edit it.")
        document.title = title
        await self._db.flush()
        return DocumentRead.model_validate(document)

    async def list_documents(
            self, requester_id: UUID, *, pagination: PaginationParams,
            ticker: str | None = None,
            category: DocumentCategory | None = None,
            status: str | None = None, created_from: date | None = None,
            created_to: date | None = None, content_from: date | None = None,
            content_to: date | None = None, sort: str = "-created_at",
            include_system: bool = False) -> tuple[list[DocumentRead], int]:
        ticker_normalized = ticker.upper() if ticker else None
        org_ids = await auth_client.list_user_organization_ids(
            str(requester_id))
        docs, total = await self.documents.list_for_user(
            requester_id, org_ids, pagination=pagination,
            ticker=ticker_normalized, category=category, status=status,
            created_from=created_from, created_to=created_to,
            content_from=content_from, content_to=content_to, sort=sort,
            include_system_owner=(
                SYSTEM_DOCUMENT_OWNER_ID if include_system else None))
        return [DocumentRead.model_validate(d) for d in docs], total

    async def _require_readable_document(
            self, document_id: UUID, requester_id: UUID) -> Document:
        document = await self.documents.get_by_id(document_id)
        if document is None:
            raise NotFoundError("Document not found.")
        if document.owner_id == requester_id:
            return document
        if document.owner_id == SYSTEM_DOCUMENT_OWNER_ID:
            return document
        if document.organization_id is not None:
            org_ids = await auth_client.list_user_organization_ids(
                str(requester_id))
            if document.organization_id in org_ids:
                return document
        raise NotFoundError("Document not found.")

    async def _is_organization_writer(
            self, user_id: UUID, organization_id: UUID) -> bool:
        role = await auth_client.get_organization_role(
            str(user_id), organization_id)
        return role in auth_client.ORGANIZATION_WRITE_ROLES

    async def note_access(
            self, note: ResearchNote, user: CurrentUser) -> NoteAccess:
        is_author = note.author_id == user.user_id
        readable = is_author
        if not readable and note.organization_id is not None:
            org_ids = await auth_client.list_user_organization_ids(
                str(user.user_id))
            readable = note.organization_id in org_ids
        if (readable and note.is_hidden
                and not (is_author or can_moderate(user))):
            readable = False
        if not readable or not has_permission(user.role,
                                              Permission.NOTE_WRITE):
            return NoteAccess(can_read=readable, can_write=False)
        if is_author:
            return NoteAccess(can_read=True, can_write=True)
        if note.organization_id is None:
            return NoteAccess(can_read=True, can_write=False)
        writable = await self._is_organization_writer(
            user.user_id, note.organization_id)
        return NoteAccess(can_read=True, can_write=writable)

    async def create_note(
            self, user: CurrentUser,
            data: ResearchNoteCreate) -> ResearchNoteRead:
        if data.organization_id is not None:
            await auth_client.ensure_organization_writer(
                user.user_id, data.organization_id)
        note = await self.notes.create(
            author_id=user.user_id, organization_id=data.organization_id,
            title=data.title, content=data.content)
        if data.tags:
            await self.notes.set_note_tags(note.id, data.tags)
        return await self._note_to_read(note, can_edit=True)

    async def get_note(
            self, note_id: UUID, user: CurrentUser) -> ResearchNoteRead:
        note = await self.notes.get_by_id(note_id)
        if note is None:
            raise NotFoundError("Note not found.")
        access = await self.note_access(note, user)
        if not access.can_read:
            raise NotFoundError("Note not found.")
        return await self._note_to_read(note, can_edit=access.can_write)

    async def update_note(
            self, note_id: UUID, user: CurrentUser,
            data: ResearchNoteUpdate) -> NoteChange:
        note = await self.notes.get_by_id(note_id, for_update=True)
        if note is None:
            raise NotFoundError("Note not found.")
        access = await self.note_access(note, user)
        if not access.can_read:
            raise NotFoundError("Note not found.")
        if not access.can_write:
            raise PermissionDeniedError("You cannot edit this note.")
        body_changed = data.content is not None and data.content != (
            note.content or "")
        if body_changed and await yjs_relay.is_note_room_active(note.id):
            raise ConflictError(
                "This note is being edited collaboratively; its body can only "
                "be changed in the live editor until the session ends.")
        title_changed = data.title is not None and data.title != note.title
        if data.title is not None:
            note.title = data.title
        if body_changed:
            note.content = data.content
            note.yjs_state = _body_state(
                note.id, note.yjs_state, data.content or "")
        await self._db.flush()
        await self._db.refresh(note)
        if data.tags is not None:
            await self.notes.set_note_tags(note.id, data.tags)
        read = await self._note_to_read(note, can_edit=True)
        return NoteChange(
            note=read, author_id=note.author_id, text_changed=title_changed
            or body_changed)

    async def delete_note(
            self, note_id: UUID, user: CurrentUser) -> NoteChange:
        note = await self.notes.get_by_id(note_id)
        if note is None:
            raise NotFoundError("Note not found.")
        access = await self.note_access(note, user)
        if not access.can_read:
            raise NotFoundError("Note not found.")
        if note.author_id != user.user_id:
            raise PermissionDeniedError(
                "Only the note's author can delete it.")
        read = await self._note_to_read(note, can_edit=False)
        await self.notes.delete(note)
        return NoteChange(note=read, author_id=note.author_id)

    async def search_notes(
            self, user: CurrentUser, filters: ResearchNoteFilters
    ) -> tuple[list[ResearchNoteRead], int]:
        org_ids = await auth_client.list_user_organization_ids(
            str(user.user_id))
        tag_id = None
        if filters.tag:
            tag = await self.notes.get_tag_by_name(filters.tag)
            if tag is None:
                return [], 0
            tag_id = tag.id

        notes, total = await self.notes.search(
            viewer_id=user.user_id, viewer_org_ids=org_ids, q=filters.q,
            author_id=filters.author_id,
            organization_id=filters.organization_id, tag_id=tag_id,
            sort=filters.sort, offset=filters.offset, limit=filters.limit,
            include_hidden=can_moderate(user))
        tags_by_note = await self.notes.list_tags_for_notes(
            [n.id for n in notes])
        authors = await self._user_refs({n.author_id for n in notes})
        writable_orgs = await self._writable_org_ids(user, notes)
        reads = [
            self._build_note_read(
                n, authors[n.author_id], tags_by_note.get(n.id, []),
                can_edit=self._list_can_edit(n, user, writable_orgs))
            for n in notes
        ]
        return reads, total

    async def _writable_org_ids(
            self, user: CurrentUser, notes: list[ResearchNote]) -> set[UUID]:
        if not has_permission(user.role, Permission.NOTE_WRITE):
            return set()
        if not any(
                n.organization_id is not None and n.author_id != user.user_id
                for n in notes):
            return set()
        try:
            roles = await auth_client.list_user_organization_roles(
                str(user.user_id))
        except Exception:
            logger.warning(
                "organization_roles_lookup_failed", user_id=str(user.user_id),
                exc_info=True)
            return set()
        return {
            org_id
            for org_id, role in roles.items()
            if role in auth_client.ORGANIZATION_WRITE_ROLES
        }

    @staticmethod
    def _list_can_edit(
            note: ResearchNote, user: CurrentUser,
            writable_orgs: set[UUID]) -> bool:
        if not has_permission(user.role, Permission.NOTE_WRITE):
            return False
        if note.author_id == user.user_id:
            return True
        org_id = note.organization_id
        return org_id is not None and org_id in writable_orgs

    async def _user_refs(self, user_ids: set[UUID]) -> dict[UUID, UserRef]:
        ordered = list(user_ids)
        refs = await gather_bounded(
            (auth_client.get_user_ref(str(user_id)) for user_id in ordered),
            limit=20)
        return dict(zip(ordered, refs, strict=True))

    async def _readable_note(
            self, note_id: UUID, user: CurrentUser) -> ResearchNote:
        note = await self.notes.get_by_id(note_id)
        if note is None:
            raise NotFoundError("Note not found.")
        if not (await self.note_access(note, user)).can_read:
            raise NotFoundError("Note not found.")
        return note

    async def _visible_comment(
            self, note: ResearchNote, comment_id: UUID,
            user: CurrentUser) -> ResearchComment:
        comment = await self.comments.get_by_id(comment_id)
        if comment is None or comment.note_id != note.id:
            raise NotFoundError("Comment not found.")
        if comment.is_hidden and not (comment.author_id == user.user_id
                                      or can_moderate(user)):
            raise NotFoundError("Comment not found.")
        return comment

    async def add_comment(
            self, note_id: UUID, user: CurrentUser,
            data: ResearchCommentCreate) -> CommentChange:
        note = await self._readable_note(note_id, user)
        may_comment = note.author_id == user.user_id or (
            note.organization_id is not None and await
            self._is_organization_writer(user.user_id, note.organization_id))
        if not may_comment:
            raise PermissionDeniedError(
                "Only the note's author or members of its organization "
                "(not viewers) can comment on this note.")
        comment = await self.comments.create(
            note_id=note_id, author_id=user.user_id, content=data.content)
        author_ref = await auth_client.get_user_ref(str(user.user_id))
        return CommentChange(
            comment=self._comment_to_read(comment, author_ref),
            comment_author_id=user.user_id, note_author_id=note.author_id,
            note_title=note.title)

    async def update_comment(
            self, note_id: UUID, comment_id: UUID, user: CurrentUser,
            data: ResearchCommentUpdate) -> CommentChange:
        note = await self._readable_note(note_id, user)
        comment = await self._visible_comment(note, comment_id, user)
        if comment.author_id != user.user_id:
            raise PermissionDeniedError(
                "Only the comment's author can edit it.")
        text_changed: bool
        if comment.content != data.content:
            comment.content = data.content
            comment.updated_at = datetime.now(UTC)
            await self._db.flush()
            text_changed = True
        else:
            text_changed = False
        author_ref = await auth_client.get_user_ref(str(user.user_id))
        return CommentChange(
            comment=self._comment_to_read(comment, author_ref),
            comment_author_id=comment.author_id, note_author_id=note.author_id,
            note_title=note.title, text_changed=text_changed)

    async def list_comments(
            self, note_id: UUID, user: CurrentUser, *,
            pagination: PaginationParams
    ) -> tuple[list[ResearchCommentRead], int]:
        await self._readable_note(note_id, user)
        include_hidden = can_moderate(user)
        total = await self.comments.count_for_note(
            note_id, viewer_id=user.user_id, include_hidden=include_hidden)
        comments = await self.comments.list_for_note(
            note_id, offset=pagination.offset, limit=pagination.limit,
            viewer_id=user.user_id, include_hidden=include_hidden)
        authors = await self._user_refs({c.author_id for c in comments})
        return [
            self._comment_to_read(c, authors[c.author_id]) for c in comments
        ], total

    async def delete_comment(
            self, note_id: UUID, comment_id: UUID,
            user: CurrentUser) -> CommentChange:
        note = await self._readable_note(note_id, user)
        comment = await self._visible_comment(note, comment_id, user)
        if user.user_id not in (comment.author_id, note.author_id):
            raise PermissionDeniedError(
                "Only the comment's author or the note's author can delete it."
            )
        return await self._delete_comment(comment, note)

    async def set_note_hidden(
            self, note_id: UUID, moderator: CurrentUser, *,
            hidden: bool) -> NoteChange:
        note = await self.notes.get_by_id(note_id)
        if note is None:
            raise NotFoundError("Note not found.")
        is_author = note.author_id == moderator.user_id
        if note.is_hidden == hidden:
            read = await self._note_to_read(note, can_edit=is_author)
            return NoteChange(
                note=read, author_id=note.author_id, state_changed=False)
        await self._db.execute(
            update(ResearchNote).where(ResearchNote.id == note_id).values(
                is_hidden=hidden,
                hidden_by=moderator.user_id if hidden else None,
                hidden_at=datetime.now(UTC) if hidden else None,
                updated_at=ResearchNote.updated_at))
        await self._db.refresh(note)
        read = await self._note_to_read(note, can_edit=is_author)
        return NoteChange(note=read, author_id=note.author_id)

    async def moderator_delete_note(self, note_id: UUID) -> NoteChange:
        note = await self.notes.get_by_id(note_id)
        if note is None:
            raise NotFoundError("Note not found.")
        read = await self._note_to_read(note, can_edit=False)
        await self.notes.delete(note)
        return NoteChange(note=read, author_id=note.author_id)

    async def _delete_comment(
            self, comment: ResearchComment,
            note: ResearchNote) -> CommentChange:
        author_ref = await auth_client.get_user_ref(str(comment.author_id))
        change = CommentChange(
            comment=self._comment_to_read(comment, author_ref),
            comment_author_id=comment.author_id, note_author_id=note.author_id,
            note_title=note.title)
        await self.comments.delete(comment)
        return change

    async def _comment_with_note(
            self, comment_id: UUID) -> tuple[ResearchComment, ResearchNote]:
        comment = await self.comments.get_by_id(comment_id)
        if comment is None:
            raise NotFoundError("Comment not found.")
        note = await self.notes.get_by_id(comment.note_id)
        if note is None:
            raise NotFoundError("Comment not found.")
        return comment, note

    async def set_comment_hidden(
            self, comment_id: UUID, moderator: CurrentUser, *,
            hidden: bool) -> CommentChange:
        comment, note = await self._comment_with_note(comment_id)
        state_changed = comment.is_hidden != hidden
        if state_changed:
            comment.is_hidden = hidden
            comment.hidden_by = moderator.user_id if hidden else None
            comment.hidden_at = datetime.now(UTC) if hidden else None
            await self._db.flush()
        author_ref = await auth_client.get_user_ref(str(comment.author_id))
        return CommentChange(
            comment=self._comment_to_read(comment, author_ref),
            comment_author_id=comment.author_id, note_author_id=note.author_id,
            note_title=note.title, state_changed=state_changed)

    async def moderator_delete_comment(
            self, comment_id: UUID) -> CommentChange:
        comment, note = await self._comment_with_note(comment_id)
        return await self._delete_comment(comment, note)

    async def sentiment_summary(
            self, user: CurrentUser, *, ticker: str | None,
            date_from: date | None, date_to: date | None) -> SentimentSummary:
        check_date_range(date_from, date_to)
        ticker_normalized = ticker.upper() if ticker else None
        org_ids = await auth_client.list_user_organization_ids(
            str(user.user_id))
        rows = await SentimentRepository(self._db).daily_counts(
            viewer_id=user.user_id, viewer_org_ids=org_ids,
            include_hidden=can_moderate(user), ticker=ticker_normalized,
            date_from=date_from, date_to=date_to)
        totals = SentimentCounts()
        per_kind = {"note": 0, "comment": 0}
        score_sum = 0.0
        days: dict[date, tuple[SentimentCounts, float]] = {}
        for kind, day, label, count, day_score in rows:
            if label not in ("positive", "neutral", "negative"):
                continue
            setattr(totals, label, getattr(totals, label) + count)
            per_kind[kind] += count
            score_sum += day_score
            counts, day_sum = days.get(day, (SentimentCounts(), 0.0))
            setattr(counts, label, getattr(counts, label) + count)
            days[day] = (counts, day_sum + day_score)
        total = per_kind["note"] + per_kind["comment"]
        series = []
        for day in sorted(days):
            counts, day_sum = days[day]
            day_total = counts.positive + counts.neutral + counts.negative
            series.append(
                SentimentDay(
                    date=day, count=day_total, counts=counts, average_score=(
                        round(day_sum / day_total, 4) if day_total else None)))
        return SentimentSummary(
            ticker=ticker_normalized, date_from=date_from, date_to=date_to,
            total=total, notes=per_kind["note"], comments=per_kind["comment"],
            counts=totals,
            average_score=round(score_sum / total, 4) if total else None,
            series=series)

    @staticmethod
    def _comment_to_read(
            comment: ResearchComment, author: UserRef) -> ResearchCommentRead:
        return ResearchCommentRead(
            id=comment.id,
            note_id=comment.note_id,
            author=author,
            content=comment.content,
            created_at=comment.created_at,
            updated_at=comment.updated_at,
            is_hidden=comment.is_hidden,
            hidden_at=comment.hidden_at,
            sentiment_label=comment.sentiment_label,  # type: ignore[arg-type]
            sentiment_score=comment.sentiment_score,
            sentiment_model=comment.sentiment_model,
            sentiment_analyzed_at=comment.sentiment_analyzed_at)

    async def _note_to_read(
            self, note: ResearchNote, *, can_edit: bool) -> ResearchNoteRead:
        author = await auth_client.get_user_ref(str(note.author_id))
        tags = await self.notes.list_tags_for_note(note.id)
        return self._build_note_read(note, author, tags, can_edit=can_edit)

    @staticmethod
    def _build_note_read(
            note: ResearchNote, author: UserRef, tags: list[str], *,
            can_edit: bool) -> ResearchNoteRead:
        return ResearchNoteRead(
            id=note.id,
            author=author,
            organization_id=note.organization_id,
            title=note.title,
            content=note.content,
            tags=tags,
            created_at=note.created_at,
            updated_at=note.updated_at,
            is_hidden=note.is_hidden,
            hidden_at=note.hidden_at,
            can_edit=can_edit,
            sentiment_label=note.sentiment_label,  # type: ignore[arg-type]
            sentiment_score=note.sentiment_score,
            sentiment_model=note.sentiment_model,
            sentiment_analyzed_at=note.sentiment_analyzed_at)
