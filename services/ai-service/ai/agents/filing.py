from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any
import structlog
from ai.agents.finish_checks import (OWN_EVIDENCE_CHECK,
                                     InternalTokenValidator,
                                     own_evidence_figure_check)
from ai.agents.loop import (BaseAgent, ReActAgentLoop, combine_context,
                            prefetch_tool_calls)
from ai.graph.state import AgentOutput, AnalysisState
from ai.knowledge.filing_records import (OWN_PERIOD_MONTHS, display_amounts,
                                         load_filing_records, ordered_rows,
                                         period_months_value, quote_evidence,
                                         record_by_id, record_items,
                                         unusual_item_period)
from ai.rag.visibility import visibility_for_state
from ai.tools.filing_tool import SearchFilingsTool
from ai.tools.knowledge_graph_tool import QueryKnowledgeGraphTool

logger = structlog.get_logger(__name__)

MAX_ITERATIONS = 5
PREFETCH_FILING_QUERY = (
    "risk factors OR recent results OR guidance OR outlook OR Item 1A")

_SYSTEM_PROMPT = (
    "You are the Filing Agent in AlphaMind's multi-agent investment research "
    "system. Your job is to extract risk factors, recent results, and forward "
    "guidance from a company's uploaded SEC filings (10-K/10-Q). If "
    "search_filings returns no relevant passages, say so plainly in your "
    "final answer rather than fabricating filing content. If you were given "
    "a recently-fetched filing summary as extra context up front, it covers "
    "only the newest filing(s): you must still call search_filings at least "
    "once before finishing.\n\n"
    "Use the code-rendered values and the filing records. The first "
    "search_filings result lists the latest quarter's figures computed from "
    "SEC XBRL (with the year-ago quarter, the items below operating income "
    "and, for a 10-Q, the year to date and its cash flow) and the structured "
    "filing records (unusual items, guidance, transactions, legal matters; "
    "newest filing first). Report the latest quarter's "
    "results as that line states them, with their period and year-over-year "
    "change; when its title says the newer 10-Q or 10-K is not in SEC XBRL "
    "yet, it is the quarter before the newest filing, so take the newest "
    "quarter from the newest filing's passages. The records are extracted "
    "automatically and can be wrong or out of date: state each legal "
    "matter's or deal's status as the newest "
    "filing text you were shown supports (the record's quoted sentence or a "
    "filing passage). Guidance from an older filing is history: "
    "give its filing date and the period it covers. Name an unusual item "
    "next to the figure it affects, as its record states it (an unusual "
    "item is recorded in that period's results and may recur in later "
    "periods; do not call it one-time unless the filing does).\n\n"
    "Every other figure you state must carry its period in plain words next "
    "to it (e.g. 'Q2 2026 revenue', 'net income for the nine months ended "
    "June 30, 2026', 'total assets as of June 30, 2026'), taken from the "
    "passage's own labelling; if a passage does not label the period, say "
    "the period is not stated ('Three Months Ended' is a quarter; 'Six "
    "Months Ended' or 'Nine Months Ended' is six or nine months, never "
    "annual). Never combine a quarterly figure with an annual one into one "
    "derived number.\n\n"
    "The record ids (F1, F1.L2 and the like) are for you, not the reader: "
    "never write one in your answer; name a filing by its form and filing "
    'date (e.g. "the 10-Q filed 2026-07-23").')

_FILING_VALIDATION_SUFFIX = (
    "Fix only what the check above names, keep every other correct fact, "
    "then set finish_answer again.")


class FilingRecordsCache:

    def __init__(self) -> None:
        self.rows: list[Any] | None = None
        self.shown = False

    async def __call__(self, ticker: str) -> Sequence[Any]:
        self.shown = True
        return await self.load(ticker)

    async def load(self, ticker: str) -> list[Any]:
        if self.rows is None:
            self.rows = list(await load_filing_records(ticker))
        return self.rows


_ONE_OFF_ITEMS_MAX = 6


def one_off_item_lines(rows: Sequence[Any]) -> list[str]:
    lines: list[str] = []
    for number in range(1, _ONE_OFF_ITEMS_MAX + 1):
        record = record_by_id(rows, f"F1.O{number}")
        if record is None:
            break
        lines.append(
            display_amounts(
                f"{record['description']}: {record['amount_text']}"
                f"{unusual_item_period(record)} "
                f"[{record['id']}, {record['form']} filed "
                f"{record['filing_date']}]"
                f"{quote_evidence(record)}"))
    return lines


async def one_off_items_for(
        ticker: str, cache: FilingRecordsCache, *, role: str) -> list[str]:
    return await _lines_for(
        one_off_item_lines, ticker, cache, role=role,
        event="filing.one_off_items_failed")


_KEY_TRANSACTION_TYPES = ("financing", "guarantee")
_OPEN_TRANSACTION_STATUSES = ("announced", "pending")


def _text(item: dict[str, Any], name: str) -> str:
    return " ".join(str(item.get(name) or "").split())


def _legal_key_line(item: dict[str, Any]) -> str | None:
    exposure = _text(item, "exposure_text")
    if not exposure:
        return None
    when = (
        f" ({_text(item, 'status_date')})"
        if _text(item, "status_date") else "")
    return (
        f"legal matter: {_text(item, 'name')} -- status "
        f"{_text(item, 'status')}"
        f"{when}; amount stated in the filing (claimed, sought, assessed or "
        f"awarded): {exposure}.")


def _transaction_key_line(item: dict[str, Any]) -> str | None:
    if (_text(item, "type") not in _KEY_TRANSACTION_TYPES
            and _text(item, "status") not in _OPEN_TRANSACTION_STATUSES):
        return None
    when = f" ({_text(item, 'date')})" if _text(item, "date") else ""
    parts = [
        f"transaction: {_text(item, 'name')} ({_text(item, 'type')}) -- "
        f"status "
        f"{_text(item, 'status')}{when}"
    ]
    if _text(item, "detail"):
        parts.append(_text(item, "detail").rstrip("."))
    return "; ".join(parts) + "."


def _guidance_key_line(item: dict[str, Any]) -> str:
    covers = (
        f" (period covered: {_text(item, 'period_covered')})" if _text(
            item, "period_covered") else "")
    return (
        f"guidance: {_text(item, 'metric')}: {_text(item, 'value_text')}"
        f"{covers}")


def key_record_lines(rows: Sequence[Any]) -> list[str]:
    if not rows:
        return []
    records = ordered_rows(rows)[0].records
    lines: list[str | None] = [
        *(_legal_key_line(i)
          for i in record_items(records, "legal_matters")),
        *(_transaction_key_line(i)
          for i in record_items(records, "transactions")),
        *(_guidance_key_line(i) for i in record_items(records, "guidance"))
    ]
    return [display_amounts(line) for line in lines if line]


async def key_records_for(
        ticker: str, cache: FilingRecordsCache, *, role: str) -> list[str]:
    return await _lines_for(
        key_record_lines, ticker, cache, role=role,
        event="filing.key_records_failed")


_PERIOD_TOLERANCE_DAYS = 7
_YEAR_AGO_DAYS = 364
_YEAR_AGO_TOLERANCE_DAYS = 10


def _iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _form_kind(form: str) -> str:
    for kind in ("10-Q", "10-K"):
        if form.upper().startswith(kind):
            return kind
    return form


def _unusual_line(item: dict[str, Any], row: Any) -> str:
    return display_amounts(
        f"{_text(item, 'description')}: {_text(item, 'amount_text')}"
        f"{unusual_item_period(item)} "
        f"[{row.form} filed {row.filing_date.isoformat()}]"
        f"{quote_evidence(item)}")


def _year_end_of(row: Any) -> date | None:
    if row.report_date is not None:
        report: date = row.report_date
        return report
    ends = [
        end for item in record_items(row.records, "one_off_items")
        if period_months_value(item.get("period_months")) == 12 and (
            end := _iso_date(item.get("period_end"))) is not None
        and end < row.filing_date
    ]
    return max(ends, default=None)


def year_to_date_unusual_item_lines(rows: Sequence[Any]) -> list[str]:
    ordered = ordered_rows(rows)
    if not ordered:
        return []
    newest = ordered[0]
    if _form_kind(newest.form) != "10-Q" or newest.report_date is None:
        return []
    year_ends = [
        end for r in ordered if _form_kind(r.form) == "10-K" and (
            end := _year_end_of(r)) is not None and end < newest.report_date
    ]
    if not year_ends or (newest.report_date - max(year_ends)).days > (
            9 * 31 + _PERIOD_TOLERANCE_DAYS):
        logger.info(
            "filing.ytd_window_unknown", newest=newest.filename,
            report_date=newest.report_date.isoformat())
        return []
    tolerance = timedelta(days=_PERIOD_TOLERANCE_DAYS)
    start, end = max(year_ends) + tolerance, newest.report_date - tolerance
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    from_record_periods = 0
    for row in ordered[1:]:
        for item in record_items(row.records, "one_off_items"):
            period_end = _iso_date(item.get("period_end"))
            months = period_months_value(item.get("period_months"))
            if period_end is None or not start < period_end < end:
                continue
            if row.report_date is None and months != 3:
                continue
            if item.get("period_months") not in (None, "") and months != 3:
                continue
            key = (period_end.isoformat(), _text(item, "amount_text"))
            if key in seen:
                continue
            seen.add(key)
            from_record_periods += row.report_date is None
            lines.append(_unusual_line(item, row))
    if from_record_periods or max(year_ends) not in {r.report_date
                                                     for r in ordered}:
        logger.info(
            "filing.ytd_window_from_record_periods", newest=newest.filename,
            year_end=max(year_ends).isoformat(), items=from_record_periods)
    return lines


def prior_year_unusual_item_lines(rows: Sequence[Any]) -> list[str]:
    ordered = ordered_rows(rows)
    if not ordered:
        return []
    newest = ordered[0]
    if newest.report_date is None:
        return []
    kind = _form_kind(newest.form)
    own_months = OWN_PERIOD_MONTHS.get(kind)
    if own_months is None:
        return []

    newest_report: date = newest.report_date

    def gap(row: Any) -> int:
        report: date = row.report_date
        return abs((newest_report - report).days - _YEAR_AGO_DAYS)

    candidates = [
        r for r in ordered[1:] if _form_kind(r.form) == kind
        and r.report_date is not None and gap(r) <= _YEAR_AGO_TOLERANCE_DAYS
    ]
    if not candidates:
        return []
    year_ago = min(candidates, key=gap)
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in record_items(year_ago.records, "one_off_items"):
        if item.get("period_months") not in (None, "") and (
                period_months_value(item.get("period_months")) != own_months):
            continue
        key = (_text(item, "period_end"), _text(item, "amount_text"))
        if key in seen:
            continue
        seen.add(key)
        lines.append(_unusual_line(item, year_ago))
    return lines


async def _lines_for(
        build: Callable[[Sequence[Any]], list[str]], ticker: str,
        cache: FilingRecordsCache, *, role: str, event: str) -> list[str]:
    try:
        return build(await cache.load(ticker))
    except Exception as exc:
        logger.warning(event, role=role, error=str(exc))
        return []


class FilingAgent(BaseAgent):

    async def run(self, state: AnalysisState) -> AgentOutput:
        rag_visibility = visibility_for_state(state)
        feedback = state.reflection_feedback.get(self.role)
        records = FilingRecordsCache()
        filings_tool = SearchFilingsTool(
            ticker=state.ticker, visibility=rag_visibility,
            records_loader=records)
        prefetched = await prefetch_tool_calls([
            (filings_tool, {
                "query": PREFETCH_FILING_QUERY
            })
        ])
        kg_context = state.kg_context
        loop = ReActAgentLoop(
            role=self.role, tools=[
                filings_tool,
                QueryKnowledgeGraphTool(viewer_user_id=state.user_id or None)
            ], system_prompt=_SYSTEM_PROMPT, max_iterations=MAX_ITERATIONS,
            finish_answer_validator=InternalTokenValidator(
                self.role.value, once_checks=[(
                    OWN_EVIDENCE_CHECK,
                    own_evidence_figure_check(
                        state.fresh_filing_context or ""))]),
            finish_validation_suffix=_FILING_VALIDATION_SUFFIX)
        result = await loop.run(
            f"Extract key risk factors, recent results, and guidance from "
            f"{state.ticker}'s filings.", extra_context=combine_context(
                feedback, state.procedural_hints.get(self.role),
                state.fresh_filing_context, kg_context),
            prefetched_evidence=prefetched)
        return self.completed_output(
            result, feedback=feedback, summary=result.final_answer,
            raw_output={
                "injected_context": combine_context(
                    state.fresh_filing_context, kg_context),
                "final_answer": result.final_answer,
                "one_off_items": await
                one_off_items_for(state.ticker, records, role=self.role.value),
                "key_records": await
                key_records_for(state.ticker, records, role=self.role.value),
                "year_to_date_unusual_items": await _lines_for(
                    year_to_date_unusual_item_lines, state.ticker, records,
                    role=self.role.value,
                    event="filing.ytd_unusual_items_failed"),
                "prior_year_unusual_items": await _lines_for(
                    prior_year_unusual_item_lines, state.ticker, records,
                    role=self.role.value,
                    event="filing.prior_year_unusual_items_failed")
            })
