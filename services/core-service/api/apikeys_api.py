from uuid import UUID
from apikeys import create_api_key, list_for_owner, revoke
from database import get_db
from deps import get_current_user
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from shared.exceptions import NotFoundError
from shared.schemas.base import APIResponse, BaseSchema, IDMixin
from shared.security import CurrentUser

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=1000)


class ApiKeyRead(BaseSchema, IDMixin):
    name: str
    rate_limit_per_minute: int
    last_used_at: str | None = None
    revoked_at: str | None = None


class ApiKeyCreated(ApiKeyRead):
    key: str


@router.post("", status_code=status.HTTP_201_CREATED,
             response_model=APIResponse[ApiKeyCreated])
async def create_key(
    data: ApiKeyCreate, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[ApiKeyCreated]:
    row, raw_key = await create_api_key(
        db, owner_user_id=current_user.user_id, owner_org_id=None,
        name=data.name, rate_limit_per_minute=data.rate_limit_per_minute)
    return APIResponse(data=ApiKeyCreated(
        id=row.id, name=row.name,
        rate_limit_per_minute=row.rate_limit_per_minute, last_used_at=None,
        revoked_at=None, key=raw_key))


@router.get("", response_model=APIResponse[list[ApiKeyRead]])
async def list_keys(current_user: CurrentUser = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)
                    ) -> APIResponse[list[ApiKeyRead]]:
    rows = await list_for_owner(db, current_user.user_id)
    return APIResponse(data=[ApiKeyRead(
        id=row.id, name=row.name,
        rate_limit_per_minute=row.rate_limit_per_minute,
        last_used_at=row.last_used_at.isoformat()
        if row.last_used_at else None, revoked_at=row.revoked_at.isoformat()
        if row.revoked_at else None)
        for row in rows])


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    key_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> None:
    rows = await list_for_owner(db, current_user.user_id)
    match = next((row for row in rows if row.id == key_id), None)
    if match is None:
        raise NotFoundError("API key not found.")
    await revoke(db, match)
