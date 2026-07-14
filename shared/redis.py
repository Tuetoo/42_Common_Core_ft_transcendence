# import json
# from collections.abc import AsyncGenerator
import redis.asyncio as redis
from shared.config import BaseServiceSettings

_pools: dict[int, redis.Redis] = {}


def _get_client(settings: BaseServiceSettings, db_index: int) -> redis.Redis:
    key = (settings.assemble_redis_url(), db_index)
    if key not in _pools:
        _pools[key] = redis.from_url(
            settings.assemble_redis_url(), db=db_index, decode_responses=True,
            max_connections=20)
        return _pools[key]


def get_cache_client(settings: BaseServiceSettings) -> redis.Redis:
    return _get_client(settings, settings.REDIS_DB_CACHE)


def get_rate_limit_client(settings: BaseServiceSettings) -> redis.Redis:
    return _get_client(settings, settings.REDIS_DB_RATE_LIMIT)


def get_events_client(settings: BaseServiceSettings) -> redis.Redis:
    return _get_client(settings, settings.REDIS_DB_EVENTS)


def get_queue_client(settings: BaseServiceSettings) -> redis.Redis:
    return _get_client(settings, settings.REDIS_DB_QUEUE)


# async def get_redis() -> AsyncGenerator[redis.Redis, None]:
#     yield get_cache_client()


# async def cache_get_json(key: str) -> dict | None:
#     client = get_cache_client()
#     value = await client.get(key)
#     return json.loads(value) if value else None


# async def cache_set_json(key: str, value: str, ttl_seconds: int = 3000
#                          ) -> None:
#     client = get_cache_client()
#     await client.set(key, json.dumps(value), ex=ttl_seconds)


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
