from decimal import Decimal
from uuid import UUID
import httpx
from shared.exceptions import UpstreamServiceError
from shared.http_client import get_http_client
from shared.redis import get_cache_client
from shared.schemas.refs import AnalysisRef, RecommendationRef
from config import settings

_CACHE_TTL_SECONDS = 10
_TIMEOUT = 3.0


async def get_latest_recommendation(ticker: str) -> RecommendationRef | None:
    cache_key = f"core:ai_client:latest_recommendation:{ticker}"
    cache = get_cache_client(settings)
    cached = await cache.get(cache_key)
    if cached:
        return RecommendationRef.model_validate_json(cached)
    url = f"{settings.AI_SERVICE_URL}/internal/recommendations/latest"
    try:
        response = await get_http_client(_TIMEOUT).get(
            url, params={"ticker": ticker})
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
    url = f"{settings.AI_SERVICE_URL}/internal/analyses/{analysis_id}"
    try:
        response = await get_http_client(_TIMEOUT).get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError("ai-service unreachable while resolving "
                                   f"analysis {analysis_id}") from exc
    return AnalysisRef.model_validate(response.json())


async def get_current_price(ticker: str) -> Decimal | None:
    url = (f"{settings.AI_SERVICE_URL}/api/v1/market/instruments/{ticker}/"
           "prices")
    try:
        response = await get_http_client(_TIMEOUT).get(
            url, params={"interval": "1d"})
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    bars = response.json().get("data") or []
    if not bars:
        return None
    return Decimal(str(bars[0]["close"]))


async def request_analysis(ticker: str, portfolio_id: UUID | None = None
                           ) -> AnalysisRef | None:
    url = f"{settings.AI_SERVICE_URL}/api/v1/analyses"
    payload = {"ticker": ticker, "portfolio_id": str(portfolio_id)
               if portfolio_id else None}
    try:
        response = await get_http_client(15.0).post(url, json=payload)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return AnalysisRef.model_validate(response.json().get(
        "data", response.json()))
