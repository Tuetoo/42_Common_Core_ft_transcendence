from urllib.parse import urlsplit, urlunsplit
import redis.asyncio as redis
from shared.config import BaseServiceSettings

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
