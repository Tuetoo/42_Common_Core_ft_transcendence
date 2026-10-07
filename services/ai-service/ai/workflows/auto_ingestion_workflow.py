from dataclasses import dataclass, field
from typing import Any
import structlog
from ai.ingestion.macro_ingestion import generate_macro_report
from ai.ingestion.news_ingestion import generate_news_digest
from ai.ingestion.sec_edgar_filings import ingest_new_filings
from ai.ingestion.source_failures import track_source_failures
from ai.providers.base import ProviderQuotaError
from clients.core_client import (create_system_document,
                                 get_existing_document_filenames,
                                 get_tracked_tickers)
from config import settings
from shared.enums import DocumentCategory

logger = structlog.get_logger(__name__)

_SOURCE_SETTINGS = {
    "sec_edgar": "SEC_EDGAR_USER_AGENT",
    "newsapi": "NEWSAPI_API_KEY",
    "fred": "FRED_API_KEY"
}


def source_configured(source: str) -> bool:
    setting = _SOURCE_SETTINGS[source]
    if getattr(settings, setting):
        return True
    logger.warning(
        "auto_ingestion.source_not_configured", source=source, setting=setting)
    return False


@dataclass
class _Step:
    attempted: int = 0
    failed_tickers: list[str] = field(default_factory=list)
    quota_deferred: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "failed_tickers": self.failed_tickers,
            "quota_deferred": self.quota_deferred
        }


async def run_auto_ingestion_workflow() -> dict[str, Any]:
    tickers = await get_tracked_tickers()
    logger.info("auto_ingestion.started", ticker_count=len(tickers))
    filings_on = source_configured("sec_edgar")
    news_on = source_configured("newsapi")

    documents_created = 0
    filings, news = _Step(), _Step()
    for ticker in tickers:
        if filings_on:
            stored, ok = await _ingest_filings(ticker)
            documents_created += stored
            filings.attempted += 1
            if not ok:
                filings.failed_tickers.append(ticker)
        if news_on:
            outcome = await _ingest_news(ticker)
            if outcome == "quota":
                news.quota_deferred.append(ticker)
                continue
            news.attempted += 1
            if outcome == "failed":
                news.failed_tickers.append(ticker)
            elif outcome == "stored":
                documents_created += 1

    failed = sorted(set(filings.failed_tickers) | set(news.failed_tickers))
    if news.quota_deferred:
        logger.warning(
            "auto_ingestion.news_quota_deferred", tickers=news.quota_deferred)
    logger.info(
        "auto_ingestion.complete",
        tickers_processed=len(tickers) - len(failed),
        documents_created=documents_created, failures=failed)
    return {
        "tickers_processed": len(tickers) - len(failed),
        "documents_created": documents_created,
        "failed_tickers": failed,
        "filings": filings.as_dict(),
        "news": news.as_dict()
    }


async def _ingest_filings(ticker: str) -> tuple[int, bool]:
    with track_source_failures() as source_failures:
        try:
            result = await ingest_new_filings(ticker)
        except Exception:
            logger.exception("auto_ingestion.filings_failed", ticker=ticker)
            return 0, False
    if result.failed_filenames:
        logger.warning(
            "auto_ingestion.filings_not_stored", ticker=ticker,
            filenames=result.failed_filenames)
    if source_failures:
        logger.warning(
            "auto_ingestion.ticker_source_failed", ticker=ticker,
            step="filings", sources=sorted(set(source_failures)),
            documents_created=result.documents_created)
    ok = not result.failed_filenames and not source_failures
    return result.documents_created, ok


async def _ingest_news(ticker: str) -> str:
    with track_source_failures() as source_failures:
        try:
            existing = set(
                await get_existing_document_filenames(
                    ticker, DocumentCategory.NEWS.value))
            digest = await generate_news_digest(
                ticker, already_ingested_filenames=existing)
            if digest is not None:
                await create_system_document(
                    filename=digest.filename, content=digest.text,
                    category=DocumentCategory.NEWS.value, ticker=ticker,
                    content_date=digest.content_date)
                logger.info(
                    "auto_ingestion.document_created", ticker=ticker,
                    filename=digest.filename,
                    article_count=digest.article_count)
        except ProviderQuotaError:
            return "quota"
        except Exception:
            logger.exception("auto_ingestion.news_failed", ticker=ticker)
            return "failed"
    if source_failures:
        logger.warning(
            "auto_ingestion.ticker_source_failed", ticker=ticker, step="news",
            sources=sorted(set(source_failures)))
        return "failed"
    return "stored" if digest is not None else "nothing_new"


async def run_macro_auto_ingestion_workflow() -> dict[str, Any]:
    if not source_configured("fred"):
        return {"documents_created": 0, "failed": False, "attempted": False}
    try:
        existing_filenames = set(
            await get_existing_document_filenames(
                None, DocumentCategory.MACRO_RESEARCH.value))
        with track_source_failures() as source_failures:
            report = await generate_macro_report(
                already_ingested_filenames=existing_filenames)
        if report is None:
            if source_failures:
                logger.warning(
                    "auto_ingestion.macro_source_failed",
                    sources=sorted(set(source_failures)))
            return {
                "documents_created": 0,
                "failed": bool(source_failures),
                "attempted": True
            }
        await create_system_document(
            filename=report.filename, content=report.text,
            category=DocumentCategory.MACRO_RESEARCH.value, ticker=None,
            content_date=report.content_date)
    except Exception:
        logger.exception("auto_ingestion.macro_workflow_failed")
        return {"documents_created": 0, "failed": True, "attempted": True}

    logger.info("auto_ingestion.document_created", filename=report.filename)
    return {"documents_created": 1, "failed": False, "attempted": True}
