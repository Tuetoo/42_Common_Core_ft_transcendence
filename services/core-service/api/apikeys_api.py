from uuid import UUID
from apikeys import (ApiKey, count_active_for_owner, create_api_key,
                     forget_cached_key, get_for_owner, list_for_owner,
                     mark_revoked_in_cache, revoke, update)
from config import settings
from database import get_db
from deps import get_current_user
from fastapi import APIRouter, BackgroundTasks, Depends, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from domains.notification.service import notify_after_commit
from shared.enums import NotificationType
from shared.exceptions import ConflictError, NotFoundError
from shared.permissions import Permission
from shared.schemas import APIResponse, BaseSchema, IDMixin
from shared.security import CurrentUser, require_permission

router = APIRouter(prefix="/api-keys", tags=["api-keys"])

api_key_manager = require_permission(
    Permission.API_KEY_MANAGE)(get_current_user)


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    rate_limit_per_minute: int = Field(
        default=settings.PUBLIC_API_RATE_LIMIT_PER_MINUTE, ge=1,
        le=settings.PUBLIC_API_RATE_LIMIT_PER_MINUTE)


class ApiKeyUpdate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Body of `PATCH /api/v1/api-keys/{id}`; at least one field is "
                "required.")
        })

    name: str | None = Field(default=None, min_length=1, max_length=128)
    rate_limit_per_minute: int | None = Field(
        default=None, ge=1, le=settings.PUBLIC_API_RATE_LIMIT_PER_MINUTE)

    @model_validator(mode="after")
    def _not_empty(self) -> "ApiKeyUpdate":
        if self.name is None and self.rate_limit_per_minute is None:
            raise ValueError("Provide name and/or rate_limit_per_minute.")
        return self


class ApiKeyRead(BaseSchema, IDMixin):
    name: str
    rate_limit_per_minute: int
    last_used_at: str | None = None
    revoked_at: str | None = None


class ApiKeyCreated(ApiKeyRead):
    key: str


@router.post(
    "", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[ApiKeyCreated])
async def create_key(
    data: ApiKeyCreate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(api_key_manager),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ApiKeyCreated]:
    active = await count_active_for_owner(db, current_user.user_id, lock=True)
    if active >= settings.MAX_ACTIVE_API_KEYS_PER_USER:
        raise ConflictError(
            f"You already have {active} active API keys (maximum "
            f"{settings.MAX_ACTIVE_API_KEYS_PER_USER}). Revoke one first.")
    row, raw_key = await create_api_key(
        db, owner_user_id=current_user.user_id, name=data.name,
        rate_limit_per_minute=data.rate_limit_per_minute)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.API_KEY_CREATED, title="API key created",
        content=f"API key '{row.name}' was created.")
    return APIResponse(
        data=ApiKeyCreated(
            id=row.id, name=row.name,
            rate_limit_per_minute=row.rate_limit_per_minute, last_used_at=None,
            revoked_at=None, key=raw_key))


def _to_read(row: ApiKey) -> ApiKeyRead:
    return ApiKeyRead(
        id=row.id, name=row.name,
        rate_limit_per_minute=row.rate_limit_per_minute, last_used_at=(
            row.last_used_at.isoformat() if row.last_used_at else None),
        revoked_at=row.revoked_at.isoformat() if row.revoked_at else None)


async def _own_key(
        db: AsyncSession, user: CurrentUser, key_id: UUID) -> ApiKey:
    match = await get_for_owner(db, user.user_id, key_id)
    if match is None:
        raise NotFoundError("API key not found.")
    return match


@router.get("", response_model=APIResponse[list[ApiKeyRead]])
async def list_keys(
        current_user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[list[ApiKeyRead]]:
    rows = await list_for_owner(db, current_user.user_id)
    return APIResponse(data=[_to_read(row) for row in rows])


@router.patch(
    "/{key_id}", response_model=APIResponse[ApiKeyRead], description=(
        "Rename an API key or change its per-key rate limit (capped by the "
        "server"
        " setting). A revoked key cannot be changed (409)."))
async def update_key(
    key_id: UUID, data: ApiKeyUpdate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(api_key_manager),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[ApiKeyRead]:
    match = await _own_key(db, current_user, key_id)
    if match.revoked_at is not None:
        raise ConflictError("This API key is revoked.")
    await update(
        db, match, name=data.name,
        rate_limit_per_minute=data.rate_limit_per_minute)
    background_tasks.add_task(forget_cached_key, match.id, match.key_hash)
    notify_after_commit(
        background_tasks, [current_user.user_id],
        type=NotificationType.API_KEY_UPDATED, title="API key updated",
        content=f"API key '{match.name}' was updated.")
    return APIResponse(data=_to_read(match))


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    key_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(api_key_manager),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    match = await _own_key(db, current_user, key_id)
    already_revoked = match.revoked_at is not None
    await revoke(db, match)
    background_tasks.add_task(mark_revoked_in_cache, match.id, match.key_hash)
    if not already_revoked:
        notify_after_commit(
            background_tasks, [current_user.user_id],
            type=NotificationType.API_KEY_REVOKED, title="API key revoked",
            content=f"API key '{match.name}' was revoked.")
