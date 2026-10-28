from dataclasses import asdict
from typing import Any
from uuid import UUID
from config import settings
from database import get_db
from events_out import schedule_events
from fastapi import APIRouter, BackgroundTasks, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from user_state_sync import state_of
from domains.admin.service import AdminService
from domains.auth.repository import UserRepository
from domains.auth.service import (AuthService, avatar_or_default,
                                  is_user_online)
from domains.organization.repository import OrganizationRepository
from domains.social.repository import ConnectionRepository
from shared.deleted_users import DELETED_USERS_BATCH_SIZE
from shared.enums import ConnectionStatus
from shared.scheduled_jobs import job_response
from shared.schemas import OrganizationRef, UserRef
from shared.security import require_internal_token

router = APIRouter(
    prefix="/internal", tags=["internal"], include_in_schema=False,
    dependencies=[Depends(require_internal_token)])


class PresenceUpdate(BaseModel):
    online: bool


class UserIdsQuery(BaseModel):
    user_ids: list[UUID] = Field(max_length=DELETED_USERS_BATCH_SIZE)


class DeletedUserIdsRead(BaseModel):
    deleted_ids: list[UUID]


@router.post("/users/deleted", response_model=DeletedUserIdsRead)
async def list_deleted_user_ids(
    data: UserIdsQuery, db: AsyncSession = Depends(get_db, scope="function")
) -> DeletedUserIdsRead:
    deleted = await UserRepository(db).list_deleted_ids(data.user_ids)
    return DeletedUserIdsRead(deleted_ids=sorted(deleted, key=str))


class OrganizationIdsQuery(BaseModel):
    organization_ids: list[UUID] = Field(max_length=DELETED_USERS_BATCH_SIZE)


class MissingOrganizationIdsRead(BaseModel):
    missing_ids: list[UUID]


@router.post(
    "/organizations/missing", response_model=MissingOrganizationIdsRead)
async def list_missing_organization_ids(
    data: OrganizationIdsQuery,
    db: AsyncSession = Depends(get_db, scope="function")
) -> MissingOrganizationIdsRead:
    missing = await OrganizationRepository(db).list_missing_ids(
        data.organization_ids)
    return MissingOrganizationIdsRead(missing_ids=sorted(missing, key=str))


@router.get("/users/{user_id}", response_model=UserRef)
async def get_user_ref(
    user_id: UUID, db: AsyncSession = Depends(get_db, scope="function")
) -> UserRef:
    users = UserRepository(db)
    user = await users.require(user_id, f"User {user_id} not found.")

    profile = await users.get_profile(user_id)
    return UserRef(
        id=user.id, username=user.username, avatar_url=avatar_or_default(
            profile.avatar_url if profile else None, user.username),
        is_online=is_user_online(user.last_seen_at))


class UserStateRead(BaseModel):
    role: str
    is_active: bool


@router.get("/users/{user_id}/state", response_model=UserStateRead)
async def get_user_state(
    user_id: UUID, db: AsyncSession = Depends(get_db, scope="function")
) -> UserStateRead:
    user = await UserRepository(db).require(
        user_id, f"User {user_id} not found.")
    state = state_of(user)
    return UserStateRead(role=state.role, is_active=state.is_active)


@router.post(
    "/users/{user_id}/presence", status_code=status.HTTP_204_NO_CONTENT)
async def set_user_presence(
    user_id: UUID, data: PresenceUpdate, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    service = AuthService(db)
    await service.set_presence(user_id, data.online)
    schedule_events(background_tasks, service.pending_events)


@router.get(
    "/users/{user_id}/organizations", response_model=list[OrganizationRef])
async def list_user_organizations(
    user_id: UUID, db: AsyncSession = Depends(get_db, scope="function")
) -> list[OrganizationRef]:
    orgs = OrganizationRepository(db)
    memberships = await orgs.list_for_user_with_roles(user_id)
    return [
        OrganizationRef(id=org.id, name=org.name, role=role)
        for org, role in memberships
    ]


class FriendIdsRead(BaseModel):
    friend_ids: list[UUID]


@router.get("/users/{user_id}/friends", response_model=FriendIdsRead)
async def list_friend_ids(
    user_id: UUID, db: AsyncSession = Depends(get_db, scope="function")
) -> FriendIdsRead:
    friend_ids = await ConnectionRepository(db).list_friend_ids(user_id)
    return FriendIdsRead(friend_ids=friend_ids)


@router.get("/connections/check")
async def check_connection(
    user_a: UUID, user_b: UUID,
    db: AsyncSession = Depends(get_db, scope="function")
) -> dict[str, Any]:
    connections = ConnectionRepository(db)
    connection = await connections.get_between(user_a, user_b)
    connected = (
        connection is not None
        and connection.status == ConnectionStatus.ACCEPTED)
    return {"connected": connected}


@router.get("/users/{user_id}/notification-settings")
async def get_notification_settings(
    user_id: UUID, db: AsyncSession = Depends(get_db, scope="function")
) -> dict[str, Any]:
    users = UserRepository(db)
    user = await users.require(user_id, f"User {user_id} not found.")
    settings_row = await users.get_settings(user_id)
    deliverable = (
        user.deleted_at is None and user.is_active and user.is_verified)
    return {
        "email": user.email if deliverable else None,
        "deleted": user.deleted_at is not None,
        "notification_preferences": (
            settings_row.notification_preferences if settings_row else None)
    }


@router.post("/scheduler/purge-unverified", response_model=None)
async def purge_unverified_accounts(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> JSONResponse:
    service = AdminService(db)
    result = await service.purge_unverified_accounts()
    leftovers = await service.clean_deleted_account_leftovers()
    expired_invitations = await service.purge_expired_invitations()
    schedule_events(background_tasks, service.pending_events)
    return job_response(
        "purge-unverified", {
            "retention_days": settings.UNVERIFIED_ACCOUNT_RETENTION_DAYS,
            "candidates": result.candidates,
            "deleted": result.deleted,
            "skipped": result.skipped,
            "failed": result.failed,
            "deleted_account_leftovers": asdict(leftovers),
            "expired_invitations_deleted": expired_invitations or 0
        }, steps={
            "accounts": (result.attempted, result.failed),
            "deleted_account_leftovers": (leftovers.candidates,
                                          leftovers.failed),
            "deleted_account_leftovers_lookup": (
                1, int(leftovers.lookup_failed)),
            "expired_invitations": (1, int(expired_invitations is None))
        })
