import asyncio
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any, TypeVar
import structlog
from ai.ingestion.digests import (report_source_failure,
                                  track_source_failures)
from ai.knowledge.filing_records import store_filing_records
from ai.knowledge.filing_summary import (FilingTooLargeError,
                                         extract_filing_records,
                                         is_fallback_summary, summarize_filing)
from ai.llm.base import LLMTier
from ai.providers.sec_edgar import filing_period, forget_filing_caches
from clients.core_client import (create_system_document,
                                 get_existing_document_filenames)
from config import settings
from shared.enums import DocumentCategory
from shared.storage import upload_bytes

logger = structlog.get_logger(__name__)

_WANTED_FORMS = ("10-K", "10-Q")
_EDGAR_API_TIMEOUT_SECONDS = 15
_MAX_PER_FORM: dict[str, int] = {"10-K": 1, "10-Q": 4}


@dataclass
class NewFiling:
    filename: str
    text: str
    form: str
    filing_date: str
    is_fallback: bool = False
    records: dict[str, Any] | None = None
    report_date: str | None = None


_T = TypeVar("_T")


def _call_with_timeout(fn: Callable[[], _T], timeout: float) -> _T:
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        return executor.submit(fn).result(timeout=timeout)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


FetchedFiling = tuple[str, str, str, str, str | None, str | None]


@dataclass
class FormListing:
    filings: list[Any]
    older_pages_read: int = 0


def _older_page_filings(
        company: Any, form: str, needed: int, *,
        until_date: str | None = None) -> tuple[list[Any], int]:
    from edgar import Filing
    from edgar.config import SEC_DATA_URL
    from edgar.httprequests import download_json

    def _main() -> dict[str, Any]:
        cik = int(company.cik)
        return download_json(f"{SEC_DATA_URL}/submissions/CIK{cik:010d}.json")

    main = _call_with_timeout(_main, _EDGAR_API_TIMEOUT_SECONDS)
    pages = (main.get("filings") or {}).get("files") or []
    found: list[Any] = []
    pages_read = 0
    for page in pages:
        if len(found) >= needed:
            break
        name = page.get("name") if isinstance(page, dict) else None
        if not name:
            continue

        def _page(name: str = str(name)) -> dict[str, Any]:
            return download_json(f"{SEC_DATA_URL}/submissions/{name}")

        data = _call_with_timeout(_page, _EDGAR_API_TIMEOUT_SECONDS)
        pages_read += 1
        rows = zip(
            data.get("form") or [],
            data.get("filingDate") or [],
            data.get("accessionNumber") or [], strict=False)
        reached = False
        for row_form, filing_date, accession in rows:
            reached = reached or (
                until_date is not None and str(filing_date) < until_date)
            if row_form != form:
                continue
            found.append(
                Filing(
                    cik=int(company.cik), company=str(company.name), form=form,
                    filing_date=str(filing_date), accession_no=str(accession)))
            if len(found) >= needed:
                break
        if reached:
            break
    return found, pages_read


def list_form_filings(company: Any, form: str, cap: int) -> FormListing:

    def _recent() -> list[Any]:
        listing = company.get_filings(form=form, trigger_full_load=False)
        return [f for f in (listing or []) if f.form == form][:cap]

    recent = _call_with_timeout(_recent, _EDGAR_API_TIMEOUT_SECONDS)
    if len(recent) >= cap:
        return FormListing(filings=recent)
    older, pages_read = _older_page_filings(company, form, cap - len(recent))
    return FormListing(filings=recent + older, older_pages_read=pages_read)


def _amendment_listing(company: Any, *, full_history: bool) -> list[Any]:
    listing = company.get_filings(
        form="10-K/A", trigger_full_load=full_history)
    return list(listing or [])


def filing_text(filing: Any) -> str:
    try:
        text = filing.sgml().text()
    except Exception as exc:
        logger.info(
            "sec_edgar_filings.sgml_text_failed",
            accession_no=str(getattr(filing, "accession_no", "")),
            error=repr(exc))
        text = None
    return str(text) if text else str(filing.text())


def _merged_amendment_text(
        company: Any, filing: Any, *, full_history: bool, ticker: str,
        filing_date: str) -> str | None:
    try:
        period_of_report = str(filing.period_of_report)

        def _fetch_amendments() -> list[Any]:
            return _amendment_listing(company, full_history=full_history)

        amendment_filings = _call_with_timeout(
            _fetch_amendments, _EDGAR_API_TIMEOUT_SECONDS)
        for amendment in amendment_filings:
            if str(amendment.period_of_report) != period_of_report:
                continue
            text = filing_text(amendment)
            logger.info(
                "sec_edgar_filings.real_amendment_found", ticker=ticker,
                filing_date=filing_date,
                amendment_filing_date=str(amendment.filing_date))
            return text
    except Exception as exc:
        logger.warning(
            "sec_edgar_filings.amendment_check_failed", ticker=ticker,
            filing_date=filing_date, error=repr(exc))
    return None


def _fetch_real_filings_sync(
        ticker: str,
        already_ingested_filenames: set[str]) -> list[FetchedFiling]:
    from edgar import Company, set_identity

    set_identity(settings.SEC_EDGAR_USER_AGENT)
    company = Company(ticker.upper())

    results: list[FetchedFiling] = []
    for form in _WANTED_FORMS:
        cap = _MAX_PER_FORM.get(form, 1)
        listing_started = time.monotonic()
        try:
            listing = list_form_filings(company, form, cap)
        except Exception as exc:
            logger.warning(
                "sec_edgar_filings.form_listing_failed", ticker=ticker,
                form=form, error=repr(exc),
                elapsed_seconds=round(time.monotonic() - listing_started, 1),
                timeout_seconds=_EDGAR_API_TIMEOUT_SECONDS)
            report_source_failure("sec_edgar")
            continue
        logger.info(
            "sec_edgar_filings.form_listed", ticker=ticker, form=form,
            found=len(listing.filings),
            older_pages_read=listing.older_pages_read,
            elapsed_seconds=round(time.monotonic() - listing_started, 1))
        filings = listing.filings

        target_window: list[tuple[str, str]] = []
        candidate_filings = {}
        for filing in filings:
            if len(target_window) >= cap:
                break
            if filing.form != form:
                continue
            filing_date = str(filing.filing_date)
            filename = f"{ticker.upper()}_{filing.form}_{filing_date}.txt"
            target_window.append((filing_date, filename))
            candidate_filings[filename] = filing

        for filing_date, filename in target_window:
            if filename in already_ingested_filenames:
                continue
            filing = candidate_filings[filename]
            try:
                raw_text = filing_text(filing)
            except Exception as exc:
                logger.warning(
                    "sec_edgar_filings.filing_text_fetch_failed",
                    ticker=ticker, form=form, filing_date=filing_date,
                    error=repr(exc))
                report_source_failure("sec_edgar")
                continue
            if len(raw_text.split()) < 100:
                logger.warning(
                    "sec_edgar_filings.filing_text_too_short", ticker=ticker,
                    form=form, filing_date=filing_date)
                continue

            amendment_text: str | None = None
            if form == "10-K":
                amendment_text = _merged_amendment_text(
                    company, filing, full_history=listing.older_pages_read > 0,
                    ticker=ticker, filing_date=filing_date)

            results.append((
                filename, form, filing_date, raw_text, amendment_text,
                _period_of_report(filing)))

    return results


def fetch_filing_text_sync(
        ticker: str, form: str,
        filing_date: str) -> tuple[str, str | None, str | None] | None:
    from edgar import Company, set_identity

    set_identity(settings.SEC_EDGAR_USER_AGENT)
    company = Company(ticker.upper())

    def _recent() -> list[Any]:
        listing = company.get_filings(form=form, trigger_full_load=False)
        return [f for f in (listing or []) if f.form == form]

    def _on_date(filings: list[Any]) -> Any:
        return next((f for f in filings if str(f.filing_date) == filing_date),
                    None)

    recent = _call_with_timeout(_recent, _EDGAR_API_TIMEOUT_SECONDS)
    filing = _on_date(recent)
    older_pages_read = 0
    if filing is None and not any(str(f.filing_date) < filing_date
                                  for f in recent):
        older, older_pages_read = _older_page_filings(
            company, form, sys.maxsize, until_date=filing_date)
        filing = _on_date(older)
    if filing is None:
        return None
    amendment_text = (
        _merged_amendment_text(
            company, filing, full_history=older_pages_read > 0, ticker=ticker,
            filing_date=filing_date) if form == "10-K" else None)
    return filing_text(filing), amendment_text, _period_of_report(filing)


def _period_of_report(filing: Any) -> str | None:
    try:
        value = str(getattr(filing, "period_of_report", "") or "")[:10]
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return value


async def list_new_filings(
        ticker: str, *,
        already_ingested_filenames: set[str]) -> list[NewFiling]:
    if not settings.SEC_EDGAR_USER_AGENT:
        logger.warning("sec_edgar_filings.user_agent_not_configured")
        return []

    try:
        raw_results = await asyncio.to_thread(
            _fetch_real_filings_sync, ticker, already_ingested_filenames)
    except Exception as exc:
        from edgar import CompanyNotFoundError

        if isinstance(exc, CompanyNotFoundError):
            logger.info("sec_edgar_filings.no_sec_filer", ticker=ticker)
            return []
        logger.exception("sec_edgar_filings.fetch_failed", ticker=ticker)
        report_source_failure("sec_edgar")
        return []

    raw_results = sorted(raw_results, key=lambda r: (r[2], r[0]))
    new_filings: list[NewFiling] = []
    for row in raw_results:
        (filename, form, filing_date, raw_text, amendment_text,
         period_of_report) = row
        if period_of_report is None:
            found = await filing_period(ticker, form, filing_date)
            period_of_report = found.report_date if found else None
        try:
            await upload_bytes(
                settings, bucket=settings.S3_BUCKET_DOCUMENTS,
                key=f"raw_filings/{ticker}/{filename}",
                content=raw_text.encode("utf-8"), content_type="text/plain")
        except Exception:
            logger.warning(
                "sec_edgar_filings.raw_archive_failed", ticker=ticker,
                form=form, filing_date=filing_date)
        if amendment_text is not None:
            try:
                await upload_bytes(
                    settings, bucket=settings.S3_BUCKET_DOCUMENTS,
                    key=f"raw_filings/{ticker}/{filename}.amendment.txt",
                    content=amendment_text.encode("utf-8"),
                    content_type="text/plain")
            except Exception:
                logger.warning(
                    "sec_edgar_filings.amendment_raw_archive_failed",
                    ticker=ticker, form=form, filing_date=filing_date)
        try:
            summary = await summarize_filing(
                raw_text, form=form, ticker=ticker, filing_date=filing_date,
                amendment_text=amendment_text)
        except FilingTooLargeError as exc:
            logger.warning(
                "sec_edgar_filings.filing_too_large_skipped", ticker=ticker,
                form=form, filing_date=filing_date, error=str(exc))
            continue
        is_fallback = is_fallback_summary(summary.text)
        extracted = None
        if not is_fallback:
            try:
                extracted = await extract_filing_records(
                    raw_text, form=form, ticker=ticker,
                    filing_date=filing_date, amendment_text=amendment_text,
                    report_date=period_of_report,
                    tier=LLMTier(settings.FILING_LLM_TIER))
            except Exception as exc:
                logger.warning(
                    "sec_edgar_filings.records_failed", ticker=ticker,
                    form=form, filing_date=filing_date, error=repr(exc))
        new_filings.append(
            NewFiling(
                filename=filename, text=summary.text, form=form,
                filing_date=filing_date, is_fallback=is_fallback,
                records=extracted.records if extracted is not None else None,
                report_date=period_of_report))
        if filename != raw_results[-1][0]:
            await asyncio.sleep(3)

    return new_filings


@dataclass
class FilingIngestResult:
    new_filings: list[NewFiling]
    documents_created: int = 0
    failed_filenames: list[str] = field(default_factory=list)


_TrackedIngest = tuple[FilingIngestResult, list[str]]
_ingests_in_flight: dict[str, "asyncio.Task[_TrackedIngest]"] = {}


def _forget_finished_ingest(key: str, task: "asyncio.Task[Any]") -> None:
    if _ingests_in_flight.get(key) is task:
        del _ingests_in_flight[key]
    if not task.cancelled():
        task.exception()


async def ingest_new_filings(ticker: str) -> FilingIngestResult:
    key = ticker.upper()
    task = _ingests_in_flight.get(key)
    if task is None:
        task = asyncio.create_task(_tracked_ingest(ticker))
        _ingests_in_flight[key] = task
        task.add_done_callback(lambda done: _forget_finished_ingest(key, done))
    else:
        logger.info("sec_filings.ingest_joined", ticker=key)
    result, failures = await asyncio.shield(task)
    for source in failures:
        report_source_failure(source)
    return result


async def _tracked_ingest(ticker: str) -> _TrackedIngest:
    with track_source_failures() as failures:
        result = await _ingest_new_filings(ticker)
    return result, list(failures)


async def _ingest_new_filings(ticker: str) -> FilingIngestResult:
    existing = set(
        await
        get_existing_document_filenames(ticker, DocumentCategory.FILING.value))
    new_filings = await list_new_filings(
        ticker, already_ingested_filenames=existing)
    result = FilingIngestResult(new_filings=new_filings)
    for filing in new_filings:
        if filing.is_fallback:
            logger.warning(
                "sec_filings.fallback_not_persisted", ticker=ticker,
                filename=filing.filename)
            report_source_failure("filing_summary")
            continue
        try:
            await create_system_document(
                filename=filing.filename, content=filing.text,
                category=DocumentCategory.FILING.value, ticker=ticker,
                content_date=filing.filing_date)
        except Exception as exc:
            logger.warning(
                "sec_filings.document_create_failed", ticker=ticker,
                filename=filing.filename, error=str(exc))
            result.failed_filenames.append(filing.filename)
            continue
        result.documents_created += 1
        logger.info(
            "sec_filings.document_created", ticker=ticker,
            filename=filing.filename, form=filing.form)
        forget_filing_caches(ticker)
        await _store_records(ticker, filing)
    return result


async def _store_records(ticker: str, filing: NewFiling) -> None:
    if filing.records is None:
        return
    report_date = filing.report_date
    if report_date is None:
        period = await filing_period(ticker, filing.form, filing.filing_date)
        report_date = period.report_date if period else None
    await store_filing_records(
        ticker=ticker, filename=filing.filename, form=filing.form,
        filing_date=filing.filing_date, report_date=report_date,
        records=filing.records)
