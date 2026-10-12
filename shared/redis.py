from collections.abc import Sequence
from urllib.parse import urlsplit, urlunsplit
import redis.asyncio as redis
import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from shared.config import BaseServiceSettings
from shared.exceptions import error_response

logger = structlog.get_logger(__name__)

_pools: dict[tuple[str, int], redis.Redis] = {}


def _get_client(settings: BaseServiceSettings, db_index: int) -> redis.Redis:
    base_url = settings.assemble_redis_url()
    parsed = urlsplit(base_url)
    url_without_db = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    key = (url_without_db, db_index)
    if key not in _pools:
        _pools[key] = redis.from_url(
            url_without_db, db=db_index, decode_responses=True,
            max_connections=20, socket_timeout=15, socket_connect_timeout=15)
    return _pools[key]


def get_cache_client(settings: BaseServiceSettings) -> redis.Redis:
    return _get_client(settings, settings.REDIS_DB_CACHE)


def get_rate_limit_client(settings: BaseServiceSettings) -> redis.Redis:
    return _get_client(settings, settings.REDIS_DB_RATE_LIMIT)


def get_events_client(settings: BaseServiceSettings) -> redis.Redis:
    return _get_client(settings, settings.REDIS_DB_EVENTS)


def get_queue_client(settings: BaseServiceSettings) -> redis.Redis:
    return _get_client(settings, settings.REDIS_DB_QUEUE)


async def check_redis_connection(settings: BaseServiceSettings) -> bool:
    try:
        await get_cache_client(settings).ping()
        return True
    except Exception:
        return False


async def close_redis_connections() -> None:
    for client in _pools.values():
        await client.aclose()
    _pools.clear()


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


E2E_LOCK_KEY = "e2e:run_lock"
E2E_LOCK_TTL_SECONDS = 45 * 60

_E2E_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


async def acquire_e2e_lock(
        settings: BaseServiceSettings, holder: str, *,
        break_lock: bool = False) -> str | None:
    client = get_cache_client(settings)
    if break_lock:
        await client.delete(E2E_LOCK_KEY)
    if await client.set(
            E2E_LOCK_KEY, holder, nx=True, ex=E2E_LOCK_TTL_SECONDS):
        return None
    current = await client.get(E2E_LOCK_KEY)
    return (
        str(current) if current is not None else "(expired meanwhile, retry)")


async def release_e2e_lock(settings: BaseServiceSettings, holder: str) -> None:
    client = get_cache_client(settings)
    await client.eval(_E2E_RELEASE_SCRIPT, 1, E2E_LOCK_KEY, holder)
