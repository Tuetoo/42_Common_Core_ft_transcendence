import hashlib
import json
import secrets
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
import structlog
from config import settings
from sqlalchemy import DateTime, Integer, String, func, select, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from shared.database import Base, UUIDPKMixin
from shared.redis import get_cache_client

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "amk_"
KEY_CACHE_TTL_SECONDS = 30


def validation_cache_key(key_hash: str) -> str:
    return f"apikey:valid:{key_hash}"


class ApiKey(Base, UUIDPKMixin):
    __tablename__ = "api_keys"

    owner_user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def create_api_key(
        db: AsyncSession, *, owner_user_id: UUID, name: str,
        rate_limit_per_minute: int | None = None) -> tuple[ApiKey, str]:
    raw_key = _KEY_PREFIX + secrets.token_urlsafe(32)
    row = ApiKey(
        owner_user_id=owner_user_id, name=name, key_hash=hash_key(raw_key),
        rate_limit_per_minute=(
            rate_limit_per_minute if rate_limit_per_minute is not None else
            settings.PUBLIC_API_RATE_LIMIT_PER_MINUTE))
    db.add(row)
    await db.flush()
    return row, raw_key


async def get_by_raw_key(db: AsyncSession, raw_key: str) -> ApiKey | None:
    result = await db.execute(
        select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key)))
    return result.scalar_one_or_none()


async def get_for_owner(
        db: AsyncSession, owner_user_id: UUID, key_id: UUID) -> ApiKey | None:
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.id == key_id, ApiKey.owner_user_id == owner_user_id))
    return result.scalar_one_or_none()


async def list_for_owner(db: AsyncSession,
                         owner_user_id: UUID) -> list[ApiKey]:
    result = await db.execute(
        select(ApiKey).where(ApiKey.owner_user_id == owner_user_id))
    return list(result.scalars().all())


async def count_active_for_owner(
        db: AsyncSession, owner_user_id: UUID, *, lock: bool = False) -> int:
    if lock:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:owner))"),
            {"owner": f"apikeys:{owner_user_id}"})
    result = await db.execute(
        select(func.count()).select_from(ApiKey).where(
            ApiKey.owner_user_id == owner_user_id,
            ApiKey.revoked_at.is_(None)))
    return int(result.scalar_one())


async def update(
        db: AsyncSession, api_key: ApiKey, *, name: str | None = None,
        rate_limit_per_minute: int | None = None) -> None:
    if name is not None:
        api_key.name = name
    if rate_limit_per_minute is not None:
        api_key.rate_limit_per_minute = rate_limit_per_minute
    await db.flush()
    await forget_cached_key(api_key.id, api_key.key_hash)


async def revoke(db: AsyncSession, api_key: ApiKey) -> None:
    if api_key.revoked_at is not None:
        return
    api_key.revoked_at = datetime.now(UTC)
    await db.flush()
    await mark_revoked_in_cache(api_key.id, api_key.key_hash)


async def forget_cached_key(key_id: UUID, key_hash: str) -> None:
    await _write_cache_safely(
        "delete", key_id,
        get_cache_client(settings).delete(validation_cache_key(key_hash)))


async def mark_revoked_in_cache(key_id: UUID, key_hash: str) -> None:
    await _write_cache_safely(
        "revoke", key_id,
        get_cache_client(settings).set(
            validation_cache_key(key_hash),
            json.dumps({
                "id": str(key_id),
                "revoked": True
            }), ex=KEY_CACHE_TTL_SECONDS))


async def _write_cache_safely(
        action: str, key_id: UUID, write: Awaitable[Any]) -> None:
    try:
        await write
    except Exception as exc:
        logger.warning(
            "api_key_cache_write_failed", action=action,
            api_key_id=str(key_id), error=str(exc))
