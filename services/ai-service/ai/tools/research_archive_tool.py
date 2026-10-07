from typing import Any, ClassVar
import structlog
from ai.agents.evidence_grounding import non_english_reason
from ai.rag.retriever import BaseRetriever, RetrievedChunk
from ai.rag.visibility import SYSTEM_ONLY_SCOPE, RetrievalVisibility
from ai.tools.base import Tool, age_in_days
from shared.enums import DocumentCategory

logger = structlog.get_logger(__name__)

_STALE_THRESHOLD_DAYS: dict[DocumentCategory, int] = {
    DocumentCategory.ANALYST_REPORT: 100,
    DocumentCategory.RISK_ANALYSIS: 180,
    DocumentCategory.MACRO_RESEARCH: 45,
    DocumentCategory.NEWS: 3
}

_CATEGORY_LABEL: dict[DocumentCategory, str] = {
    DocumentCategory.ANALYST_REPORT: "prior analysis",
    DocumentCategory.RISK_ANALYSIS: "prior risk note",
    DocumentCategory.MACRO_RESEARCH: "prior macro note",
    DocumentCategory.EARNINGS_CALL: "earnings call transcript",
    DocumentCategory.NEWS: "prior news note"
}


def _stale_flag(category: DocumentCategory, content_date: str) -> str:
    threshold = _STALE_THRESHOLD_DAYS.get(category)
    if threshold is None:
        return ""
    age_days = age_in_days(content_date)
    if age_days is None or age_days <= threshold:
        return ""
    return (
        f" [STALE -- this {_CATEGORY_LABEL.get(category, 'archived note')} is "
        f"~{age_days} days old, check for a more recent one before treating "
        "it as current]")


AGENT_NOTE_GUIDANCE = (
    "Note: passages tagged [agent:...] are PRIOR AI-WRITTEN NOTES from "
    "earlier AlphaMind runs (dated as shown). They are unverified and have "
    "repeated wrong facts before (dates, deal timings, case status). Use "
    "them only as leads for what to check: before you state any fact, "
    "figure, date or status that appears in one, confirm it in a fresh tool "
    "result from this run (filings, news, web search) and cite that source; "
    "if fresh data differs, the fresh data wins. Never restate a prior note "
    "as a current fact. Other passages are research documents: when you use "
    "a view from one, give its date (or say it is undated) and do not "
    "present it as the current situation for a later period.")


def _prior_note_label(chunk: RetrievedChunk) -> str:
    written = (
        f" written {chunk.content_date[:10]}" if chunk.content_date else "")
    return (
        f" [PRIOR AI-WRITTEN NOTE{written} -- unverified lead, confirm with "
        "fresh tool data before stating any fact from it]")


def _is_agent_note(chunk: RetrievedChunk) -> bool:
    return bool(chunk.source) and str(chunk.source).startswith("agent:")


def _date_part(category: DocumentCategory, chunk: RetrievedChunk) -> str:
    if category == DocumentCategory.EARNINGS_CALL:
        if chunk.content_date:
            return f" (call date {chunk.content_date[:10]})"
        return " (call date not recorded; the file name may give the quarter)"
    return f" ({chunk.content_date[:10]})" if chunk.content_date else ""


def _format_chunk(category: DocumentCategory, chunk: RetrievedChunk) -> str:
    source = f" [{chunk.source}]" if chunk.source else ""
    date_part = _date_part(category, chunk)
    stale = (
        _stale_flag(category, chunk.content_date)
        if chunk.content_date else "")
    note_label = _prior_note_label(chunk) if _is_agent_note(chunk) else ""
    return (
        f"- "
        f"({chunk.ticker or 'unknown ticker'}){source}{date_part}{note_label}"
        f"{stale}: "
        f"{chunk.text}")


class SearchResearchArchiveTool(Tool):
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
            self, *, name: str, description: str, category: DocumentCategory,
            ticker: str | None, retriever: BaseRetriever | None = None,
            visibility: RetrievalVisibility = SYSTEM_ONLY_SCOPE) -> None:
        self.name = name
        self.description = description
        self.category = category
        self.ticker = ticker
        self.retriever = retriever or BaseRetriever(
            category_filter=category, visibility=visibility)

    async def run(  # type: ignore[override]
            self, query: str, ticker: str | None = None) -> str:
        if ticker is not None and (self.ticker is None
                                   or ticker.strip().upper()
                                   != self.ticker.upper()):
            logger.info(
                "research_archive_tool.ticker_argument_ignored",
                tool=self.name, requested=ticker, bound=self.ticker)
        effective_ticker = self.ticker
        try:
            chunks = await self.retriever.retrieve(
                query, ticker=effective_ticker)
        except Exception as exc:
            return f"Research archive search failed for {query!r}: {exc}"

        english_chunks = [
            c for c in chunks if non_english_reason(c.text) is None
        ]
        if len(english_chunks) != len(chunks):
            logger.info(
                "research_archive_tool.non_english_passages_dropped",
                query=query, dropped=len(chunks) - len(english_chunks),
                total=len(chunks))
        chunks = english_chunks

        if not chunks:
            scope_note = (
                f" (ticker={effective_ticker!r})" if effective_ticker else "")
            if self.category == DocumentCategory.EARNINGS_CALL:
                return (
                    f"No earnings call transcript passages found for "
                    f"{query!r}{scope_note} (usually none has been uploaded "
                    "for this ticker, which is expected; do not retry).")
            return (
                f"No "
                f"{_CATEGORY_LABEL.get(self.category, self.category.value)} "
                f"passages found for {query!r}{scope_note}. This may mean "
                "nothing has been archived yet for this query, or the query "
                "didn't match anything relevant.")

        label = _CATEGORY_LABEL.get(self.category, self.category.value)
        lines = [
            f"{label.capitalize()} search results for {query!r} "
            f"({len(chunks)} passages):"
        ]
        agent_notes = sum(1 for chunk in chunks if _is_agent_note(chunk))
        if agent_notes:
            lines.append(AGENT_NOTE_GUIDANCE)
            logger.info(
                "research_archive_tool.prior_notes_labelled", tool=self.name,
                prior_notes=agent_notes, total=len(chunks))
        lines.extend(_format_chunk(self.category, chunk) for chunk in chunks)
        return "\n".join(lines)
