from uuid import UUID
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from shared.schemas.refs import OrganizationRef, UserRef
from database import get_db

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/users/{user_id}", response_model=UserRef)
async def get_user_ref(user_id: UUID, db: AsyncSession = Depends(get_db)
                       ) -> UserRef:
    # TODO
    # Real implementation: SELECT id, username, avatar_url, last_seen_at
    # FROM users WHERE id = :user_id, then compute is_online from
    # last_seen_at. Left as a stub here since domains/auth/model.py
    # (the real `User` model) is Backend Lead's own work, not part of
    # this alignment package.
    raise NotImplementedError


@router.get("/organizations/{org_id}", response_model=OrganizationRef)
async def get_organization_ref(
  org_id: UUID, db: AsyncSession = Depends(get_db)) -> OrganizationRef:
    # TODO
    raise NotImplementedError
