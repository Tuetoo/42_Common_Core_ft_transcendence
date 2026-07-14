import httpx
from shared.exceptions import UpstreamServiceError
from shared.redis import get_cache_client
from shared.schemas.refs import AnalysisRef, RecommendationRef
from config import settings

_CACHE_TTL_SECONDS = 10


async def get_latest_recommendation(ticker: str) -> RecommendationRef | None:
    cache_key = f"core:ai_client:latest_recommendation: {ticker}"
    cache = get_cache_client(settings)
    cached = await cache.get(cache_key)
    if cached:
        return RecommendationRef.model_validate_jason(cached)
    url = f"{settings.AI_SERVICE_URL}/internal/recommendations/latest"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(url, params={"ticker": ticker})
    except httpx.HTTPError as exc:
        raise UpstreamServiceError("ai-service unreachable while resolving "
                                   f"recommendation for {ticker}") from exc
    if response.status_code == 404:
        return None
    response.raise_for_status()
    rec_ref = RecommendationRef.model_validate(response.json())
    await cache.set(cache_key, rec_ref.model_dump_json(),
                    ex=_CACHE_TTL_SECONDS)
    return rec_ref


async def get_analysis_ref(analysis_id: str) -> AnalysisRef:
    url = f"{settings.AI_SERVICE_URL}/iternal/analyses/{analysis_id}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError("ai-service unreachable while resolving "
                                   f"analysis {analysis_id}") from exc
    return AnalysisRef.model_validate(response.json())
