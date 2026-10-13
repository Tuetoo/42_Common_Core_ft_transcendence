from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Literal
from urllib.parse import quote
from uuid import UUID
from database import get_db
from deps import check_date_range, get_current_user
from fastapi import (APIRouter, BackgroundTasks, Depends, Form, Query, Request,
                     Response, UploadFile, status)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from yjs_relay import relay
from domains.notification.service import notify_after_commit
from domains.research.schema import (DocumentRead, DocumentSort,
                                     DocumentUpdate, ResearchCommentCreate,
                                     ResearchCommentRead,
                                     ResearchCommentUpdate, ResearchNoteCreate,
                                     ResearchNoteFilters, ResearchNoteRead,
                                     ResearchNoteUpdate, SentimentSummary)
from domains.research.sentiment import (refresh_comment_sentiment,
                                        refresh_note_sentiment)
from domains.research.service import (CommentChange, NoteChange,
                                      ResearchService)
from shared.enums import DocumentCategory, DocumentStatus, NotificationType
from shared.exceptions import error_response
from shared.permissions import Permission
from shared.schemas import (MAX_PAGE, APIResponse, PaginatedResponse,
                            PaginationParams)
from shared.security import CurrentUser, require_permission

router = APIRouter(tags=["research"])

note_writer = require_permission(Permission.NOTE_WRITE)(get_current_user)
comment_writer = require_permission(Permission.COMMENT_WRITE)(get_current_user)
document_uploader = require_permission(
    Permission.DOCUMENT_UPLOAD)(get_current_user)


def quoted_title(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return f'"{text[: limit - 1]}\u2026"' if len(text) > limit else f'"{text}"'


@router.post(
    "/notes", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[ResearchNoteRead])
async def create_note(
    data: ResearchNoteCreate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(note_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ResearchNoteRead]:
    result = await ResearchService(db).create_note(current_user, data)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.NOTE_CREATED, title="Note created",
        content=f"You created the note {quoted_title(result.title)}.")
    background_tasks.add_task(refresh_note_sentiment, result.id)
    return APIResponse(data=result)


@router.get("/notes", response_model=PaginatedResponse[ResearchNoteRead])
async def search_notes(
    q: str | None = None, tag: str | None = None,
    author_id: UUID | None = None, organization_id: UUID | None = None,
    sort: Literal["created_at", "-created_at", "title",
                  "-title"] = Query(default="-created_at"),
    page: int = Query(default=1, ge=1, le=MAX_PAGE), page_size: int = Query(
        default=20, ge=1,
        le=100), current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> PaginatedResponse[ResearchNoteRead]:
    filters = ResearchNoteFilters(
        q=q, tag=tag, author_id=author_id, organization_id=organization_id,
        sort=sort, page=page, page_size=page_size)
    items, total = await ResearchService(db).search_notes(
        current_user, filters)
    return PaginatedResponse(
        items=items, total=total, page=page, page_size=page_size)


@router.get("/notes/{note_id}", response_model=APIResponse[ResearchNoteRead])
async def get_note(
    note_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ResearchNoteRead]:
    result = await ResearchService(db).get_note(note_id, current_user)
    return APIResponse(data=result)


@router.patch(
    "/notes/{note_id}", response_model=APIResponse[ResearchNoteRead],
    description=(
        "Update a note. Returns 409 when `content` changes while a "
        "collaborative "
        "editing session on the note is open (edit the body in the live "
        "editor "
        "instead); title and tag edits are always allowed."))
async def update_note(
    note_id: UUID, data: ResearchNoteUpdate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(note_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ResearchNoteRead]:
    change = await ResearchService(db).update_note(note_id, current_user, data)
    title = quoted_title(change.note.title)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.NOTE_UPDATED, title="Note updated",
        content=f"You updated the note {title}.")
    if change.author_id != current_user.user_id:
        notify_after_commit(
            background_tasks, [change.author_id],
            type=NotificationType.NOTE_UPDATED, title="Your note was edited",
            content=f"A member of your organization edited your note {title}.")
    if change.text_changed:
        background_tasks.add_task(refresh_note_sentiment, note_id)
    return APIResponse(data=change.note)


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(note_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    change = await ResearchService(db).delete_note(note_id, current_user)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.NOTE_DELETED, title="Note deleted",
        content=f"You deleted the note {quoted_title(change.note.title)}.")
    background_tasks.add_task(
        relay.close_note_sessions, str(note_id), reason="note deleted",
        discard_edits=True)


@router.post(
    "/notes/{note_id}/comments", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[ResearchCommentRead])
async def add_comment(
    note_id: UUID, data: ResearchCommentCreate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(comment_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ResearchCommentRead]:
    change = await ResearchService(db).add_comment(note_id, current_user, data)
    title = quoted_title(change.note_title)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.COMMENT_CREATED, title="Comment posted",
        content=f"You commented on the note {title}.")
    if change.note_author_id != current_user.user_id:
        notify_after_commit(
            background_tasks, [change.note_author_id],
            type=NotificationType.COMMENT_CREATED,
            title="New comment on your note", content=(
                f"{change.comment.author.username} commented on your note "
                f"{title}."))
    background_tasks.add_task(refresh_comment_sentiment, change.comment.id)
    return APIResponse(data=change.comment)


@router.get(
    "/notes/{note_id}/comments",
    response_model=PaginatedResponse[ResearchCommentRead], description=(
        "Comments of a note, oldest first. Same read rule as the note."))
async def list_comments(
    note_id: UUID, page: int = Query(default=1, ge=1, le=MAX_PAGE),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> PaginatedResponse[ResearchCommentRead]:
    pagination = PaginationParams(page=page, page_size=page_size)
    items, total = await ResearchService(db).list_comments(
        note_id, current_user, pagination=pagination)
    return PaginatedResponse(
        items=items, total=total, page=pagination.page,
        page_size=pagination.page_size)


@router.patch(
    "/notes/{note_id}/comments/{comment_id}",
    response_model=APIResponse[ResearchCommentRead],
    description="The comment's author edits its text.")
async def update_comment(
    note_id: UUID, comment_id: UUID, data: ResearchCommentUpdate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(comment_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ResearchCommentRead]:
    change = await ResearchService(db).update_comment(
        note_id, comment_id, current_user, data)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.COMMENT_UPDATED, title="Comment updated",
        content=(
            f"You edited your comment on the note "
            f"{quoted_title(change.note_title)}."))
    if change.text_changed:
        background_tasks.add_task(refresh_comment_sentiment, comment_id)
    return APIResponse(data=change.comment)


@router.delete(
    "/notes/{note_id}/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT, description=(
        "Delete a comment. Allowed for the comment author or the note author; "
        "a comment hidden by a moderator can only be deleted by its author."
    ))
async def delete_comment(
    note_id: UUID, comment_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(comment_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    change = await ResearchService(db).delete_comment(
        note_id, comment_id, current_user)
    _notify_comment_deleted(background_tasks, change, current_user.user_id)


def _notify_comment_deleted(
        background_tasks: BackgroundTasks, change: CommentChange,
        actor_id: UUID) -> None:
    title = quoted_title(change.note_title)
    notify_after_commit(
        background_tasks, [actor_id], type=NotificationType.COMMENT_DELETED,
        title="Comment deleted",
        content=f"You deleted a comment on the note {title}.")
    if change.comment_author_id != actor_id:
        notify_after_commit(
            background_tasks, [change.comment_author_id],
            type=NotificationType.COMMENT_DELETED,
            title="Your comment was removed",
            content=f"Your comment on the note {title} was deleted.")


@router.get(
    "/sentiment/summary", response_model=SentimentSummary, description=(
        "Sentiment of the research notes and comments the caller may read: "
        "counts"
        " per label, average score (-1..1) and a per-day series (UTC creation "
        "day). `ticker` selects notes tagged with it or mentioning it (and "
        "their "
        "comments)."))
async def sentiment_summary(
        ticker: str | None = Query(
            default=None, min_length=1, max_length=10,
            pattern=r"^[A-Za-z0-9.\-]+$"),
        date_from: date | None = Query(default=None, alias="from"),
        date_to: date | None = Query(default=None, alias="to"),
        current_user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db,
                                   scope="function")) -> SentimentSummary:
    return await ResearchService(db).sentiment_summary(
        current_user, ticker=ticker, date_from=date_from, date_to=date_to)


def _content_disposition(filename: str) -> str:
    fallback = (
        "".join(
            ch for ch in filename
            if 32 <= ord(ch) < 127 and ch not in '"\\').strip() or "download")
    encoded = quote(filename, safe="")
    return f"inline; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


@router.post(
    "/documents", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[DocumentRead])
async def upload_document(
    background_tasks: BackgroundTasks, file: UploadFile,
    category: str = Form(...), ticker: str | None = Form(default=None),
    organization_id: UUID | None = Form(default=None),
    title: str | None = Form(default=None, max_length=256),
    content_date: datetime | None = Form(default=None),
    current_user: CurrentUser = Depends(document_uploader),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[DocumentRead]:
    content = await file.read()
    outcome = await ResearchService(db).upload(
        owner_id=current_user.user_id, organization_id=organization_id,
        filename=file.filename or "upload", content=content, category=category,
        ticker=ticker, content_date=content_date, title=(title or "").strip()
        or None, background_tasks=background_tasks)
    name = quoted_title(outcome.document.title or outcome.document.filename)
    if outcome.retried:
        notify_after_commit(
            background_tasks, [current_user.user_id],
            type=NotificationType.DOCUMENT_UPDATED,
            title="Document re-uploaded", content=(
                f"You re-uploaded the document {name}; it is processed again."
            ))
    elif not outcome.duplicate:
        notify_after_commit(
            background_tasks, [current_user.user_id],
            type=NotificationType.DOCUMENT_UPLOADED, title="Document uploaded",
            content=f"You uploaded the document {name}.")
    return APIResponse(data=outcome.document)


@router.get("/documents", response_model=PaginatedResponse[DocumentRead])
async def list_documents(
        page: int = Query(default=1, ge=1, le=MAX_PAGE),
        page_size: int = Query(default=20, ge=1, le=100),
        ticker: str | None = Query(default=None, max_length=10),
        category: DocumentCategory | None = Query(default=None),
        status_filter: DocumentStatus | None = Query(
            default=None,
            alias="status"), created_from: date | None = Query(default=None),
        created_to: date | None = Query(default=None),
        content_from: date | None = Query(default=None),
        content_to: date | None = Query(default=None),
        sort: DocumentSort = Query(default="-created_at"),
        include_system: bool = Query(default=False),
        current_user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(
            get_db, scope="function")) -> PaginatedResponse[DocumentRead]:
    for low, high, name in ((created_from, created_to, "created"),
                            (content_from, content_to, "content")):
        check_date_range(
            low, high, f"'{name}_from' must not be after '{name}_to'.")
    pagination = PaginationParams(page=page, page_size=page_size)
    items, total = await ResearchService(db).list_documents(
        current_user.user_id, pagination=pagination, ticker=ticker,
        category=category, status=status_filter, created_from=created_from,
        created_to=created_to, content_from=content_from,
        content_to=content_to, sort=sort, include_system=include_system)
    return PaginatedResponse(
        items=items, total=total, page=pagination.page,
        page_size=pagination.page_size)


@router.get(
    "/documents/{document_id}", response_model=APIResponse[DocumentRead])
async def get_document(
    document_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[DocumentRead]:
    result = await ResearchService(db).get_document(
        document_id, current_user.user_id)
    return APIResponse(data=result)


@router.get(
    "/documents/{document_id}/content", description=(
        "Download the stored file of a document. Same access rule as the "
        "other "
        "document endpoints."))
async def download_document_content(
    document_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> Response:
    metadata, content = await ResearchService(db).download_document(
        document_id, current_user.user_id)
    media_type = {
        "pdf": "application/pdf",
        "txt": "text/plain",
        "md": "text/markdown"
    }.get(metadata.document_type, "application/octet-stream")
    return Response(
        content=content, media_type=media_type, headers={
            "Content-Disposition": _content_disposition(metadata.filename),
            "X-Content-Type-Options": "nosniff"
        })


_PREVIEW_TYPES = {
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
    "md": "text/plain; charset=utf-8"
}
_PREVIEW_CHUNK_BYTES = 64 * 1024


@router.get(
    "/documents/{document_id}/preview", description=(
        "The stored file with `Content-Disposition: inline`, for an in-page "
        "viewer (iframe or img). Same read rule as the download. Markdown is "
        "served as text/plain (never rendered as HTML)."))
async def preview_document(
    document_id: UUID, request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> Response:
    metadata = await ResearchService(db).get_document(
        document_id, current_user.user_id)
    media_type = _PREVIEW_TYPES.get(metadata.document_type.lower())
    if media_type is None:
        return error_response(
            request, status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "unsupported_media_type",
            f"No inline preview for '{metadata.document_type}' files.")
    content = await ResearchService.document_content(metadata)

    async def _chunks() -> AsyncIterator[bytes]:
        for offset in range(0, len(content), _PREVIEW_CHUNK_BYTES):
            yield content[offset:offset + _PREVIEW_CHUNK_BYTES]

    headers = {
        "Content-Disposition": _content_disposition(metadata.filename),
        "Content-Length": str(len(content)),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store"
    }
    if media_type.startswith("text/"):
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return StreamingResponse(_chunks(), media_type=media_type, headers=headers)


@router.patch(
    "/documents/{document_id}", response_model=APIResponse[DocumentRead],
    description="Set or clear the display title of a document (owner only).")
async def update_document(
    document_id: UUID, data: DocumentUpdate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(document_uploader),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[DocumentRead]:
    result = await ResearchService(db).update_document(
        document_id, current_user.user_id, title=data.title)
    name = quoted_title(result.title or result.filename)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.DOCUMENT_UPDATED, title="Document updated",
        content=f"You renamed the document {name}.")
    return APIResponse(data=result)


@router.delete(
    "/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(document_uploader),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    deleted = await ResearchService(db).delete_document(
        document_id, current_user.user_id, background_tasks=background_tasks)
    name = quoted_title(deleted.title or deleted.filename)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.DOCUMENT_DELETED, title="Document deleted",
        content=f"You deleted the document {name}.")


moderation_router = APIRouter(tags=["moderation"])

moderator_dep = require_permission(
    Permission.CONTENT_MODERATE)(get_current_user)


def _notify_note(
        background_tasks: BackgroundTasks, change: NoteChange,
        moderator_id: UUID, *, type: NotificationType, author_title: str,
        verb: str) -> None:
    if not change.state_changed:
        return
    name = quoted_title(change.note.title)
    if change.author_id != moderator_id:
        notify_after_commit(
            background_tasks, [change.author_id], type=type,
            title=author_title,
            content=f"A moderator {verb} your note {name}.")
    notify_after_commit(
        background_tasks, [moderator_id], type=type, title=f"Note {verb}",
        content=f"You {verb} the note {name}.")


def _notify_comment(
        background_tasks: BackgroundTasks, change: CommentChange,
        moderator_id: UUID, *, type: NotificationType, author_title: str,
        verb: str) -> None:
    if not change.state_changed:
        return
    name = quoted_title(change.note_title)
    if change.comment_author_id != moderator_id:
        notify_after_commit(
            background_tasks, [change.comment_author_id], type=type,
            title=author_title,
            content=f"A moderator {verb} your comment on the note {name}.")
    notify_after_commit(
        background_tasks, [moderator_id], type=type, title=f"Comment {verb}",
        content=f"You {verb} a comment on the note {name}.")


@moderation_router.post(
    "/notes/{note_id}/hide", response_model=APIResponse[ResearchNoteRead])
async def hide_note(
    note_id: UUID, background_tasks: BackgroundTasks,
    moderator: CurrentUser = Depends(moderator_dep),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ResearchNoteRead]:
    change = await ResearchService(db).set_note_hidden(
        note_id, moderator, hidden=True)
    _notify_note(
        background_tasks, change, moderator.user_id,
        type=NotificationType.NOTE_HIDDEN, author_title="Your note was hidden",
        verb="hid")
    background_tasks.add_task(
        relay.close_note_sessions, str(note_id),
        reason="note hidden by a moderator", discard_edits=False)
    return APIResponse(data=change.note)


@moderation_router.post(
    "/notes/{note_id}/unhide", response_model=APIResponse[ResearchNoteRead])
async def unhide_note(
    note_id: UUID, background_tasks: BackgroundTasks,
    moderator: CurrentUser = Depends(moderator_dep),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ResearchNoteRead]:
    change = await ResearchService(db).set_note_hidden(
        note_id, moderator, hidden=False)
    _notify_note(
        background_tasks, change, moderator.user_id,
        type=NotificationType.NOTE_UPDATED,
        author_title="Your note is visible again", verb="restored")
    return APIResponse(data=change.note)


@moderation_router.delete(
    "/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT,
    name="delete_note")
async def moderator_delete_note(
    note_id: UUID, background_tasks: BackgroundTasks,
    moderator: CurrentUser = Depends(moderator_dep),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    change = await ResearchService(db).moderator_delete_note(note_id)
    _notify_note(
        background_tasks, change, moderator.user_id,
        type=NotificationType.NOTE_DELETED,
        author_title="Your note was removed", verb="deleted")
    background_tasks.add_task(
        relay.close_note_sessions, str(note_id),
        reason="note deleted by a moderator", discard_edits=True)


@moderation_router.post(
    "/comments/{comment_id}/hide",
    response_model=APIResponse[ResearchCommentRead])
async def hide_comment(
    comment_id: UUID, background_tasks: BackgroundTasks,
    moderator: CurrentUser = Depends(moderator_dep),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ResearchCommentRead]:
    change = await ResearchService(db).set_comment_hidden(
        comment_id, moderator, hidden=True)
    _notify_comment(
        background_tasks, change, moderator.user_id,
        type=NotificationType.COMMENT_HIDDEN,
        author_title="Your comment was hidden", verb="hid")
    return APIResponse(data=change.comment)


@moderation_router.post(
    "/comments/{comment_id}/unhide",
    response_model=APIResponse[ResearchCommentRead])
async def unhide_comment(
    comment_id: UUID, background_tasks: BackgroundTasks,
    moderator: CurrentUser = Depends(moderator_dep),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ResearchCommentRead]:
    change = await ResearchService(db).set_comment_hidden(
        comment_id, moderator, hidden=False)
    _notify_comment(
        background_tasks, change, moderator.user_id,
        type=NotificationType.COMMENT_UPDATED,
        author_title="Your comment is visible again", verb="restored")
    return APIResponse(data=change.comment)


@moderation_router.delete(
    "/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT,
    name="delete_comment")
async def moderator_delete_comment(
    comment_id: UUID, background_tasks: BackgroundTasks,
    moderator: CurrentUser = Depends(moderator_dep),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    change = await ResearchService(db).moderator_delete_comment(comment_id)
    _notify_comment(
        background_tasks, change, moderator.user_id,
        type=NotificationType.COMMENT_DELETED,
        author_title="Your comment was removed", verb="deleted")
