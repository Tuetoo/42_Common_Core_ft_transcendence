from pathlib import Path
from typing import Any
from uuid import UUID
from config import settings
from database import get_db
from events_out import schedule_events
from fastapi import APIRouter, BackgroundTasks, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from user_state_sync import state_of
from domains.admin.service import AdminService
from domains.auth.repository import UserRepository
from domains.auth.service import (AuthService, avatar_or_default,
                                  delete_stored_avatar, is_user_online)
from domains.organization.repository import OrganizationRepository
from domains.social.repository import ConnectionRepository
from shared.enums import ConnectionStatus
from shared.scheduled_jobs import job_response
from shared.schemas import OrganizationRef, UserRef
from shared.security import require_internal_token

router = APIRouter(
    prefix="/internal", tags=["internal"], include_in_schema=False,
    dependencies=[Depends(require_internal_token)])


class PresenceUpdate(BaseModel):
    online: bool


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
    schedule_events(background_tasks, service.pending_events)
    for avatar_url in result.removed_avatar_urls:
        background_tasks.add_task(
            delete_stored_avatar, avatar_url,
            Path(settings.AVATAR_STORAGE_DIR))
    return job_response(
        "purge-unverified", {
            "retention_days": settings.UNVERIFIED_ACCOUNT_RETENTION_DAYS,
            "candidates": result.candidates,
            "deleted": result.deleted,
            "skipped": result.skipped,
            "failed": result.failed
        }, steps={"accounts": (result.attempted, result.failed)})
