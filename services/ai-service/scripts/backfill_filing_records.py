import argparse
import asyncio
import sys
from collections.abc import Sequence
from ai.ingestion.sec_edgar_filings import fetch_filing_text_sync
from ai.knowledge.filing_records import (parse_filing_filename,
                                         store_filing_records)
from ai.knowledge.filing_summary import extract_filing_records
from ai.llm.base import LLMTier
from ai.providers.sec_edgar import filing_period
from clients.core_client import get_existing_document_filenames
from config import settings
from database import SessionLocal
from sqlalchemy import text
from domains.filings.repository import FilingRecordRepository
from shared.enums import DocumentCategory
from shared.storage import download_bytes

LARGE_FILING_CHARS = 500_000
IDLE_WAIT_SECONDS = 65.0


async def _archived_text(ticker: str, key_name: str) -> str | None:
    try:
        data = await download_bytes(
            settings, bucket=settings.S3_BUCKET_DOCUMENTS,
            key=f"raw_filings/{ticker}/{key_name}")
    except Exception:
        return None
    return data.decode("utf-8", errors="replace")


async def _raw_text(
        ticker: str, filename: str, form: str,
        filing_date: str) -> tuple[str, str | None, str | None] | None:
    raw = await _archived_text(ticker, filename)
    amendment = await _archived_text(ticker, f"{filename}.amendment.txt")
    period: str | None = None
    if raw is None:
        fetched = await asyncio.to_thread(
            fetch_filing_text_sync, ticker, form, filing_date)
        if fetched is None:
            return None
        raw, fetched_amendment, period = fetched
        amendment = amendment if amendment is not None else fetched_amendment
    if period is None:
        found = await filing_period(ticker, form, filing_date)
        period = found.report_date if found else None
    return raw, amendment, period


async def _pending(
    ticker: str, *, redo: bool = False, latest: int | None = None,
    filenames: Sequence[str] = ()
) -> list[tuple[str, str, str]]:
    documents = await get_existing_document_filenames(
        ticker, DocumentCategory.FILING.value)
    filings: list[tuple[str, str, str]] = []
    for filename in documents:
        if filenames and filename not in filenames:
            continue
        parsed = parse_filing_filename(filename)
        if parsed is not None:
            filings.append((filename, parsed[1], parsed[2]))
    filings.sort(key=lambda p: (p[2], p[0]))
    if latest is not None:
        filings = filings[-latest:] if latest > 0 else []
    done: set[str] = set()
    if not redo:
        async with SessionLocal() as session:
            done = await FilingRecordRepository(session).filenames_for_ticker(
                ticker)
    return [f for f in filings if f[0] not in done]


async def filing_tickers() -> list[str]:
    async with SessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT DISTINCT ticker FROM core.documents "
                "WHERE category = :category AND ticker IS NOT NULL ORDER BY "
                "ticker"), {"category": DocumentCategory.FILING.value})
        return [str(t) for t in result.scalars().all()]


async def filings_without_records() -> list[str]:
    missing: list[str] = []
    for ticker in await filing_tickers():
        missing.extend(filename for filename, *_ in await _pending(ticker))
    return missing


def _filename_tickers(filenames: Sequence[str]) -> list[str]:
    tickers: list[str] = []
    for filename in filenames:
        parsed = parse_filing_filename(filename)
        if parsed is not None and parsed[0].upper() not in tickers:
            tickers.append(parsed[0].upper())
    return tickers


async def run(
        tickers: list[str], *, apply: bool, redo: bool = False,
        latest: int | None = None, tier: LLMTier | None = None,
        filenames: Sequence[str] = ()) -> dict[str, int]:
    print(
        "=== APPLY ==="
        if apply else "=== DRY RUN (no LLM, SEC or database writes) ===")
    counts = {"pending": 0, "stored": 0, "failed": 0}
    matched: set[str] = set()
    for ticker in tickers:
        pending = await _pending(
            ticker, redo=redo, latest=latest, filenames=filenames)
        matched.update(p[0] for p in pending)
        counts["pending"] += len(pending)
        label = "to re-extract" if redo else "without records"
        print(f"{ticker}: {len(pending)} filing(s) {label}")
        for filename, form, filing_date in pending:
            if not apply:
                print(f"  would process {filename}")
                continue
            try:
                source = await _raw_text(ticker, filename, form, filing_date)
                if source is None:
                    raise RuntimeError("raw text not found (archive or EDGAR)")
                raw, amendment, period = source
                size = len(raw) + len(amendment or "")
                if size > LARGE_FILING_CHARS:
                    print(
                        f"  waiting {IDLE_WAIT_SECONDS:.0f}s before "
                        f"{filename} ({size:,} characters)")
                    await asyncio.sleep(IDLE_WAIT_SECONDS)
                extracted = await extract_filing_records(
                    raw, form=form, ticker=ticker, filing_date=filing_date,
                    amendment_text=amendment, report_date=period, tier=tier)
                if extracted is None:
                    raise RuntimeError(
                        "no complete records from the LLM call (see the "
                        "filing_summary log; the stored row is kept)")
                stored = await store_filing_records(
                    ticker=ticker, filename=filename, form=form,
                    filing_date=filing_date, report_date=period,
                    records=extracted.records)
                if not stored:
                    raise RuntimeError("database write failed")
                sizes = ", ".join(
                    f"{k}={len(v)}" for k, v in extracted.records.items())
            except Exception as exc:
                counts["failed"] += 1
                print(f"  FAILED {filename}: {type(exc).__name__}: {exc}")
                continue
            counts["stored"] += 1
            print(
                f"  stored {filename} (period {period or 'unknown'}; {sizes}; "
                f"served by {extracted.served_by})")
    for filename in filenames:
        if filename not in matched:
            print(
                f"  NOT FOUND {filename}: no such filing document to process "
                "(check the name and --ticker; without --redo a filing that "
                "already has records is skipped)")
    print(f"done: {counts}")
    return counts


DESCRIPTION = (
    "Extract `ai.filing_records` rows for stored filing documents that have "
    "none\n"
    "(for example when the records call failed at ingestion time).\n"
    "`--redo` re-extracts filings that already have a row. Each filing is\n"
    "extracted on its own (no previous-filing input), so one filing can be "
    "redone\n"
    "alone with `--filename`.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--ticker", action="append", default=[], help=(
            "ticker to process (repeatable; default: every ticker with "
            "filings)"))
    parser.add_argument(
        "--redo", action="store_true", help=(
            "re-extract filings that already have records (replaces the rows)"
        ))
    parser.add_argument(
        "--latest", type=int, default=None,
        help="only each ticker's N newest filings (without --redo: those "
        "of them without records)")
    parser.add_argument(
        "--filename", action="append", default=[],
        help="only this filing document, e.g. AMZN_10-Q_2026-07-31.txt "
        "(repeatable; combine with --ticker and --redo to redo exact filings)")
    parser.add_argument(
        "--tier", choices=[t.value for t in LLMTier], default=None,
        help="LLM tier of the records call (default: "
        "FILING_RECORDS_BACKFILL_LLM_TIER)")
    parser.add_argument(
        "--apply", action="store_true",
        help="make the LLM calls and store the rows (default: dry run)")
    args = parser.parse_args(argv)

    async def _main() -> int:
        tickers = ([t.upper() for t in args.ticker]
                   or _filename_tickers(args.filename)
                   or await filing_tickers())
        counts = await run(
            tickers, apply=args.apply, redo=args.redo, latest=args.latest,
            tier=LLMTier(args.tier) if args.tier else None,
            filenames=args.filename)
        return 1 if counts["failed"] else 0

    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
