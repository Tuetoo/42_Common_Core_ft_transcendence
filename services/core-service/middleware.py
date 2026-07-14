import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from shared.redis import get_rate_limit_client
from config import settings

_PROTECTED_PREFIX = f"{settings.API_V1_PREFIX}/public"


class PublicAPIRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(_PROTECTED_PREFIX):
            return await call_next(request)
        api_key = request.headers.get("X-API-Key")
        if not api_key:
            return JSONResponse(status_code=401, content={
                "error": {"message": "Missing X-API-Key"}})
        redis_client = get_rate_limit_client(settings)
        window_key = f"ratelimit:{api_key}:{int(time.time() // 60)}"
        current = await redis_client.incr(window_key)
        if current == 1:
            await redis_client.expire(window_key, 60)
        if current > settings.PUBLIC_API_RATE_LIMIT_PER_MINUTE:
            return JSONResponse(status_code=429, content={
                "error": {"code": "rate_limit_exceeded",
                          "message": "Too many requests"}})
        return await call_next(request)


def register_middleware(app: FastAPI) -> None:
    app.add_middleware(PublicAPIRateLimitMiddleware)
