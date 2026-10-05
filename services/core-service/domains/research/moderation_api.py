from uuid import UUID
from database import get_db
from deps import get_current_user
from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from yjs_relay import relay
from domains.notification.actions import notify_after_commit
from domains.research.api import quoted_title
from domains.research.schema import ResearchCommentRead, ResearchNoteRead
from domains.research.service import CommentChange, NoteChange, ResearchService
from shared.enums import NotificationType
from shared.permissions import Permission
from shared.schemas import APIResponse
from shared.security import CurrentUser, require_permission

router = APIRouter(tags=["moderation"])

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


@router.post(
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


@router.post(
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


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
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


@router.post(
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


@router.post(
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


@router.delete(
    "/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(
    comment_id: UUID, background_tasks: BackgroundTasks,
    moderator: CurrentUser = Depends(moderator_dep),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    change = await ResearchService(db).moderator_delete_comment(comment_id)
    _notify_comment(
        background_tasks, change, moderator.user_id,
        type=NotificationType.COMMENT_DELETED,
        author_title="Your comment was removed", verb="deleted")
