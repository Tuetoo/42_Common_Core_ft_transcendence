import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar
import structlog
from ai.knowledge.recycled_content import scratchpad_field
from ai.providers.base import NewsArticle, NewsProvider, ProviderError
from ai.providers.news_filters import (build_ticker_news_query,
                                       company_search_name)
from ai.providers.news_relevance import (COMPANY_BUSINESS, CONSUMER_PRODUCT,
                                         EVIDENCE_KINDS, OPINION_LISTICLE,
                                         STOCK_MARKET, UNCLASSIFIED,
                                         UNVERIFIED, cap_consumer_items,
                                         clean_articles, lookup_company_name)
from ai.providers.newsapi import (NewsApiArticle, NewsApiProvider,
                                  SourceCatalog, catalog_flag, source_catalog)
from ai.providers.registry import get_news_provider
from ai.tools.base import Tool, clamp_int
from config import settings

logger = structlog.get_logger(__name__)

LIVE_SEARCH_ACTIONS = frozenset({"search_news", "web_search"})
KIND_LABELS: dict[str, str] = {
    COMPANY_BUSINESS: "company business",
    STOCK_MARKET: "stock market",
    OPINION_LISTICLE: "opinion/listicle",
    CONSUMER_PRODUCT: "consumer product",
    UNCLASSIFIED: "unclassified",
    UNVERIFIED: "unverified"
}
_OPTIONAL_HEADER_KINDS = frozenset({UNVERIFIED})
_LABEL_KINDS = {label: kind for kind, label in KIND_LABELS.items()}
_HEADER_RE = re.compile(
    r"^(?:News|Web search) results for .*?\((\d+) (?:articles|results)"
    r"(?:: ([^()\n]*))?\):")
_KIND_COUNT_RE = re.compile(
    r"(\d+) (" + "|".join(re.escape(label) for label in _LABEL_KINDS) + r")\b")
_CATALOG_COUNT_RE = re.compile(r"(\d+) from catalog outlets")
_ZERO_YIELD_MARKERS = (
    "No news articles found", "No web search results found",
    "none were judged genuinely relevant", "News search failed",
    "Web search failed")
CATALOG_MARKER = "catalog=yes"
NOT_IN_CATALOG_MARKER = "catalog=no"
LINK_UNAVAILABLE = "link unavailable"


def catalog_marker(flag: bool | None) -> str:
    if flag is None:
        return ""
    return f" | {CATALOG_MARKER if flag else NOT_IN_CATALOG_MARKER}"


def results_header(
        label: str, query: str, noun: str, kinds: Sequence[str],
        catalog_flags: Sequence[bool | None]) -> str:
    counts = Counter(kinds)
    kinds_text = ", ".join(
        f"{counts.get(kind, 0)} {kind_label}"
        for kind, kind_label in KIND_LABELS.items()
        if kind not in _OPTIONAL_HEADER_KINDS or counts.get(kind, 0))
    in_catalog = sum(flag is True for flag in catalog_flags)
    catalog_text = (
        f"; {in_catalog} from catalog outlets" if in_catalog or all(
            flag is not None for flag in catalog_flags) else "")
    return (
        f"{label} results for {query!r} "
        f"({len(kinds)} {noun}: {kinds_text}{catalog_text}):")


@dataclass(frozen=True)
class ResultHeader:
    total: int
    kinds: dict[str, int] | None
    catalog_outlets: int | None


def parse_results_header(observation: str | None) -> ResultHeader | None:
    if not observation:
        return None
    match = _HEADER_RE.match(observation)
    if match is None:
        return None
    detail = match.group(2) or ""
    kinds = {
        _LABEL_KINDS[label]: int(count)
        for count, label in _KIND_COUNT_RE.findall(detail)
    } or None
    catalog = _CATALOG_COUNT_RE.search(detail)
    return ResultHeader(
        total=int(match.group(1)), kinds=kinds,
        catalog_outlets=int(catalog.group(1)) if catalog else None)


def observation_yield(observation: str | None) -> int | None:
    if not observation:
        return None
    header = parse_results_header(observation)
    if header is not None:
        if header.kinds is None:
            return header.total
        return sum(header.kinds.get(kind, 0) for kind in EVIDENCE_KINDS)
    if any(marker in observation for marker in _ZERO_YIELD_MARKERS):
        return 0
    return None


def counts_as_live_search(entry: Any) -> bool:
    if not scratchpad_field(entry, "is_tool_result", True):
        return False
    if not scratchpad_field(entry, "tool_error", False):
        return True
    observation = scratchpad_field(entry, "observation")
    return (
        observation_yield(
            observation if isinstance(observation, str) else None) == 0)


def ran_live_search(scratchpad: Iterable[Any]) -> bool:
    return any(
        scratchpad_field(entry, "action") in LIVE_SEARCH_ACTIONS
        and counts_as_live_search(entry) for entry in scratchpad)


def live_search_yield(scratchpad: Iterable[Any]) -> int | None:
    total: int | None = None
    for entry in scratchpad:
        if scratchpad_field(
                entry,
                "action") not in LIVE_SEARCH_ACTIONS or not (scratchpad_field(
                    entry, "is_tool_result", True)):
            continue
        observation = scratchpad_field(entry, "observation")
        count = observation_yield(
            observation if isinstance(observation, str) else None)
        if count is None:
            continue
        total = (total or 0) + count
    return total


TICKER_SCOPED_MIN_LIMIT = 20

_KIND_TAGS = {
    CONSUMER_PRODUCT: "[consumer product] ",
    OPINION_LISTICLE: "[opinion/listicle] ",
    UNVERIFIED: "[unverified] "
}


def kind_tag(kind: str) -> str:
    return _KIND_TAGS.get(kind, "")


TOOL_LABELS: tuple[str, ...] = (
    "catalog=", "catalog outlet", "not in catalog",
    *(tag.strip() for tag in _KIND_TAGS.values()), LINK_UNAVAILABLE)


def tool_labels_in_text(text: str) -> list[str]:
    lowered = text.lower()
    return [label for label in TOOL_LABELS if label.lower() in lowered]


def article_catalog_flag(
        article: NewsArticle, catalog: SourceCatalog | None) -> bool | None:
    source_id = (
        article.source_id if isinstance(article, NewsApiArticle) else None)
    return catalog_flag(article.url, source_id=source_id, catalog=catalog)


def format_article_line(
        article: NewsArticle, *, kind: str = UNCLASSIFIED,
        catalog: bool | None = None) -> str:
    day = article.published_at.date()
    return (
        f"[{day} {day:%a}] {article.source_name}: "
        f"{kind_tag(kind)}{article.title} "
        f"({article.url or LINK_UNAVAILABLE})" +
        (f" -- {article.summary}" if article.summary else "") +
        catalog_marker(catalog))


def merge_articles(
        first: Iterable[NewsArticle],
        second: Iterable[NewsArticle]) -> list[NewsArticle]:
    seen: set[str] = set()
    merged: list[NewsArticle] = []
    for article in [*first, *second]:
        keys = {" ".join(article.title.lower().split())}
        if article.url:
            keys.add(article.url)
        if keys & seen:
            continue
        seen |= keys
        merged.append(article)
    return merged


class SearchNewsTool(Tool):
    name = "search_news"
    description = (
        'Search recent news articles. action_input: {"query": '
        '"earnings guidance", "limit": 10} ("limit" optional, default 20). '
        "When this search is for "
        "the ticker under analysis, the ticker and company name are added to "
        'the query automatically: put only topic words in "query" (or a '
        'general phrase such as "latest news" for all recent coverage). '
        "Each item line ends with catalog=yes (an outlet in NewsAPI's source "
        "catalog) or catalog=no.")
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
            self, provider: NewsProvider | None = None,
            ticker: str | None = None) -> None:
        self.provider = provider or get_news_provider()
        self.ticker = ticker
        self._consumer_shown: set[str] = set()

    async def run(  # type: ignore[override]
            self, query: str, limit: int = 20) -> str:
        limit = clamp_int(limit, default=20, lo=1, hi=100)
        company_name: str | None = None
        if self.ticker is not None:
            resolved = await lookup_company_name(self.ticker)
            company_name = company_search_name(resolved) if resolved else None
            model_query = query
            query = build_ticker_news_query(
                model_query, ticker=self.ticker, company_name=company_name)
            limit = max(limit, TICKER_SCOPED_MIN_LIMIT)
            logger.info(
                "news_tool.ticker_query_built", ticker=self.ticker,
                model_query=model_query, query=query)
        try:
            articles = await self.provider.search_news(query, limit=limit)
        except ProviderError as exc:
            return f"News search failed for {query!r}: {exc}"
        articles = await self._with_recent(query, articles)

        if not articles and self.ticker is not None and ") AND (" in query:
            fallback = build_ticker_news_query(
                "", ticker=self.ticker, company_name=company_name)
            logger.info(
                "news_tool.topic_query_empty_fallback", ticker=self.ticker,
                query=query, fallback_query=fallback)
            query = fallback
            try:
                articles = await self.provider.search_news(query, limit=limit)
            except ProviderError as exc:
                return f"News search failed for {query!r}: {exc}"
            articles = await self._with_recent(query, articles)

        if not articles:
            return f"No news articles found for {query!r}."

        total = len(articles)

        def _log_non_english(dropped: int) -> None:
            logger.info(
                "news_tool.non_english_articles_dropped", query=query,
                dropped=dropped, total=total)

        cleaned = await clean_articles(
            articles, self.ticker, on_non_english_dropped=_log_non_english)
        classified = cleaned.classified
        if not cleaned.english:
            return (
                f"No news articles found for {query!r} "
                f"({total} non-English result(s) were "
                f"discarded).")

        if self.ticker is not None:
            classified = cap_consumer_items(
                classified, shown=self._consumer_shown,
                max_items=settings.NEWS_CONSUMER_MAX_ITEMS,
                log_event="news_tool.consumer_run_cap", ticker=self.ticker)
            if not classified:
                return (
                    f"News search for {query!r} returned results, but none "
                    f"were "
                    f"judged genuinely relevant to {self.ticker} after "
                    f"filtering "
                    "out likely off-topic matches.")

        classified = [c for c in classified if c.kind != CONSUMER_PRODUCT] + [
            c for c in classified if c.kind == CONSUMER_PRODUCT
        ]
        catalog = await source_catalog()
        flags = [article_catalog_flag(c.article, catalog) for c in classified]
        lines = [
            format_article_line(c.article, kind=c.kind, catalog=flag)
            for c, flag in zip(classified, flags, strict=True)
        ]
        header = results_header(
            "News", query, "articles", [c.kind for c in classified], flags)
        return header + "\n" + "\n".join(lines)

    async def _with_recent(self, query: str,
                           articles: list[NewsArticle]) -> list[NewsArticle]:
        extra = settings.NEWSAPI_RECENT_EXTRA_LIMIT
        if (self.ticker is None or extra <= 0
                or not isinstance(self.provider, NewsApiProvider)
                or settings.NEWSAPI_SORT_BY == "publishedAt"):
            return articles
        try:
            recent = await self.provider.search_news(
                query, limit=extra, sort_by="publishedAt")
        except ProviderError:
            logger.warning("news_tool.recent_search_failed", query=query)
            return articles
        merged = merge_articles(recent, articles)
        logger.info(
            "news_tool.recent_merged", query=query, recent=len(recent),
            main=len(articles), merged=len(merged))
        return merged
