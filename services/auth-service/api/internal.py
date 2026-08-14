from uuid import UUID
from datetime import datetime, timedelta, UTC
from typing import Any
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings
from database import get_db
from domains.auth.repository import UserRepository
from domains.auth.service import default_avatar_url
from domains.organization.repository import OrganizationRepository
from domains.social.repository import ConnectionRepository
from shared.enums.connection import ConnectionStatus
from shared.exceptions import NotFoundError
from shared.schemas.refs import OrganizationRef, UserRef

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/users/{user_id}", response_model=UserRef)
async def get_user_ref(user_id: UUID, db: AsyncSession = Depends(get_db)
                       ) -> UserRef:
    users = UserRepository(db)
    user = await users.get_by_id(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    profile = await users.get_profile(user_id)
    is_online = False
    if user.last_seen_at is not None:
        threshold = timedelta(
            minutes=settings.PRESENCE_ONLINE_THRESHOLD_MINUTES)
        is_online = (datetime.now(UTC) - user.last_seen_at) < threshold
    return UserRef(
        id=user.id, username=user.username,
        avatar_url=(profile.avatar_url if profile else None)
        or default_avatar_url(user.username), is_online=is_online)


@router.get("/organizations/{org_id}", response_model=OrganizationRef)
async def get_organization_ref(
  org_id: UUID, db: AsyncSession = Depends(get_db)) -> OrganizationRef:
    orgs = OrganizationRepository(db)
    org = await orgs.get_by_id(org_id)
    if org is None:
        raise NotFoundError(f"Organization {org_id} not found.")
    return OrganizationRef(id=org.id, name=org.name)


@router.get("/users/{user_id}/organizations",
            response_model=list[OrganizationRef])
async def list_user_organizations(
    user_id: UUID, db: AsyncSession = Depends(get_db)
  ) -> list[OrganizationRef]:
    orgs = OrganizationRepository(db)
    organizations = await orgs.list_for_user(user_id)
    return [OrganizationRef(id=org.id, name=org.name) for org in organizations]


@router.get("/connections/check")
async def check_connection(
    user_a: UUID, user_b: UUID, db: AsyncSession = Depends(get_db)
  ) -> dict[str, Any]:
    connections = ConnectionRepository(db)
    connection = await connections.get_between(user_a, user_b)
    connected = (connection is not None
                 and connection.status == ConnectionStatus.ACCEPTED)
    return {"connected": connected}


@router.get("/users/{user_id}/notification-settings")
async def get_notification_settings(
    user_id: UUID, db: AsyncSession = Depends(get_db)
  ) -> dict[str, Any]:
    users = UserRepository(db)
    user = await users.get_by_id(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    settings_row = await users.get_settings(user_id)
    return {"email": user.email, "notification_preferences": (
        settings_row.notification_preferences if settings_row else None)}
