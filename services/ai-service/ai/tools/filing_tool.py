from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ClassVar
import structlog
from ai.agents.evidence_grounding import non_english_reason
from ai.knowledge.filing_records import (load_filing_records,
                                         parse_filing_filename,
                                         render_filing_records)
from ai.providers.sec_edgar import (FilingPeriod, filing_period,
                                    latest_quarter_payload)
from ai.rag.retriever import FilingRetriever, RetrievedChunk
from ai.rag.visibility import SYSTEM_ONLY_SCOPE, RetrievalVisibility
from ai.tools.base import Tool, age_in_days

logger = structlog.get_logger(__name__)

PeriodLookup = Callable[[str, str, str], Awaitable[FilingPeriod | None]]
RecordsLoader = Callable[[str], Awaitable[Sequence[Any]]]
QuarterLookup = Callable[[str], Awaitable[dict[str, Any] | None]]

_STALE_FILING_THRESHOLD_DAYS = 450


def _stale_filing_flag(content_date: str) -> str:
    age_days = age_in_days(content_date)
    if age_days is None or age_days <= _STALE_FILING_THRESHOLD_DAYS:
        return ""
    years = age_days / 365
    return (
        f" [STALE FILING -- ~{years:.1f} years old, check for a more "
        "recent filing before treating this as current]")


class SearchFilingsTool(Tool):
    name = "search_filings"
    description = (
        "Search uploaded SEC filings (10-K/10-Q) of the company under "
        "analysis. Results list the newest filing first, each passage with "
        "its form, filing date and fiscal period (from SEC EDGAR); the first "
        "result also lists the latest quarter's SEC XBRL figures and the "
        "structured records (unusual items, guidance, transactions, legal "
        "matters) of the newest filings. "
        'action_input: {"query": "risk factors related to supply '
        'chain"}')
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string"
            }
        },
        "required": ["query"],
        "additionalProperties": False
    }

    def __init__(
            self, *, ticker: str, retriever: FilingRetriever | None = None,
            visibility: RetrievalVisibility = SYSTEM_ONLY_SCOPE,
            period_lookup: PeriodLookup | None = None,
            records_loader: RecordsLoader | None = None,
            quarter_lookup: QuarterLookup | None = None) -> None:
        self.ticker = ticker
        self._periods: dict[str, FilingPeriod | None] = {}
        self._period_lookup = period_lookup or filing_period
        self._records_loader = records_loader or load_filing_records
        self._quarter_lookup = quarter_lookup or latest_quarter_payload
        self._header_shown = False
        self.retriever = retriever or FilingRetriever(visibility=visibility)

    async def run(  # type: ignore[override]
            self, query: str, ticker: str | None = None) -> str:
        if (ticker is not None
                and ticker.strip().upper() != self.ticker.upper()):
            logger.info(
                "filing_tool.ticker_argument_ignored", requested=ticker,
                bound=self.ticker)
        ticker = self.ticker
        try:
            chunks = await self.retriever.retrieve(query, ticker=ticker)
        except Exception as exc:
            return f"Filing search failed for {query!r}: {exc}"

        english_chunks = [
            c for c in chunks if non_english_reason(c.text) is None
        ]
        if len(english_chunks) != len(chunks):
            logger.info(
                "filing_tool.non_english_passages_dropped", query=query,
                dropped=len(chunks) - len(english_chunks), total=len(chunks))
        chunks = english_chunks

        header = await self._header()
        if not chunks:
            return header + (
                f"No filing passages found for {query!r} (ticker={ticker!r})"
                ". This may mean no filings have been indexed yet for this "
                "ticker, or the query didn't match anything relevant.")

        await self._load_periods(chunks)
        return header + _render_passages(query, chunks, periods=self._periods)

    async def _header(self) -> str:
        if self._header_shown:
            return (
                "(Latest-quarter SEC XBRL figures and filing records were "
                "listed with this tool's first result.)\n")
        parts: list[str] = []
        try:
            quarter = await self._quarter_lookup(self.ticker)
        except Exception as exc:
            logger.warning("filing_tool.latest_quarter_failed", error=str(exc))
            quarter = None
        if quarter:
            parts.append(str(quarter["text"]))
        try:
            rows = await self._records_loader(self.ticker)
        except Exception as exc:
            logger.warning("filing_tool.records_failed", error=str(exc))
            rows = []
        block = render_filing_records(rows)
        if block:
            parts.append(block)
        logger.info(
            "filing_tool.header_rendered", ticker=self.ticker,
            latest_quarter=bool(quarter), record_filings=len(rows))
        self._header_shown = bool(parts)
        return "\n\n".join(parts) + "\n\n" if parts else ""

    async def _load_periods(self, chunks: list[RetrievedChunk]) -> None:
        for chunk in chunks:
            key = _document_key(chunk)
            if key in self._periods:
                continue
            parsed = parse_filing_filename(chunk.source or "")
            if parsed is None:
                self._periods[key] = None
                continue
            try:
                period = await self._period_lookup(
                    self.ticker, parsed[1], parsed[2])
            except Exception as exc:
                logger.warning(
                    "filing_tool.period_lookup_failed", source=key,
                    error=str(exc))
                period = None
            self._periods[key] = period
            logger.info(
                "filing_tool.period_from_edgar", source=key,
                report_date=period.report_date if period else None)


NEWEST_FILING_GUIDANCE = (
    "Passages are ordered newest filing first. The newest filing wins: when "
    "an older filing says something different on the same topic (quarterly "
    "results, a lawsuit's status, a deal or divestiture, guidance), use the "
    "newer one. Forward guidance in an older filing is history, not the "
    "current outlook -- give its filing date if you mention it. State a "
    "legal matter's status as of the newest filing that mentions it.")


def _document_key(chunk: RetrievedChunk) -> str:
    return chunk.source or chunk.document_id or chunk.chunk_id


def _form_of(chunk: RetrievedChunk) -> str | None:
    parsed = parse_filing_filename(chunk.source or "")
    return parsed[1] if parsed else None


def period_label(period: FilingPeriod | None) -> str:
    if period is None:
        return "not available from SEC EDGAR"
    form = period.form.removesuffix("/A")
    if form == "10-Q":
        return f"quarter ended {period.report_date}"
    if form in ("10-K", "20-F", "40-F"):
        return f"fiscal year ended {period.report_date}"
    return f"period ended {period.report_date}"


def _render_passages(
        query: str, chunks: list[RetrievedChunk], *,
        periods: dict[str, FilingPeriod | None] | None = None) -> str:
    ordered = sorted(
        chunks, key=lambda c: (c.content_date or "")[:10], reverse=True)
    periods = periods or {}
    newest = (ordered[0].content_date or "")[:10] if ordered else ""
    newest_form = (
        next((
            _form_of(c) for c in ordered
            if (c.content_date or "")[:10] == newest and _form_of(c)), None)
        or "filing")
    older = 0
    lines = [
        f"Filing search results for {query!r} ({len(chunks)} passages):",
        NEWEST_FILING_GUIDANCE
    ]
    for chunk in ordered:
        source = f" [{chunk.source}]" if chunk.source else ""
        filed = (chunk.content_date or "")[:10]
        date_part = f" (filed {filed})" if filed else ""
        form = _form_of(chunk)
        period = period_label(periods.get(_document_key(chunk)))
        if not filed:
            recency = "filing date unknown"
        elif filed == newest:
            recency = "NEWEST filing in these results"
        else:
            older += 1
            recency = (
                f"OLDER filing -- may be superseded by the {newest_form} "
                f"filed {newest}")
        label = f" [{form or 'form unknown'}; period: {period}; {recency}]"
        stale_flag = (
            _stale_filing_flag(chunk.content_date)
            if chunk.content_date else "")
        lines.append(
            f"- ({chunk.ticker or 'unknown ticker'}){source}{date_part}{label}"
            f"{stale_flag}: {chunk.text}")
    logger.info(
        "filing_tool.passages_ordered_newest_first", query=query,
        passages=len(ordered), newest_filed=newest or None,
        older_passages=older)
    return "\n".join(lines)
