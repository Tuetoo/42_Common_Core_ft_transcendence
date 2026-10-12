import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse
import structlog
from ai.agents.evidence_grounding import non_english_reason
from ai.llm.router import LLMRouter
from ai.providers.base import ProviderError, WebSearchProvider, WebSearchResult
from ai.providers.news_filters import (SNIPPET_CHARS, drop_prechecked,
                                       relink_consent_items)
from ai.providers.news_relevance import (NEWS_KINDS, NOT_NEWS, PRICE_PAGE,
                                         REFERENCE_OR_SOCIAL, UNCLASSIFIED,
                                         UNVERIFIED, kind_definitions_text,
                                         lookup_search_name,
                                         verify_with_cheap_tier)
from ai.providers.newsapi import catalog_flag, source_catalog, url_host
from ai.providers.registry import get_web_search_provider
from ai.providers.tavily import TavilyProvider
from ai.tools.base import Tool, age_in_days, clamp_int
from ai.tools.news_tool import (LINK_UNAVAILABLE, catalog_marker, kind_tag,
                                results_header)

logger = structlog.get_logger(__name__)

WEB_KINDS = (*NEWS_KINDS, REFERENCE_OR_SOCIAL)
_DROPPED_KINDS = frozenset({PRICE_PAGE, NOT_NEWS, REFERENCE_OR_SOCIAL})


@dataclass(frozen=True)
class ClassifiedResult:
    result: WebSearchResult
    kind: str


def _system_prompt(query: str, subject: str | None) -> str:
    context = (
        f" The search is part of research on {subject}." if subject else "")
    return (
        f"The search query was: {query!r}.{context}\n\n"
        "For each numbered search result give:\n"
        '- "reasoning": one short sentence about that result, written first;\n'
        '- "relevant": true when the result really addresses what the query '
        "was looking for, false when it only shares a keyword while being "
        "about something else (an ordinary word, an unrelated business or "
        "person with the same name, a generic page that never addresses the "
        "query's subject);\n"
        '- "kind", exactly one of:\n'
        f"{kind_definitions_text(WEB_KINDS)}\n\n"
        'Respond with ONLY a JSON object whose "relevant" key holds one '
        'object per result, in the order given: {"relevant": [{"index": '
        '0, "title": "first five words of that result\'s title, copied '
        'without any quotation marks", "reasoning": "...", "relevant": '
        'true, "kind": "..."}, ...]}. No text outside the JSON object. '
        "Never put a double quote character inside a string value (use "
        "single quotes there), so the object stays valid JSON.")


def _verdict(item: dict[str, Any]) -> tuple[bool, str] | None:
    relevant = item.get("relevant")
    if isinstance(relevant, str):
        relevant = {"true": True, "false": False}.get(relevant.strip().lower())
    kind = str(item.get("kind", "")).strip().lower()
    if not isinstance(relevant, bool) or kind not in WEB_KINDS:
        return None
    return relevant, kind


async def _classify_results(
        query: str, results: list[WebSearchResult], llm_router: LLMRouter,
        subject: str | None) -> list[ClassifiedResult]:
    verdicts = await verify_with_cheap_tier(
        llm_router, system_prompt=_system_prompt(query, subject),
        numbered="\n".join(
            f"{i}. {r.title} ({url_host(r.url)}): {r.snippet}"
            for i, r in enumerate(results)), titles=[r.title for r in results],
        log_prefix="search_relevance", response_format="json_object",
        query=query)
    if verdicts is None:
        return [ClassifiedResult(r, UNCLASSIFIED) for r in results]
    kept: list[ClassifiedResult] = []
    dropped: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for i, result in enumerate(results):
        if i in verdicts.unreadable:
            kept.append(ClassifiedResult(result, UNVERIFIED))
            counts[UNVERIFIED] += 1
            continue
        item = verdicts.trusted.get(i)
        verdict = _verdict(item) if item is not None else None
        if verdict is None:
            kept.append(ClassifiedResult(result, UNCLASSIFIED))
            counts[UNCLASSIFIED] += 1
            continue
        relevant, kind = verdict
        counts[f"{'relevant' if relevant else 'irrelevant'}/{kind}"] += 1
        if relevant and kind not in _DROPPED_KINDS:
            kept.append(ClassifiedResult(result, kind))
            continue
        dropped.append({
            "title": result.title[:SNIPPET_CHARS],
            "relevant": relevant,
            "kind": kind,
            "reasoning": (
                str(item.get("reasoning", ""))[:160] if item else "")
        })
    logger.info(
        "search_relevance.classified", query=query, total_checked=len(results),
        by_verdict=dict(counts), dropped_count=len(dropped), dropped=dropped)
    return kept


async def classify_results(
        query: str, results: Sequence[WebSearchResult], *,
        llm_router: LLMRouter | None = None,
        ticker: str | None = None) -> list[ClassifiedResult]:
    if not results:
        return []
    kept = drop_prechecked(
        list(results), title_of=lambda r: r.title,
        log_event="search_relevance.prechecked", min_title_words=1,
        query=query, ticker=ticker)
    if not kept:
        return []
    kept = relink_consent_items(
        kept, title_of=lambda r: r.title,
        log_event="search_relevance.consent_link_replaced", query=query,
        ticker=ticker)
    subject: str | None = None
    if ticker is not None:
        name = await lookup_search_name(ticker)
        subject = f"{name} (ticker {ticker})" if name else f"ticker {ticker}"
    return await _classify_results(
        query, kept, llm_router or LLMRouter(), subject)


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
