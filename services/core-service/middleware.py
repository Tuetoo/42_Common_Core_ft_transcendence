import contextlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID
import structlog
from apikeys import (KEY_CACHE_TTL_SECONDS, ApiKey, get_by_raw_key, hash_key,
                     validation_cache_key)
from config import settings
from database import SessionLocal
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import update
from starlette.middleware.base import BaseHTTPMiddleware
from shared.exceptions import error_response
from shared.permissions import Permission, has_permission
from shared.rate_limit import fixed_window_hits, rate_limited_response
from shared.redis import get_cache_client, get_rate_limit_client
from shared.user_state import get_user_state

logger = structlog.get_logger(__name__)

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
                    update(ApiKey).where(
                        ApiKey.id == UUID(str(info["id"]))).values(
                            last_used_at=datetime.now(UTC)))
                await session.commit()

        request.state.api_key_owner_user_id = UUID(info["owner_user_id"])

        return await call_next(request)


def register_middleware(app: FastAPI) -> None:
    app.add_middleware(PublicAPIRateLimitMiddleware)
