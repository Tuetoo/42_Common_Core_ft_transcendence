import json
import time
from uuid import UUID
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from apikeys import get_by_raw_key, hash_key
from config import settings
from database import SessionLocal
from shared.redis import get_cache_client, get_rate_limit_client

_PROTECTED_PREFIX = f"{settings.API_V1_PREFIX}/public"
_KEY_CACHE_TTL_SECONDS = 30
_LAST_USED_UPDATE_INTERVAL_SECONDS = 60


class PublicAPIRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request,
                       call_next: Callable[[Request], Awaitable[Response]]
                       ) -> Response:
        if not request.url.path.startswith(_PROTECTED_PREFIX):
            return await call_next(request)
        raw_key = request.headers.get("X-API-Key")
        if not raw_key:
            return JSONResponse(status_code=401, content={
                "error": {"code": "missing_api_key",
                          "message": "Missing X-API-Key"}})
        key_hash = hash_key(raw_key)
        cache = get_cache_client(settings)
        cache_key = f"apikey:valid:{key_hash}"
        cached = await cache.get(cache_key)
        if cached is not None:
            info = json.loads(cached)
        else:
            async with SessionLocal() as session:
                api_key = await get_by_raw_key(session, raw_key)
                if api_key is None:
                    return JSONResponse(status_code=401, content={
                        "error": {"code": "invalid_api_key",
                                  "message": "Invalid or revoked API key"}})
                info = {"id": str(api_key.id), "owner_user_id": (str(
                    api_key.owner_user_id) if api_key.owner_user_id else None),
                    "owner_org_id": (str(api_key.owner_org_id)
                                     if api_key.owner_org_id else None),
                    "rate_limit_per_minute": api_key.rate_limit_per_minute,
                    "revoked": api_key.revoked_at is not None}
                await cache.set(cache_key, json.dumps(info),
                                ex=_KEY_CACHE_TTL_SECONDS)
        if info["revoked"]:
            return JSONResponse(status_code=401, content={
                "error": {"code": "invalid_api_key",
                          "message": "Invalid or revoked API key"}})
        redis_client = get_rate_limit_client(settings)
        window_key = f"ratelimit:{info['id']}:{int(time.time() // 60)}"
        current = await redis_client.incr(window_key)
        if current == 1:
            await redis_client.expire(window_key, 60)
        if current > info["rate_limit_per_minute"]:
            return JSONResponse(status_code=429, content={
                "error": {"code": "rate_limit_exceeded",
                          "message": "Too many requests"}})
        touch_gate_key = f"apikey:last_used_gate:{info['id']}"
        should_touch = await redis_client.set(
            touch_gate_key, "1", ex=_LAST_USED_UPDATE_INTERVAL_SECONDS,
            nx=True)
        if should_touch:
            async with SessionLocal() as session:
                api_key = await get_by_raw_key(session, raw_key)
                if api_key is not None:
                    api_key.last_used_at = datetime.now(UTC)
                    await session.commit()
        request.state.api_key_owner_user_id = (
            UUID(info["owner_user_id"]) if info["owner_user_id"] else None)
        request.state.api_key_owner_org_id = (
            UUID(info["owner_org_id"]) if info["owner_org_id"] else None)
        return await call_next(request)


def register_middleware(app: FastAPI) -> None:
    app.add_middleware(PublicAPIRateLimitMiddleware)
