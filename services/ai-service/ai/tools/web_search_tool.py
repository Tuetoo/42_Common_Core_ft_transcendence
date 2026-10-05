import re
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse
import structlog
from ai.agents.evidence_grounding import non_english_reason
from ai.providers.base import ProviderError, WebSearchProvider, WebSearchResult
from ai.providers.news_relevance import UNCLASSIFIED
from ai.providers.newsapi import catalog_flag, source_catalog
from ai.providers.registry import get_web_search_provider
from ai.providers.tavily import TavilyProvider
from ai.tools.base import Tool, age_in_days, clamp_int
from ai.tools.news_tool import (LINK_UNAVAILABLE, catalog_marker, kind_tag,
                                results_header)
from ai.tools.search_relevance import classify_results

logger = structlog.get_logger(__name__)

_STALE_SOURCE_THRESHOLD_DAYS = 730

_URL_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")


def _extract_date_from_url(url: str) -> datetime | None:
    match = _URL_DATE_RE.search(urlparse(url).path)
    if not match:
        return None
    year, month, day = (int(group) for group in match.groups())
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def _stale_source_flag(published_at: datetime) -> str:
    age_days = age_in_days(published_at)
    if age_days is None or age_days <= _STALE_SOURCE_THRESHOLD_DAYS:
        return ""
    years = age_days / 365
    return (
        f"[STALE SOURCE -- published ~{years:.1f} years ago, do NOT treat "
        "as describing current conditions unless the content itself is "
        "explicitly historical context] ")


def _format_result(
        r: WebSearchResult, catalog: bool | None = None,
        kind: str = UNCLASSIFIED) -> str:
    link = r.url or LINK_UNAVAILABLE
    body = f"{kind_tag(kind)}{r.title} ({link}): {r.snippet}" + catalog_marker(
        catalog)
    if r.published_at is not None:
        date_part = (
            f"{_stale_source_flag(r.published_at)}"
            f"[{r.published_at.date()} {r.published_at:%a}] ")
        return f"{date_part}{body}"

    inferred = _extract_date_from_url(r.url)
    if inferred is not None:
        date_part = (
            f"{_stale_source_flag(inferred)}[{inferred.date()}, inferred "
            "from URL, not confirmed by the source] ")
        return f"{date_part}{body}"

    return (
        "[PUBLISH DATE UNKNOWN -- provider gave no date and none could be "
        "inferred from the URL; verify recency before treating this as "
        "describing current conditions] "
        f"{body}")


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web for real, current information. action_input: "
        '{"query": "Federal Reserve July 2026 rate decision", "limit": 5} '
        '("limit" optional, default 5, max 10). Returns short snippets from '
        "each result, not full articles -- you must synthesize your own "
        "summary from these snippets in your own words; do not quote long "
        "passages verbatim, and mention which source(s) a claim came from. "
        "Each result line ends with catalog=yes (an outlet in NewsAPI's "
        "source catalog) or catalog=no.")
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string"
            },
            "limit": {
                "type": "integer"
            }
        },
        "required": ["query"],
        "additionalProperties": False
    }

    def __init__(
            self, provider: WebSearchProvider | None = None,
            ticker: str | None = None, news_days: int | None = None) -> None:
        self.provider = provider or get_web_search_provider()
        self.ticker = ticker
        self.news_days = news_days

    async def run(  # type: ignore[override]
            self, query: str, limit: int = 5) -> str:
        limit = clamp_int(limit, default=5, lo=1, hi=10)
        try:
            if self.news_days and isinstance(self.provider, TavilyProvider):
                results = await self.provider.search(
                    query, limit=limit, news_days=self.news_days)
            else:
                results = await self.provider.search(query, limit=limit)
        except ProviderError as exc:
            return f"Web search failed for {query!r}: {exc}"

        if not results:
            return f"No web search results found for {query!r}."

        english_results = [
            r for r in results
            if non_english_reason(f"{r.title}\n{r.snippet}") is None
        ]
        if len(english_results) != len(results):
            dropped = len(results) - len(english_results)
            logger.info(
                "web_search_tool.non_english_results_dropped", query=query,
                dropped=dropped, total=len(results))
            if not english_results:
                return (
                    f"No web search results found for {query!r} "
                    f"({dropped} non-English result(s) were discarded).")
        results = english_results

        classified = await classify_results(query, results, ticker=self.ticker)
        if not classified:
            return (
                f"Web search for {query!r} returned results, but none were "
                "judged genuinely relevant to the query -- try a more "
                "specific or differently-worded search.")

        catalog = await source_catalog()
        flags = [
            catalog_flag(c.result.url, source_id=None, catalog=catalog)
            for c in classified
        ]
        lines = [
            _format_result(c.result, flag, c.kind)
            for c, flag in zip(classified, flags, strict=True)
        ]
        header = results_header(
            "Web search", query, "results", [c.kind for c in classified],
            flags)
        return header + "\n" + "\n".join(lines)
