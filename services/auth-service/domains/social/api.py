from uuid import UUID
from database import get_db
from deps import get_current_user, require_connection_write
from events_out import schedule_events
from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from domains.social.schema import ConnectionRead, ConnectionRequestCreate
from domains.social.service import SocialService
from shared.enums import ConnectionStatus
from shared.schemas import APIResponse
from shared.security import CurrentUser

router = APIRouter(tags=["connections"])


@router.post(
    "", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[ConnectionRead])
async def send_connection_request(
    data: ConnectionRequestCreate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(require_connection_write),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ConnectionRead]:
    service = SocialService(db)
    result = await service.send_request(
        current_user.user_id, data.addressee_username)
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=result)


@router.get("", response_model=APIResponse[list[ConnectionRead]])
async def list_connections(
        status_filter: ConnectionStatus | None = Query(
            default=None, alias="status"),
        current_user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[list[ConnectionRead]]:
    result = await SocialService(db).list_my_connections(
        current_user.user_id, status_filter)
    return APIResponse(data=result)


@router.post(
    "/{connection_id}/accept", response_model=APIResponse[ConnectionRead])
async def accept_connection(
    connection_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(require_connection_write),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ConnectionRead]:
    service = SocialService(db)
    result = await service.accept(connection_id, current_user.user_id)
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=result)


@router.post(
    "/{connection_id}/block", response_model=APIResponse[ConnectionRead])
async def block_connection(
    connection_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ConnectionRead]:
    service = SocialService(db)
    result = await service.block(connection_id, current_user.user_id)
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=result)


@router.delete(
    "/{connection_id}", status_code=status.HTTP_204_NO_CONTENT, description=(
        "Remove a friend, decline a pending request, cancel your own pending "
        "request, or unblock a user (only the user who blocked can unblock)."))
async def remove_connection(
    connection_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    service = SocialService(db)
    await service.remove(connection_id, current_user.user_id)
    schedule_events(background_tasks, service.pending_events)
