import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
import httpx
import structlog
from config import settings
from shared.exceptions import UpstreamServiceError
from shared.http_client import get_http_client
from shared.market_calendar import MARKET_TZ
from shared.redis import get_cache_client
from shared.schemas import AnalysisRef, RecommendationRef

logger = structlog.get_logger(__name__)

_CACHE_TTL_SECONDS = 10
_TIMEOUT = 3.0
_CLOSES_TIMEOUT = 15.0
_SENTIMENT_TIMEOUT = 270.0
_LABELS = frozenset({"positive", "neutral", "negative"})


async def get_latest_recommendation(
        ticker: str, portfolio_id: UUID | str) -> RecommendationRef | None:
    cache_key = f"core:ai_client:latest_recommendation:{portfolio_id}:{ticker}"
    cache = get_cache_client(settings)
    try:
        cached = await cache.get(cache_key)
    except Exception as exc:
        logger.warning(
            "latest_recommendation_cache_read_failed", error=str(exc))
        cached = None
    if cached:
        return RecommendationRef.model_validate_json(cached)
    url = f"{settings.AI_SERVICE_URL}/internal/recommendations/latest"
    try:
        response = await get_http_client(_TIMEOUT).get(
            url, params={
                "ticker": ticker,
                "portfolio_id": str(portfolio_id)
            })
    except httpx.HTTPError as exc:
        raise UpstreamServiceError(
            f"ai-service unreachable while resolving recommendation for "
            f"{ticker}") from exc
    if response.status_code == 404:
        return None
    response.raise_for_status()
    rec_ref = RecommendationRef.model_validate(response.json())
    try:
        await cache.set(
            cache_key, rec_ref.model_dump_json(), ex=_CACHE_TTL_SECONDS)
    except Exception as exc:
        logger.warning(
            "latest_recommendation_cache_write_failed", error=str(exc))
    return rec_ref


async def get_analysis_ref(analysis_id: str) -> AnalysisRef:
    url = f"{settings.AI_SERVICE_URL}/internal/analyses/{analysis_id}"
    try:
        response = await get_http_client(_TIMEOUT).get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError(
            f"ai-service unreachable while resolving analysis {analysis_id}"
        ) from exc
    return AnalysisRef.model_validate(response.json())


def bar_session_date(timestamp: str) -> date:
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(MARKET_TZ).date()


async def get_daily_closes(
        ticker: str, *, lookback_days: int = 5,
        timeout: float = _CLOSES_TIMEOUT) -> dict[date, Decimal] | None:
    url = (
        f"{settings.AI_SERVICE_URL}/internal/market/instruments/{ticker}"
        f"/prices")
    try:
        response = await get_http_client(timeout).get(
            url, params={
                "interval": "1d",
                "lookback_days": lookback_days
            })
        response.raise_for_status()
        bars = response.json().get("data") or []
    except (httpx.HTTPError, ValueError):
        return None
    closes: dict[date, Decimal] = {}
    for bar in bars:
        try:
            session = bar_session_date(str(bar["timestamp"]))
            close = Decimal(str(bar["close"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        if close.is_finite() and close > 0:
            closes[session] = close
    return closes or None


async def get_fx_rate(from_currency: str, to_currency: str) -> Decimal | None:
    if from_currency.upper() == to_currency.upper():
        return Decimal(1)
    url = f"{settings.AI_SERVICE_URL}/internal/fx-rate"
    try:
        response = await get_http_client(_TIMEOUT).get(
            url, params={
                "from_currency": from_currency,
                "to_currency": to_currency
            })
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    rate = response.json().get("rate")
    return Decimal(str(rate)) if rate is not None else None


async def request_analysis(
        ticker: str, portfolio_id: UUID | None = None, *,
        user_id: UUID | None = None) -> AnalysisRef | None:
    url = f"{settings.AI_SERVICE_URL}/internal/analyses"
    payload: dict[str, str | None] = {
        "ticker": ticker,
        "portfolio_id": str(portfolio_id) if portfolio_id else None
    }
    if user_id is not None:
        payload["user_id"] = str(user_id)
    try:
        response = await get_http_client(15.0).post(url, json=payload)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return AnalysisRef.model_validate(
        response.json().get("data", response.json()))


SentimentKind = Literal["note", "comment"]


@dataclass(frozen=True)
class SentimentResult:
    label: str
    score: float
    model: str


async def analyze_sentiment(
        text: str, kind: SentimentKind) -> SentimentResult | None:
    if not text.strip():
        return None
    url = f"{settings.AI_SERVICE_URL}/internal/sentiment"
    try:
        response = await get_http_client(_SENTIMENT_TIMEOUT).post(
            url, json={
                "text": text,
                "kind": kind
            })
        if response.status_code != 200:
            logger.info(
                "sentiment_request_rejected", kind=kind,
                status_code=response.status_code)
            return None
        data = response.json()
        label = str(data["label"]).lower()
        score = float(data["score"])
        model = str(data.get("model") or "")
    except Exception as exc:
        logger.warning(
            "sentiment_request_failed", kind=kind,
            error=str(exc)[:200] or type(exc).__name__)
        return None
    if (label not in _LABELS or not math.isfinite(score)
            or not -1.0 <= score <= 1.0):
        logger.warning("sentiment_response_invalid", kind=kind, label=label)
        return None
    return SentimentResult(label=label, score=score, model=model[:128])
