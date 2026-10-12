from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import structlog
from ai.llm.base import AGENT_LLM_CALL_RETRIES, LLMMessage, LLMTier
from ai.llm.router import LLMRouter
from ai.providers.base import (MacroDataPoint, ProviderError,
                               ProviderQuotaError, ProviderUnavailableError)
from ai.providers.fred import FredProvider
from ai.providers.news_filters import (build_ticker_news_query,
                                       company_search_name)
from ai.providers.news_relevance import (clean_articles, lookup_company_name,
                                         stored_evidence_articles)
from ai.providers.newsapi import NewsApiProvider
from ai.providers.sec_edgar import load_ticker_to_title
from ai.tools.macro_tool import (YOY_INDEX_SERIES_IDS, YOY_MIN_LOOKBACK_DAYS,
                                 point_on_or_before)
from config import settings

logger = structlog.get_logger(__name__)

_tracked: ContextVar[list[str] | None] = ContextVar(
    "ingestion_source_failures", default=None)


def tracking_source_failures() -> bool:
    return _tracked.get() is not None


def report_source_failure(source: str) -> None:
    failures = _tracked.get()
    if failures is not None:
        failures.append(source)


@contextmanager
def track_source_failures() -> Iterator[list[str]]:
    failures: list[str] = []
    token = _tracked.set(failures)
    try:
        yield failures
    finally:
        _tracked.reset(token)


_NEWS_ARTICLES_PER_TICKER = 8
_NEWS_SINCE_DAYS = 28
_NEWS_MIN_WORD_COUNT = 80


async def _ticker_map_unavailable() -> bool:
    if not settings.SEC_EDGAR_USER_AGENT:
        return False
    try:
        await load_ticker_to_title()
    except ProviderError:
        return True
    return False


@dataclass
class NewsDigest:
    filename: str
    text: str
    article_count: int
    content_date: str | None = None


async def generate_news_digest(
        ticker: str, *,
        already_ingested_filenames: set[str]) -> NewsDigest | None:
    today = datetime.now(UTC).date().isoformat()
    filename = f"{ticker.upper()}_news_{today}.txt"
    if filename in already_ingested_filenames:
        return None

    provider = NewsApiProvider()
    since = datetime.now(UTC) - timedelta(days=_NEWS_SINCE_DAYS)
    legal_name = await lookup_company_name(ticker)
    if legal_name is None:
        logger.warning(
            "auto_ingestion.news_unverified_skipped", ticker=ticker,
            reason="company name lookup failed")
        if tracking_source_failures() and await _ticker_map_unavailable():
            report_source_failure("sec_ticker_map")
        return None
    query = build_ticker_news_query(
        "", ticker=ticker, company_name=company_search_name(legal_name))
    try:
        raw_articles = await provider.search_news(
            query, since=since, limit=_NEWS_ARTICLES_PER_TICKER,
            sort_by="publishedAt")
    except (ProviderQuotaError, ProviderUnavailableError):
        raise
    except ProviderError:
        logger.exception("auto_ingestion.news_fetch_failed", ticker=ticker)
        report_source_failure("newsapi")
        return None

    def _log_non_english(dropped: int) -> None:
        logger.info(
            "auto_ingestion.news_non_english_dropped", ticker=ticker,
            dropped_count=dropped)

    cleaned = await clean_articles(
        raw_articles, ticker, on_non_english_dropped=_log_non_english)
    english_articles = cleaned.english
    checked = stored_evidence_articles(cleaned.classified, ticker=ticker)
    if checked is None:
        logger.warning(
            "auto_ingestion.news_unverified_skipped", ticker=ticker,
            reason="relevance classifier unavailable",
            count=len(english_articles))
        report_source_failure("news_relevance")
        return None
    articles = checked
    if len(articles) < len(english_articles):
        logger.info(
            "auto_ingestion.news_filtered_irrelevant", ticker=ticker,
            dropped_count=len(english_articles) - len(articles))

    if not articles:
        logger.info("auto_ingestion.no_news_articles", ticker=ticker)
        return None

    lines = [f"Recent news summary -- {ticker.upper()}\n"]
    for article in articles:
        published = article.published_at.date().isoformat()
        summary = article.summary or ""
        lines.append(
            f"[{published} | {article.source_name}] {article.title}\n"
            f"{summary}\n")
    document_text = "\n".join(lines)

    if len(document_text.split()) < _NEWS_MIN_WORD_COUNT:
        logger.info(
            "auto_ingestion.news_digest_too_short", ticker=ticker,
            word_count=len(document_text.split()))
        return None

    return NewsDigest(
        filename=filename, text=document_text, article_count=len(articles),
        content_date=today)


_MACRO_INDICATORS = (
    "fed_funds_rate", "cpi", "unemployment_rate", "gdp", "10y_treasury_yield")
_MACRO_MIN_WORD_COUNT = 60

_MACRO_PROMPT_TEMPLATE = (
    "You are a macroeconomic research analyst. Using ONLY the real "
    "indicator values listed below (do not invent any numbers not given "
    "here), write a concise 2-3 paragraph macro research note summarizing "
    "the current U.S. macroeconomic backdrop for an equity research "
    "audience.\n\n{facts}")


def _fact_line(points: list[MacroDataPoint]) -> str:
    latest = points[-1]
    as_of = latest.observed_at.isoformat()
    if latest.series_id not in YOY_INDEX_SERIES_IDS:
        unit = latest.unit or ""
        return (
            f"{latest.label}: {latest.value}{unit} as of {as_of} "
            f"(source: {latest.source})")
    ref = point_on_or_before(
        points[:-1], latest.observed_at - timedelta(days=365))
    if ref is None or ref.value == 0:
        logger.warning(
            "auto_ingestion.macro_year_over_year_unavailable",
            series_id=latest.series_id,
            first_observation=points[0].observed_at.isoformat())
        return (
            f"{latest.label}: index level {latest.value} as of {as_of}; "
            f"this is an index level, not an inflation rate, and the "
            f"year-over-year change is not available "
            f"(source: {latest.source})")
    change_pct = (latest.value / ref.value - 1) * 100
    return (
        f"{latest.label}: year-over-year inflation {change_pct:.2f}% as of "
        f"{as_of} (index {latest.value} versus {ref.value} on "
        f"{ref.observed_at.isoformat()}; source: {latest.source})")


@dataclass
class MacroReport:
    filename: str
    text: str
    content_date: str | None = None


async def generate_macro_report(
        *, already_ingested_filenames: set[str]) -> MacroReport | None:
    month_key = datetime.now(UTC).strftime("%Y-%m")
    filename = f"macro_auto_{month_key}.txt"
    if filename in already_ingested_filenames:
        return None

    provider = FredProvider()
    fact_lines: list[str] = []
    fetch_failures = 0
    for indicator in _MACRO_INDICATORS:
        try:
            points = await provider.get_indicator(
                indicator, lookback_days=YOY_MIN_LOOKBACK_DAYS)
        except ProviderUnavailableError:
            raise
        except ProviderError:
            logger.exception(
                "auto_ingestion.macro_indicator_fetch_failed",
                indicator=indicator)
            fetch_failures += 1
            continue
        if not points:
            logger.warning(
                "auto_ingestion.macro_indicator_empty", indicator=indicator,
                lookback_days=YOY_MIN_LOOKBACK_DAYS)
            continue
        fact_lines.append(_fact_line(points))

    if not fact_lines:
        logger.warning("auto_ingestion.no_macro_data_available")
        if fetch_failures:
            report_source_failure("fred")
        return None

    prompt = _MACRO_PROMPT_TEMPLATE.format(facts="\n".join(fact_lines))
    router = LLMRouter()
    try:
        response = await router.complete(
            LLMTier.CHEAP, [LLMMessage(role="user", content=prompt)],
            retries=AGENT_LLM_CALL_RETRIES)
    except Exception:
        logger.exception("auto_ingestion.macro_narrative_generation_failed")
        report_source_failure("llm")
        return None

    text = response.content.strip()
    if len(text.split()) < _MACRO_MIN_WORD_COUNT:
        logger.warning(
            "auto_ingestion.macro_narrative_too_short",
            word_count=len(text.split()))
        return None

    return MacroReport(
        filename=filename, text=text,
        content_date=datetime.now(UTC).date().isoformat())
