from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import structlog
from ai.ingestion.source_failures import (report_source_failure,
                                          tracking_source_failures)
from ai.providers.base import (ProviderError, ProviderQuotaError,
                               ProviderUnavailableError)
from ai.providers.news_filters import (build_ticker_news_query,
                                       company_search_name)
from ai.providers.news_relevance import (clean_articles, lookup_company_name,
                                         stored_evidence_articles)
from ai.providers.newsapi import NewsApiProvider
from ai.providers.sec_edgar import load_ticker_to_title
from config import settings

logger = structlog.get_logger(__name__)

_ARTICLES_PER_TICKER = 8
_SINCE_DAYS = 28
_MIN_WORD_COUNT = 80


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
    since = datetime.now(UTC) - timedelta(days=_SINCE_DAYS)
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
            query, since=since, limit=_ARTICLES_PER_TICKER,
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

    if len(document_text.split()) < _MIN_WORD_COUNT:
        logger.info(
            "auto_ingestion.news_digest_too_short", ticker=ticker,
            word_count=len(document_text.split()))
        return None

    return NewsDigest(
        filename=filename, text=document_text, article_count=len(articles),
        content_date=today)
