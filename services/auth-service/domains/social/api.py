from uuid import UUID
from database import get_db
from deps import get_current_user
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from domains.social.schema import ConnectionRead, ConnectionRequestCreate
from domains.social.service import SocialService
from shared.enums.connection import ConnectionStatus
from shared.schemas.base import APIResponse
from shared.security import CurrentUser

router = APIRouter(tags=["connections"])


@router.post("", status_code=status.HTTP_201_CREATED,
             response_model=APIResponse[ConnectionRead])
async def send_connection_request(
    data: ConnectionRequestCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[ConnectionRead]:
    result = await SocialService(db).send_request(
        current_user.user_id, data.addressee_username)
    return APIResponse(data=result)


@router.get("", response_model=APIResponse[list[ConnectionRead]])
async def list_connections(status_filter: ConnectionStatus | None = Query(
    default=None, alias="status"),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[list[ConnectionRead]]:
    result = await SocialService(db).list_my_connections(
        current_user.user_id, status_filter)
    return APIResponse(data=result)


@router.post("/{connection_id}/accept",
             response_model=APIResponse[ConnectionRead])
async def accept_connection(
    connection_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[ConnectionRead]:
    result = await SocialService(db).accept(
        connection_id, current_user.user_id)
    return APIResponse(data=result)


@router.post("/{connection_id}/block",
             response_model=APIResponse[ConnectionRead])
async def block_connection(
    connection_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[ConnectionRead]:
    result = await SocialService(db).block(connection_id, current_user.user_id)
    return APIResponse(data=result)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_connection(
    connection_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> None:
    await SocialService(db).remove(connection_id, current_user.user_id)
