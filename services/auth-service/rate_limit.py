import hashlib
import structlog
from config import settings
from fastapi import Request
from fastapi.responses import JSONResponse
from shared.exceptions import RateLimitExceededError
from shared.rate_limit import fixed_window_hits, rate_limited_response
from shared.redis import get_rate_limit_client

logger = structlog.get_logger(__name__)

_PREFIX = "auth:rl"


class RateLimitedError(RateLimitExceededError):
    default_message = "Too many attempts. Please try again later."
    code = "rate_limited"

    def __init__(
            self, message: str | None = None, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


def _key(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts[1:]).encode()).hexdigest()[:32]
    return f"{_PREFIX}:{parts[0]}:{digest}"


async def hit(
        scope: str, identity: str | None, *, limit: int, window: int) -> None:
    if not settings.RATE_LIMIT_ENABLED or not identity:
        return
    hits = await fixed_window_hits(settings, [_key(scope, identity)], window)
    if hits is None:
        return
    count, retry_after = hits[0]
    if count > limit:
        raise RateLimitedError(retry_after=retry_after)


async def ensure_not_locked(scope: str, identity: str) -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return
    try:
        ttl = await get_rate_limit_client(settings).ttl(
            _key(f"{scope}:lock", identity))
    except Exception as exc:
        logger.warning("rate_limit_unavailable", scope=scope, error=str(exc))
        return
    if int(ttl) > 0:
        raise RateLimitedError(
            "Too many invalid codes. Please try again later.",
            retry_after=int(ttl))


async def record_failure(
        scope: str, identity: str, *, limit: int, window: int,
        lock_seconds: int) -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return
    key = _key(f"{scope}:fail", identity)
    hits = await fixed_window_hits(settings, [key], window)
    if hits is None or hits[0][0] < limit:
        return
    try:
        client = get_rate_limit_client(settings)
        await client.set(_key(f"{scope}:lock", identity), 1, ex=lock_seconds)
        await client.delete(key)
    except Exception as exc:
        logger.warning("rate_limit_unavailable", scope=scope, error=str(exc))


async def two_factor_guard(user_id: str) -> None:
    await ensure_not_locked("2fa", user_id)


async def two_factor_failed(user_id: str) -> None:
    await record_failure(
        "2fa", user_id, limit=settings.RATE_LIMIT_2FA_FAILURES,
        window=settings.RATE_LIMIT_2FA_WINDOW_SECONDS,
        lock_seconds=settings.RATE_LIMIT_2FA_LOCK_SECONDS)


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def rate_limited_handler(
        request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RateLimitedError)
    return rate_limited_response(
        request, code=exc.code, message=exc.message,
        retry_after=exc.retry_after)
