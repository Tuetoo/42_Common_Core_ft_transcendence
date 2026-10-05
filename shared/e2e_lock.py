from shared.config import BaseServiceSettings
from shared.redis import get_cache_client

LOCK_KEY = "e2e:run_lock"
LOCK_TTL_SECONDS = 45 * 60

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


async def acquire(
        settings: BaseServiceSettings, holder: str, *,
        break_lock: bool = False) -> str | None:
    client = get_cache_client(settings)
    if break_lock:
        await client.delete(LOCK_KEY)
    if await client.set(LOCK_KEY, holder, nx=True, ex=LOCK_TTL_SECONDS):
        return None
    current = await client.get(LOCK_KEY)
    return (
        str(current) if current is not None else "(expired meanwhile, retry)")


async def release(settings: BaseServiceSettings, holder: str) -> None:
    client = get_cache_client(settings)
    await client.eval(_RELEASE_SCRIPT, 1, LOCK_KEY, holder)
