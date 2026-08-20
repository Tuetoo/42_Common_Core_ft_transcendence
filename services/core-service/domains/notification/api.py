from uuid import UUID
from database import get_db
from deps import get_current_user
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from domains.notification.schema import (
    NotificationRead, NotificationMarkReadRequest,
    NotificationMarkReadResponse)
from domains.notification.service import NotificationService
from shared.schemas.base import (
    APIResponse, PaginatedResponse, PaginationParams)
from shared.security import CurrentUser

router = APIRouter(tags=["notifications"])


@router.get("", response_model=PaginatedResponse[NotificationRead])
async def list_notifications(
    unread_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> PaginatedResponse[NotificationRead]:
    pagination = PaginationParams(page=page, page_size=page_size)
    items, total = await NotificationService(db).list_for_user(
        current_user.user_id, unread_only=unread_only, pagination=pagination)
    return PaginatedResponse(
        items=items, total=total, page=page, page_size=page_size)


@router.post("/{notification_id}/read",
             response_model=APIResponse[NotificationRead])
async def mark_read(notification_id: UUID, current_user: CurrentUser = Depends(
    get_current_user), db: AsyncSession = Depends(get_db)
) -> APIResponse[NotificationRead]:
    result = await NotificationService(db).mark_read(
        notification_id, current_user.user_id)
    return APIResponse(data=result)


@router.post("/read", response_model=APIResponse[NotificationMarkReadResponse])
async def mark_read_bulk(body: NotificationMarkReadRequest,
                         current_user: CurrentUser = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)
                         ) -> APIResponse[NotificationMarkReadResponse]:
    marked_count = await NotificationService(db).mark_read_bulk(
        body.notification_ids, current_user.user_id)
    return APIResponse(data=NotificationMarkReadResponse(
        marked_count=marked_count))
