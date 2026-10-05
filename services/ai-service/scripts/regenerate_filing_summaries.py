import argparse
import asyncio
import sys
from pathlib import Path
from ai.knowledge.filing_records import parse_filing_filename
from ai.knowledge.filing_summary import is_fallback_summary, summarize_filing
from clients.core_client import (create_system_document,
                                 get_existing_document_filenames)
from database import SessionLocal
from scripts.backfill_filing_records import _raw_text
from sqlalchemy import text
from shared.enums import DocumentCategory

OUT_DIR = Path("/app/_regenerated")


async def _document_id(ticker: str, filename: str) -> str | None:
    async with SessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT id FROM core.documents WHERE ticker = :ticker "
                "AND category = :category AND filename = :filename"), {
                    "ticker": ticker,
                    "category": DocumentCategory.FILING.value,
                    "filename": filename
                })
        value = result.scalar_one_or_none()
    return str(value) if value is not None else None


async def generate(filename: str, out_dir: Path = OUT_DIR) -> bool:
    parsed = parse_filing_filename(filename)
    if parsed is None:
        print(f"  SKIPPED {filename}: not a filing document name")
        return False
    ticker, form, filing_date = parsed
    source = await _raw_text(ticker, filename, form, filing_date)
    if source is None:
        print(f"  FAILED {filename}: raw text not found (archive or EDGAR)")
        return False
    raw, amendment, _period = source
    summary = await summarize_filing(
        raw, form=form, ticker=ticker, filing_date=filing_date,
        amendment_text=amendment)
    if is_fallback_summary(summary.text):
        print(
            f"  FAILED {filename}: the summary call failed (fallback excerpt)")
        return False
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{filename}.summary.txt").write_text(
        summary.text, encoding="utf-8")
    flag = " OUTPUT STILL TRUNCATED" if summary.truncated else ""
    doc_id = await _document_id(ticker, filename)
    print(
        f"  saved {filename}: {len(summary.text):,} characters{flag}; "
        f"current document id {doc_id or 'none'}")
    return not summary.truncated


async def apply(filename: str, out_dir: Path = OUT_DIR) -> bool:
    parsed = parse_filing_filename(filename)
    summary_path = out_dir / f"{filename}.summary.txt"
    if parsed is None or not summary_path.exists():
        print(
            f"  SKIPPED {filename}: no saved summary (run without --apply "
            f"first)")
        return False
    ticker, _form, filing_date = parsed
    existing = await get_existing_document_filenames(
        ticker, DocumentCategory.FILING.value)
    if filename in existing:
        print(
            f"  SKIPPED {filename}: the old document still exists -- delete "
            f"it "
            "first with core-service's scripts.delete_documents")
        return False
    await create_system_document(
        filename=filename, content=summary_path.read_text(encoding="utf-8"),
        category=DocumentCategory.FILING.value, ticker=ticker,
        content_date=filing_date)
    print(f"  stored {filename} as a new filing document")
    return True


async def run(filenames: list[str], *, do_apply: bool) -> int:
    print(
        "=== APPLY ==="
        if do_apply else "=== GENERATE (no database writes) ===")
    failed = 0
    for filename in filenames:
        try:
            ok = await (apply(filename) if do_apply else generate(filename))
        except Exception as exc:
            print(f"  FAILED {filename}: {type(exc).__name__}: {exc}")
            ok = False
        failed += 0 if ok else 1
    print(f"done: {len(filenames) - failed} ok, {failed} not ok")
    return failed


DESCRIPTION = "Regenerate the stored summary of chosen filing documents."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--filename", action="append", required=True,
        help="filing document name, e.g. KO_10-K_2026-02-20.txt (repeatable)")
    parser.add_argument(
        "--apply", action="store_true",
        help="store the saved summaries as the filing documents")
    args = parser.parse_args(argv)
    return 1 if asyncio.run(run(args.filename, do_apply=args.apply)) else 0


if __name__ == "__main__":
    sys.exit(main())
