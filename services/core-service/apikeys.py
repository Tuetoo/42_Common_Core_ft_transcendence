import contextlib
import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
import structlog
from config import settings
from database import SessionLocal
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import DateTime, Integer, String, func, select, text
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from starlette.middleware.base import BaseHTTPMiddleware
from shared.database import Base, UUIDPKMixin
from shared.exceptions import error_response
from shared.permissions import Permission, has_permission
from shared.redis import (fixed_window_hits, get_cache_client,
                          get_rate_limit_client, rate_limited_response)
from shared.user_state import get_user_state

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

_PROTECTED_PREFIX = f"{settings.API_V1_PREFIX}/public"

_LAST_USED_UPDATE_INTERVAL_SECONDS = 60
_WINDOW_SECONDS = 60


def _invalid_key_response(request: Request) -> JSONResponse:
    return error_response(
        request, 401, "invalid_api_key", "Invalid or revoked API key")


_READ_METHODS = frozenset({"GET", "HEAD"})


async def _owner_access_error(
        request: Request, owner_user_id: str,
        method: str) -> JSONResponse | None:
    if not settings.USER_STATE_CHECK_ENABLED:
        return None
    state = await get_user_state(settings, owner_user_id)
    if state is None:
        logger.warning(
            "public_api.owner_state_unknown", owner_user_id=owner_user_id)
        return None
    if not state.is_active:
        return error_response(
            request, 401, "account_deactivated",
            "The owner of this API key is deactivated")
    if method not in _READ_METHODS and not has_permission(
            state.role, Permission.PORTFOLIO_WRITE):
        return error_response(
            request, 403, "insufficient_permissions",
            "This API key is read-only")
    return None


class PublicAPIRateLimitMiddleware(BaseHTTPMiddleware):

    async def dispatch(
            self, request: Request,
            call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not request.url.path.startswith(_PROTECTED_PREFIX):
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)

        raw_key = request.headers.get("X-API-Key")
        if not raw_key:
            return error_response(
                request, 401, "missing_api_key", "Missing X-API-Key")

        key_hash = hash_key(raw_key)
        cache = get_cache_client(settings)
        cache_key = validation_cache_key(key_hash)
        try:
            cached = await cache.get(cache_key)
        except Exception as exc:
            logger.warning("public_api.key_cache_unavailable", error=str(exc))
            cached = None

        if cached is not None:
            info = json.loads(cached)
        else:
            async with SessionLocal() as session:
                api_key = await get_by_raw_key(session, raw_key)
                if api_key is None:
                    return _invalid_key_response(request)
                info = {
                    "id": str(api_key.id),
                    "owner_user_id": str(api_key.owner_user_id),
                    "rate_limit_per_minute": api_key.rate_limit_per_minute,
                    "revoked": api_key.revoked_at is not None
                }
                with contextlib.suppress(Exception):
                    await cache.set(
                        cache_key, json.dumps(info), ex=KEY_CACHE_TTL_SECONDS)

        if info.get("revoked"):
            return _invalid_key_response(request)
        access_error = await _owner_access_error(
            request, str(info["owner_user_id"]), request.method)
        if access_error is not None:
            return access_error

        server_max = settings.PUBLIC_API_RATE_LIMIT_PER_MINUTE
        key_limit = min(int(info["rate_limit_per_minute"]), server_max)
        hits = await fixed_window_hits(
            settings, [
                f"ratelimit:{info['id']}",
                f"ratelimit:owner:{info['owner_user_id']}"
            ], _WINDOW_SECONDS)
        if hits is not None:
            (key_count, key_retry), (owner_count, owner_retry) = hits
            if key_count > key_limit or owner_count > server_max:
                return rate_limited_response(
                    request, code="rate_limit_exceeded",
                    message="Too many requests", retry_after=max(
                        key_retry if key_count > key_limit else 0,
                        owner_retry if owner_count > server_max else 0))

        touch_gate_key = f"apikey:last_used_gate:{info['id']}"
        try:
            should_touch = await get_rate_limit_client(settings).set(
                touch_gate_key, "1", ex=_LAST_USED_UPDATE_INTERVAL_SECONDS,
                nx=True)
        except Exception:
            should_touch = False
        if should_touch:
            async with SessionLocal() as session:
                await session.execute(
                    sql_update(ApiKey).where(
                        ApiKey.id == UUID(str(info["id"]))).values(
                            last_used_at=datetime.now(UTC)))
                await session.commit()

        request.state.api_key_owner_user_id = UUID(info["owner_user_id"])

        return await call_next(request)


def register_middleware(app: FastAPI) -> None:
    app.add_middleware(PublicAPIRateLimitMiddleware)
