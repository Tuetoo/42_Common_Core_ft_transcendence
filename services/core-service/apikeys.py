import hashlib
import secrets
from uuid import UUID
from datetime import UTC, datetime
from typing import Any
from sqlalchemy import CheckConstraint, DateTime, Integer, String, select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from shared.database import Base
from shared.models import UUIDPKMixin

# AlphaMind Key
_KEY_PREFIX = "amk_"


class ApiKey(Base, UUIDPKMixin):
    __tablename__ = "api_keys"
    __table_args__ = (CheckConstraint(
        "owner_user_id IS NOT NULL OR owner_org_id IS NOT NULL",
        name="ck_api_keys_has_owner"))
    owner_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True)
    owner_org_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    scopes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False)


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def create_api_key(
        db: AsyncSession, *, owner_user_id: UUID | None,
        owner_org_id: UUID | None, name: str,
        scopes: dict[str, Any] | None = None, rate_limit_per_minute: int = 60
        ) -> tuple[ApiKey, str]:
    raw_key = _KEY_PREFIX + secrets.token_urlsafe(32)
    row = ApiKey(owner_user_id=owner_user_id, owner_org_id=owner_org_id,
                 name=name, key_hash=hash_key(raw_key), scopes=scopes,
                 rate_limit_per_minute=rate_limit_per_minute)
    db.add(row)
    await db.flush()
    return row, raw_key


async def get_by_raw_key(db: AsyncSession, raw_key: str) -> ApiKey | None:
    result = await db.execute(select(ApiKey)
                              .where(ApiKey.key_hash == hash_key(raw_key)))
    return result.scalar_one_or_none()


async def list_for_owner(db: AsyncSession, owner_user_id: UUID
                         ) -> list[ApiKey]:
    result = await db.execute(select(ApiKey)
                              .where(ApiKey.owner_user_id == owner_user_id))
    return list(result.scalars().all())


async def revoke(db: AsyncSession, api_key: ApiKey) -> None:
    api_key.revoked_at = datetime.now(UTC)
    await db.flush()
