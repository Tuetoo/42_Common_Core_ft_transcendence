from datetime import datetime
from email.utils import parsedate_to_datetime
import structlog
from ai.providers.base import (ProviderRequestError, ProviderUnavailableError,
                               WebSearchProvider, WebSearchResult)
from config import settings
from shared.http_client import get_http_client

logger = structlog.get_logger(__name__)

_SOURCE = "tavily"
_SEARCH_URL = "https://api.tavily.com/search"


class TavilyResult(WebSearchResult):
    score: float | None = None


class TavilyProvider(WebSearchProvider):

    async def search(
            self, query: str, *, limit: int = 5,
            news_days: int | None = None) -> list[WebSearchResult]:
        if not settings.TAVILY_API_KEY:
            raise ProviderUnavailableError(
                "TAVILY_API_KEY is not configured -- register a free key at "
                "https://tavily.com (1,000 credits/month, no card required).")

        body: dict[str, object] = {
            "query": query,
            "search_depth": "basic",
            "max_results": max(1, min(limit, 10)),
            "include_answer": False,
            "include_raw_content": False
        }
        if news_days is not None and news_days > 0:
            body["topic"] = "news"
            body["days"] = news_days
        client = get_http_client()
        try:
            resp = await client.post(
                _SEARCH_URL, headers={
                    "Authorization": f"Bearer {settings.TAVILY_API_KEY}",
                    "Content-Type": "application/json"
                }, json=body)
            resp.raise_for_status()
        except Exception as exc:
            raise ProviderRequestError(
                f"Tavily search failed for query {query!r}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderRequestError(
                f"Tavily returned a non-JSON response for query {query!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError(
                f"Tavily returned an unexpected response shape for query "
                f"{query!r}")
        results: list[TavilyResult] = []
        for item in (payload.get("results") or [])[:max(limit, 0)]:
            if not isinstance(item, dict):
                continue
            published_at = _parse_published_date(item.get("published_date"))
            raw_score = item.get("score")
            score = (
                None if isinstance(raw_score, bool) or
                not isinstance(raw_score, int | float) else float(raw_score))
            results.append(
                TavilyResult(
                    title=item.get("title") or "(untitled)",
                    url=item.get("url", ""), snippet=item.get("content", ""),
                    published_at=published_at, source=_SOURCE, score=score))
        results.sort(key=lambda r: -(r.score if r.score is not None else -1.0))
        ordered: list[WebSearchResult] = [*results]
        return ordered


def _parse_published_date(raw_date: object) -> datetime | None:
    if not raw_date or not isinstance(raw_date, str):
        return None
    try:
        return datetime.fromisoformat(raw_date)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(raw_date)
    except (TypeError, ValueError, IndexError):
        logger.warning("tavily.unparseable_published_date", raw=raw_date)
        return None
