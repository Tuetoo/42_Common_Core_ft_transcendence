from pathlib import Path
from uuid import UUID
from config import settings
from database import get_db
from deps import (require_site_stats, require_user_deactivate,
                  require_user_manage)
from events_out import schedule_events
from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from domains.admin.schema import (AdminStats, AdminUserDetail,
                                  AdminUserListItem, AdminUserSort,
                                  AdminUserUpdate, SortOrder)
from domains.admin.service import AdminService
from domains.auth.service import (delete_stored_avatar,
                                  send_verification_email_safely)
from shared.enums import SystemRole
from shared.schemas import (DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE,
                            APIResponse, PaginatedResponse, PaginationParams)
from shared.security import CurrentUser

router = APIRouter(tags=["admin"])


@router.get(
    "/users", response_model=APIResponse[PaginatedResponse[AdminUserListItem]])
async def list_users(
    page: int = Query(default=1, ge=1, le=MAX_PAGE), page_size: int = Query(
        default=DEFAULT_PAGE_SIZE, ge=1,
        le=MAX_PAGE_SIZE), q: str | None = Query(
            default=None, max_length=254,
            description="Case-insensitive match on email or username."),
    role: SystemRole | None = None, is_active: bool | None = None,
    include_deleted: bool = False,
    sort: AdminUserSort = AdminUserSort.CREATED_AT, order: SortOrder = "desc",
    _: CurrentUser = Depends(require_user_manage),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[PaginatedResponse[AdminUserListItem]]:
    result = await AdminService(db).list_users(
        pagination=PaginationParams(page=page, page_size=page_size), q=q,
        role=role, is_active=is_active, include_deleted=include_deleted,
        sort=sort, order=order)
    return APIResponse(data=result)


@router.get("/users/{user_id}", response_model=APIResponse[AdminUserDetail])
async def get_user(
    user_id: UUID, _: CurrentUser = Depends(require_user_manage),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[AdminUserDetail]:
    return APIResponse(data=await AdminService(db).get_user(user_id))


@router.patch("/users/{user_id}", response_model=APIResponse[AdminUserDetail])
async def update_user(
    user_id: UUID, data: AdminUserUpdate, background_tasks: BackgroundTasks,
    actor: CurrentUser = Depends(require_user_manage),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[AdminUserDetail]:
    service = AdminService(db)
    result = await service.update_user(actor, user_id, data)
    schedule_events(background_tasks, service.pending_events)
    if service.email_dispatch is not None:
        background_tasks.add_task(
            send_verification_email_safely, service.email_dispatch)
    return APIResponse(data=result)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UUID, background_tasks: BackgroundTasks,
    actor: CurrentUser = Depends(require_user_manage),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    service = AdminService(db)
    await service.delete_user(actor, user_id)
    schedule_events(background_tasks, service.pending_events)
    if service.removed_avatar_url:
        background_tasks.add_task(
            delete_stored_avatar, service.removed_avatar_url,
            Path(settings.AVATAR_STORAGE_DIR))


@router.delete(
    "/users/{user_id}/2fa", status_code=status.HTTP_204_NO_CONTENT,
    description=(
        "Remove the user's two-factor authentication. Returns 204 also when "
        "none "
        "was set up."))
async def reset_two_factor(
    user_id: UUID, background_tasks: BackgroundTasks,
    actor: CurrentUser = Depends(require_user_manage),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    service = AdminService(db)
    await service.reset_two_factor(actor, user_id)
    schedule_events(background_tasks, service.pending_events)


@router.post(
    "/users/{user_id}/deactivate", response_model=APIResponse[AdminUserDetail])
async def deactivate_user(
    user_id: UUID, background_tasks: BackgroundTasks,
    actor: CurrentUser = Depends(require_user_deactivate),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[AdminUserDetail]:
    service = AdminService(db)
    result = await service.set_active(actor, user_id, False)
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=result)


@router.post(
    "/users/{user_id}/reactivate", response_model=APIResponse[AdminUserDetail])
async def reactivate_user(
    user_id: UUID, background_tasks: BackgroundTasks,
    actor: CurrentUser = Depends(require_user_deactivate),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[AdminUserDetail]:
    service = AdminService(db)
    result = await service.set_active(actor, user_id, True)
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=result)


@router.get("/stats", response_model=APIResponse[AdminStats])
async def site_stats(
        _: CurrentUser = Depends(require_site_stats),
        db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[AdminStats]:
    return APIResponse(data=await AdminService(db).stats())
