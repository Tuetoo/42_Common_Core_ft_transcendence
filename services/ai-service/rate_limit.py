from datetime import UTC, datetime, timedelta
import structlog
from config import settings
from fastapi import HTTPException
from shared.redis import fixed_window_hits

logger = structlog.get_logger(__name__)


class UserRateLimitedError(Exception):

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after

    def http_error(self) -> HTTPException:
        return HTTPException(
            status_code=429, detail=str(self),
            headers={"Retry-After": str(self.retry_after)})


async def _count(name: str, key: str, ttl_seconds: int) -> int | None:
    hits = await fixed_window_hits(settings, [key], ttl_seconds)
    if hits is None:
        logger.warning(f"{name}.rate_limit_unavailable", key=key)
        return None
    return hits[0][0]


async def check_user_rate_limit(
        name: str, user_id: str, *, per_minute: int, per_day: int,
        noun: str, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    minute_key = f"ai:{name}:rate:{user_id}:m:{now.strftime('%Y%m%d%H%M')}"
    day_key = f"ai:{name}:rate:{user_id}:d:{now.strftime('%Y%m%d')}"
    minute_count = await _count(name, minute_key, 120)
    if minute_count is None:
        return
    if minute_count > per_minute:
        raise UserRateLimitedError(
            f"Too many {noun}s this minute (limit {per_minute}).",
            retry_after=max(1, 60 - now.second))
    day_count = await _count(name, day_key, 2 * 86400)
    if day_count is None:
        return
    if day_count > per_day:
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        raise UserRateLimitedError(
            f"Daily {noun} limit reached (limit {per_day}).",
            retry_after=max(1, int((next_midnight - now).total_seconds())))
