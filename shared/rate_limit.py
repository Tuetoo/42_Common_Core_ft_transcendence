from collections.abc import Sequence
import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from shared.config import BaseServiceSettings
from shared.exceptions import error_response
from shared.redis import get_rate_limit_client

logger = structlog.get_logger(__name__)


async def fixed_window_hits(
        settings: BaseServiceSettings, keys: Sequence[str],
        window: int) -> list[tuple[int, int]] | None:
    if not keys:
        return []
    try:
        client = get_rate_limit_client(settings)
        async with client.pipeline(transaction=True) as pipe:
            for key in keys:
                pipe.set(key, 0, ex=window, nx=True)
                pipe.incr(key)
                pipe.ttl(key)
            results = await pipe.execute()
    except Exception as exc:
        logger.warning(
            "rate_limit_unavailable", keys=len(keys), error=str(exc))
        return None
    hits: list[tuple[int, int]] = []
    for index in range(len(keys)):
        count = int(results[3 * index + 1])
        ttl = int(results[3 * index + 2])
        hits.append((count, ttl if ttl > 0 else window))
    return hits


def rate_limited_response(
        request: Request, *, code: str, message: str,
        retry_after: int) -> JSONResponse:
    return error_response(
        request, 429, code, message,
        headers={"Retry-After": str(max(1, int(retry_after)))})
