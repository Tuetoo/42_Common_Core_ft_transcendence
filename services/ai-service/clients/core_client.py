import json
import httpx
from shared.exceptions import UpstreamServiceError
from shared.redis import get_cache_client
from config import settings

_CACHE_TTL_SECONDS = 30


async def get_portfolio_holdings(portfolio_id: str) -> list[dict]:
    cache_key = f"ai:core_client:holdings:{portfolio_id}"
    cache = get_cache_client(settings)
    cached = await cache.get(cache_key)
    if cached:
        return json.loads(cached)
    url = (f"{settings.CORE_SERVICE_URL}/internal/portfolios/{portfolio_id}"
           "/holdings")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError("core-service unreachable while resolving "
                                   f"holdings for {portfolio_id}") from exc
    holdings = response.json()
    await cache.set(cache_key, json.dumps(holdings), ex=_CACHE_TTL_SECONDS)
    return holdings
