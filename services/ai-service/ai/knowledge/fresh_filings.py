import structlog
from ai.ingestion.sec_edgar_filings import ingest_new_filings

logger = structlog.get_logger(__name__)


async def check_for_fresh_filing_content(ticker: str) -> str | None:
    try:
        new_filings = (await ingest_new_filings(ticker)).new_filings
    except Exception as exc:
        logger.warning(
            "fresh_filing_check.sec_check_failed", ticker=ticker,
            error=str(exc))
        return None

    if not new_filings:
        return None

    newest = max(new_filings, key=lambda filing: str(filing.filing_date))
    older = len(new_filings) - 1
    if older:
        logger.info(
            "fresh_filing_check.older_new_filings_not_pasted", ticker=ticker,
            count=older, pasted=newest.filename)
    others = (
        f" {older} older new filing(s) for {ticker} are being indexed too and "
        "will be searchable with search_filings soon." if older else "")
    logger.info(
        "fresh_filing_check.found_new_content", ticker=ticker,
        count=len(new_filings))
    return (
        f"A real {newest.form} filed on {newest.filing_date} for {ticker} "
        f"(freshly fetched; it may not be in the searchable filing corpus "
        f"yet -- search_filings may still have OTHER, older filing content "
        f"worth checking too; where an older filing says something different "
        f"on the same topic, this newer filing wins).{others}\n{newest.text}")
