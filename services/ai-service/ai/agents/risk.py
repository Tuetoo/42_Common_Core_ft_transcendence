import re
from collections.abc import Sequence
from typing import Any
import structlog
from ai.agents.evidence_grounding import wrap_verification_warning
from ai.agents.filing import FilingRecordsCache
from ai.agents.finish_checks import (OWN_EVIDENCE_CHECK,
                                     InternalTokenValidator,
                                     own_evidence_figure_check)
from ai.agents.loop import (BaseAgent, ReActAgentLoop, ScratchpadEntry,
                            combine_context, has_successful_tool_evidence,
                            is_successful_tool_entry, prefetch_tool_calls)
from ai.graph.state import AgentOutput, AnalysisState
from ai.knowledge.filing_records import (LEGAL_STATUSES, TRANSACTION_STATUSES,
                                         legal_record_count, ordered_rows,
                                         parse_filing_filename, record_by_id,
                                         record_title)
from ai.knowledge.recycled_content import (prior_note_similarity,
                                           split_agent_note_passages)
from ai.rag.visibility import visibility_for_state
from ai.tools.base import NON_EVIDENTIAL_TOOLS
from ai.tools.filing_tool import SearchFilingsTool
from ai.tools.knowledge_graph_tool import QueryKnowledgeGraphTool
from ai.tools.market_tool import GetRecentPriceHistoryTool
from ai.tools.research_archive_tool import SearchResearchArchiveTool
from ai.tools.risk_tool import GetVolatilityAndDrawdownTool
from ai.tools.web_search_tool import WebSearchTool
from config import settings
from shared.enums import DocumentCategory

logger = structlog.get_logger(__name__)

PREFETCH_RISK_NOTES_QUERY = "regulatory competition litigation"
PREFETCH_RISK_FILING_QUERY = (
    "risk factors legal proceedings litigation investigation regulatory")
PREFETCH_RISK_TRANSCRIPT_QUERY = "risks legal and regulatory matters headwinds"
MAX_ITERATIONS = 7

_EVENT_STATUSES: tuple[str, ...] = (
    *LEGAL_STATUSES, *(
        status
        for status in TRANSACTION_STATUSES if status not in LEGAL_STATUSES),
    "not_applicable")

_SYSTEM_PROMPT = (
    "You are the Risk Agent in AlphaMind's multi-agent investment research "
    "system. Your job is to assess this ticker's own volatility, drawdown "
    "history, and sector-level risk -- NOT how it interacts with any other "
    "position in a portfolio (that is the separate Portfolio Agent's job). "
    "Use get_volatility_and_drawdown for the actual annualized volatility "
    "and max drawdown numbers -- it computes them precisely from the full "
    "price series, so don't try to estimate them yourself from "
    "get_recent_price_history's rows (that tool only shows a readable "
    "recent-trend sample, not the full window, and re-calling it with the "
    "same or different lookback_days won't change that). Use "
    "get_recent_price_history only for qualitative context (when a "
    "specific move happened, what the recent trend looks like), "
    "query_knowledge_graph to check for previously-established "
    "relationships that carry real risk exposure (e.g. a known supply-"
    "chain dependency or competitive threat -- may return nothing if none "
    "have been extracted yet, which is normal, not an error). Some "
    "knowledge-graph facts are dated readings of a metric that changes over "
    "time (a volatility figure, a price level) rather than a stable "
    "relationship -- when the same relation type shows up more than once "
    "with different dates, the older reading is history, not a "
    "contradiction: treat the one tagged [MOST RECENT reading] (or "
    "otherwise the latest date shown) as current, and don't cite one marked "
    "[SUPERSEDED ...] as if it were today's value. "
    "search_prior_risk_notes to check your own last real risk read on this "
    "ticker BEFORE reaching for web_search (call it once, right after "
    "get_volatility_and_drawdown/query_knowledge_graph -- it may return "
    "nothing if this ticker has never been analyzed before, which is "
    "expected, don't retry it, just move on), and web_search to check for "
    "real, specific risk events (regulatory action, litigation, supply "
    "chain disruption, management changes) that price history and your "
    "own prior notes wouldn't surface. A search_filings result for the "
    "filing's own risk factors and legal proceedings is already in your "
    "scratchpad (fetched in code): name the specific open matters it lists "
    "(investigations, lawsuits, tax or regulatory disputes, fines), and "
    "never describe regulatory or legal risk as contained, limited or "
    "absent when that result lists open matters. "
    "If search_prior_risk_notes already "
    "gives you a current, non-stale risk read, you may not need web_search "
    "at all -- don't spend a call re-deriving something you already know. "
    "A query that lists every risk category at once as keywords (e.g. "
    '"regulatory risk litigation supply chain antitrust management '
    'change") tends to return generic, unfocused results and gets '
    "filtered out as not genuinely relevant -- costing a wasted retry. "
    "If query_knowledge_graph or other evidence already points to a "
    "SPECIFIC named law, case, or event, search for that specifically "
    '(e.g. "[ticker] SEC climate disclosure rule" beats "[ticker] '
    'regulatory risk") rather than a broad category list on the first '
    "attempt. Write any web_search-derived content in your own words and "
    "cite sources, don't quote at length. Each web_search result line ends "
    "with catalog=yes (an outlet in NewsAPI's source catalog) or "
    "catalog=no: do not present a claim from a catalog=no source with the "
    "same confidence as one from a catalog=yes source. If a web_search "
    "result is marked [STALE SOURCE ...], do not use it as a current risk "
    "event -- search again with a more specific/recent query instead. "
    "A search_earnings_call_transcripts result (management's own words on "
    "an earnings call, when one has been uploaded for this ticker) is "
    "already in your scratchpad (fetched in code; it may say none was "
    "found, which is expected, not an error); you may call it again with "
    "another query. Use it to see which risks, legal or regulatory matters "
    "and headwinds management itself raised. Transcripts are management's "
    "own words: attribute them with the call they come from (e.g. "
    '"management said on the Q2 2026 call"), and for a legal status or '
    "a reported figure prefer the filing over the call. "
    "A flagged [STALE ...] result from search_prior_risk_"
    "notes means a "
    "referenced risk event may have already resolved -- verify with "
    "web_search if it's central to your conclusion. Give "
    "your final answer as plain prose (a few sentences), not as a JSON "
    "object or any other structured format -- it needs to read naturally "
    "in a debate and in a report a person will read. "
    "Whenever you set finish_answer, also fill in reported_metrics."
    "risk_events: one object for EVERY named risk event your answer "
    "mentions (a regulator or agency action, a lawsuit or case, a deal, a "
    "named counterparty, supplier or incident) with: entity (spelled as the "
    "tool output spells it), evidence_tool (the tool whose output THIS RUN "
    "names it, e.g. web_search, search_filings), status (for a legal matter "
    "or deal: its status as of the newest filing that discusses it -- one of "
    f"{', '.join(_EVENT_STATUSES[:-1])}; otherwise not_applicable) and "
    "source_id (the id of the filing record of THAT matter, e.g. F1.L2 -- "
    "never the record of another matter; when no record names the matter, "
    "the filing label, e.g. F1, or the filing file name shown in "
    "search_filings; empty for a non-filing source). Use an empty "
    "list if you name none. This is checked mechanically: an entity found "
    "only in the background context, in query_knowledge_graph results or "
    "in [agent:...] passages is rejected, and a cited record id must exist "
    "and be the record of that matter. "
    "Use the code-rendered values and the filing records: the first "
    "search_filings result lists filing records (legal matters and "
    "transactions, newest filing first, each with the filing sentence it "
    "was taken from). The records are extracted automatically and their "
    "status can be wrong or out of date: state the status that the newest "
    "filing text you were shown supports (the record's quoted sentence or "
    "a filing passage), cite the newest filing that discusses the matter, "
    "and do not present a matter that filing reports as dismissed, "
    "settled, completed or terminated as an open risk. When a record "
    "states an exposure amount, give it with the matter. "
    "Passages tagged [agent:...] are prior "
    "AI-written notes: confirm any date or status from them with fresh tool "
    "data, and write your answer from this run's tool results -- do not "
    "copy a prior note's wording (checked mechanically). "
    "Record ids (e.g. F1.L2) belong only in risk_events source_id: never "
    "write one in your answer prose -- name the filing in plain words "
    'instead (its form and filing date, e.g. "the 10-Q filed '
    '2026-07-23"). Bracketed markers and catalog= fields are for you, '
    "not the reader: name the source itself.")


def _skipped_all_risk_evidence(scratchpad: Sequence[ScratchpadEntry]) -> bool:
    return not has_successful_tool_evidence(scratchpad)


def _independent_text(observation: str) -> str:
    return split_agent_note_passages([observation])[1]


_RISK_REPORTED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "risk_events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string"
                    },
                    "evidence_tool": {
                        "type": "string"
                    },
                    "status": {
                        "type": "string",
                        "enum": list(_EVENT_STATUSES)
                    },
                    "source_id": {
                        "type": "string"
                    }
                },
                "required": ["entity", "evidence_tool", "status"]
            }
        }
    },
    "additionalProperties": False
}


def _reported_risk_events(
        reported_metrics: dict[str, Any] | None) -> list[dict[str, str]] | str:
    raw = (reported_metrics or {}).get("risk_events")
    if raw is None:
        return []
    if not isinstance(raw, list):
        return (
            "reported_metrics.risk_events must be a list of "
            "{entity, evidence_tool, status, source_id} objects.")
    events: list[dict[str, str]] = []
    for item in raw:
        normalized = _normalize_risk_event(item)
        if isinstance(normalized, str):
            return normalized
        events.append(normalized)
    return events


def _normalize_risk_event(item: Any) -> dict[str, str] | str:
    if not isinstance(item, dict):
        return (
            "each reported_metrics.risk_events item must be an "
            "{entity, evidence_tool, status, source_id} object.")
    entity = str(item.get("entity") or "").strip()
    tool = str(item.get("evidence_tool") or "").strip()
    if not entity or not tool:
        return (
            "each reported_metrics.risk_events item needs a non-empty "
            "entity and evidence_tool.")
    status = str(item.get("status") or "").strip().lower()
    if status not in _EVENT_STATUSES:
        return (
            f"risk event {entity!r}: status must be one of "
            f"{', '.join(_EVENT_STATUSES)} (got {status or 'nothing'!r}).")
    return {
        "entity": entity,
        "evidence_tool": tool,
        "status": status,
        "source_id": str(item.get("source_id") or "").strip()
    }


def _risk_event_issue(
        event: dict[str, str],
        scratchpad: Sequence[ScratchpadEntry]) -> str | None:
    entity, tool = event["entity"], event["evidence_tool"]
    if tool in NON_EVIDENTIAL_TOOLS:
        return (
            f"risk event {entity!r} cites {tool}, whose facts are derived "
            f"from "
            "earlier agent reports, not an independent source -- confirm it "
            "with web_search, search_filings or another tool this run, or "
            "drop it from your answer.")
    observations = [
        e.observation or "" for e in scratchpad
        if is_successful_tool_entry(e, tool)
    ]
    if not observations:
        return (
            f"risk event {entity!r} cites {tool}, but {tool} has no "
            f"successful "
            "result this run (background context does not count) -- call a "
            "tool that returns it, or drop that event from your answer.")
    needle = entity.lower()
    if not any(needle in obs.lower() for obs in observations):
        return (
            f"risk event {entity!r} does not appear in any {tool} result this "
            "run -- name it exactly as the tool output does, cite the tool "
            "that actually returned it, or drop it from your answer.")
    if not any(needle in _independent_text(obs).lower()
               for obs in observations):
        return (
            f"risk event {entity!r} appears in {tool} only inside an earlier "
            "agent note ([agent:...] passage), which is not an independent "
            "source -- confirm it with search_filings or web_search this run "
            "(and describe it as that source does), or drop it from your "
            "answer.")
    return None


_RECORD_ID_RE = re.compile(r"^F\d+\.[A-Z]\d+$", re.IGNORECASE)
_FILING_LABEL_RE = re.compile(r"^F(\d+)$", re.IGNORECASE)


def _labelled_filing(rows: Sequence[Any], source_id: str) -> Any | None:
    match = _FILING_LABEL_RE.match(source_id.strip())
    if match is None:
        return None
    ordered = ordered_rows(rows)
    index = int(match.group(1))
    return ordered[index - 1] if 1 <= index <= len(ordered) else None


_STATUS_CATEGORIES = ("legal_matters", "transactions")
_CATEGORY_LETTER = {"legal_matters": "L", "transactions": "T"}
_NAME_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_INNER_PUNCT_RE = re.compile("(?<=[a-z0-9])[.'\u2019&-]+(?=[a-z0-9])")


def _normalized(text: str) -> str:
    return _NAME_NORMALIZE_RE.sub(" ", text.lower()).strip()


def _name_forms(text: str) -> set[str]:
    lowered = " ".join(text.lower().split())
    forms = {
        _normalized(lowered),
        _normalized(_INNER_PUNCT_RE.sub("", lowered))
    }
    return {f for f in forms if f}


def _names_match(entity: str, record_name: str) -> bool:
    for a in _name_forms(entity):
        for b in _name_forms(record_name):
            if f" {a} " in f" {b} " or f" {b} " in f" {a} ":
                return True
    return False


def _text_names(text: str, entity: str) -> bool:
    return any(
        f" {a} " in f" {b} " for a in _name_forms(entity)
        for b in _name_forms(text))


def _record_names_entity(entity: str, record: dict[str, Any]) -> bool:
    if record.get("category") not in _STATUS_CATEGORIES:
        return True
    name = str(record.get("name") or "")
    texts = [str(record.get(key) or "") for key in ("quote", "detail")]
    if _names_match(entity, name) or any(_text_names(t, entity)
                                         for t in texts):
        return True
    first_party = _caption_first_party(entity)
    return bool(first_party) and any(
        _text_names(t, first_party) for t in (name, *texts))


def _record_is_of_entity(
        entity: str, record: dict[str, Any], rows: Sequence[Any]) -> bool:
    if _record_names_entity(entity, record):
        return True
    category = record.get("category")
    letter = _CATEGORY_LETTER.get(str(category))
    name = str(record.get("name") or "")
    if letter is None or not name:
        return False
    for index, row in enumerate(ordered_rows(rows), start=1):
        items = (row.records or {}).get(str(category))
        if not isinstance(items, list):
            continue
        for number in range(1, len(items) + 1):
            other = record_by_id(rows, f"F{index}.{letter}{number}")
            if (other is not None and other["id"] != record.get("id")
                    and _record_names_entity(entity, other)
                    and _names_match(name, str(other.get("name") or ""))):
                return True
    return False


_CAPTION_SEPARATOR = " v. "


def _caption_first_party(entity: str) -> str:
    head, separator, _ = entity.partition(_CAPTION_SEPARATOR)
    return head.strip() if separator else ""


def _shown_filing_names(scratchpad: Sequence[ScratchpadEntry]) -> str:
    return "\n".join(
        e.observation or "" for e in scratchpad if e.action == "search_filings"
        and e.is_tool_result and not e.tool_error)


def _cited_filing(
        event: dict[str, str], rows: Sequence[Any], shown: bool,
        scratchpad: Sequence[ScratchpadEntry], *,
        log: bool = True) -> tuple[str | None, dict[str, Any] | None]:
    entity, source_id = event["entity"], event["source_id"]
    if not source_id:
        if (event["evidence_tool"] == "search_filings"
                and event["status"] != "not_applicable"):
            return (
                f"risk event {entity!r} cites search_filings with status "
                f"{event['status']!r} but no source_id -- give the filing "
                "record id (e.g. F1.L2) or the filing file name it comes "
                "from.", None)
        return None, None
    if _FILING_LABEL_RE.match(source_id):
        row = _labelled_filing(rows, source_id) if shown else None
        if row is None:
            return (
                f"risk event {entity!r} cites {source_id}, but no filing with "
                "that label was listed this run -- cite a record id from the "
                "search_filings records block, a filing file name, or leave "
                "source_id empty for a non-filing source.", None)
        filing_name = row.filename.removesuffix(".txt")
        if log:
            logger.info(
                "risk.filing_label_resolved", entity=entity, label=source_id,
                filename=filing_name)
        record = _record_in_filing(event, rows, filing_name)
        if log and record is not None and record["status"] != event["status"]:
            _log_status_mismatch(entity, record, event["status"])
        return None, record
    if _RECORD_ID_RE.match(source_id):
        record = record_by_id(rows, source_id) if shown else None
        if record is None:
            return (
                f"risk event {entity!r} cites {source_id}, but no filing "
                f"record "
                "with that id was listed this run -- cite a record id from "
                "the "
                "search_filings records block, a filing file name, or leave "
                "source_id empty for a non-filing source.", None)
        if (log and record["category"] in _STATUS_CATEGORIES
                and record["status"] != event["status"]
                and _record_is_of_entity(entity, record, rows)):
            _log_status_mismatch(entity, record, event["status"])
        return None, record
    parsed = parse_filing_filename(source_id)
    name = source_id.removesuffix(".txt")
    if parsed is None or name not in _shown_filing_names(scratchpad):
        return (
            f"risk event {entity!r} cites {source_id!r}, which is not a "
            f"filing "
            "shown in a search_filings result this run -- cite a record id "
            "(e.g. F1.L2) or a filing file name from the results.", None)
    record = _record_in_filing(event, rows, name) if shown else None
    if log and record is not None and record["status"] != event["status"]:
        _log_status_mismatch(entity, record, event["status"])
    return None, record


def _log_status_mismatch(
        entity: str, record: dict[str, Any], stated: str) -> None:
    logger.info(
        "risk.record_status_mismatch", entity=entity, record_id=record["id"],
        record_status=record["status"], stated_status=stated,
        filing_date=record["filing_date"])


def _record_in_filing(
        event: dict[str, str], rows: Sequence[Any],
        filing_name: str) -> dict[str, Any] | None:
    for index, row in enumerate(ordered_rows(rows), start=1):
        if row.filename.removesuffix(".txt") != filing_name:
            continue
        for category in _STATUS_CATEGORIES:
            items = (row.records or {}).get(category)
            if not isinstance(items, list):
                continue
            for number, item in enumerate(items, start=1):
                if isinstance(item, dict) and _names_match(
                        event["entity"], str(item.get("name") or "")):
                    return record_by_id(
                        rows, f"F{index}.{_CATEGORY_LETTER[category]}{number}")
    return None


def _records_naming(entity: str, rows: Sequence[Any],
                    limit: int = 3) -> tuple[list[str], Any | None]:
    for index, row in enumerate(ordered_rows(rows), start=1):
        found: list[str] = []
        for category in _STATUS_CATEGORIES:
            items = (row.records or {}).get(category)
            if not isinstance(items, list):
                continue
            for number in range(1, len(items) + 1):
                record = record_by_id(
                    rows, f"F{index}.{_CATEGORY_LETTER[category]}{number}")
                if record is not None and _names_match(entity, str(
                        record.get("name") or "")):
                    found.append(record["id"])
        if found:
            return found[:limit], row
    return [], None


def _record_name_issue(
        event: dict[str, str], record: dict[str, Any],
        rows: Sequence[Any]) -> str:
    entity = event["entity"]
    naming, naming_row = _records_naming(entity, rows)
    ordered = ordered_rows(rows)
    if not naming:
        hint = "no record names it, so cite the filing it comes from"
    elif naming_row is not None and ordered and naming_row is not ordered[0]:
        newest = ordered[0]
        hint = (
            f"the record that names it: {', '.join(naming)}, from the "
            f"{naming_row.form} filed {naming_row.filing_date.isoformat()}, "
            f"older than the newest filing F1 ({newest.form} filed "
            f"{newest.filing_date.isoformat()}); state the matter's status as "
            "of the newest filing passage that mentions it, and cite F1 if "
            "its passages name the matter")
    else:
        hint = f"the record that names it: {', '.join(naming)}"
    return (
        f"{entity!r} cites {record['id']}, which is the record of "
        f"{record.get('name')!r} ({hint})")


RECORD_NAME_MESSAGE = (
    "Record check: these risk events cite a filing record of another matter: "
    "{parts}. Cite the record of the matter itself, or, when no record names "
    "it, the filing it comes from (its label, e.g. F1, or its file name); "
    "never cite another matter's record.")


def _event_source_issue(
        event: dict[str, str], scratchpad: Sequence[ScratchpadEntry], *,
        rows: Sequence[Any], shown: bool,
        allow_record_name_mismatch: bool = True,
        log: bool = True) -> tuple[str | None, bool]:
    issue, record = _cited_filing(event, rows, shown, scratchpad, log=log)
    if issue is not None:
        return issue, False
    if (record is not None and _RECORD_ID_RE.match(event["source_id"])
            and not _record_is_of_entity(event["entity"], record, rows)):
        if log:
            logger.info(
                "risk.record_name_mismatch", entity=event["entity"],
                record_id=record["id"], record_name=record.get("name"),
                rejected=not allow_record_name_mismatch)
        if not allow_record_name_mismatch:
            return _record_name_issue(event, record, rows), True
    return None, False


def _risk_events_unsourced(
        final_answer: str, reported_metrics: dict[str, Any] | None,
        scratchpad: list[ScratchpadEntry]) -> str | None:
    del final_answer
    events = _reported_risk_events(reported_metrics)
    if isinstance(events, str):
        return events
    for event in events:
        issue = _risk_event_issue(event, scratchpad)
        if issue is not None:
            return issue
    return None


def _risk_answer_issue(
        final_answer: str, reported_metrics: dict[str, Any] | None,
        scratchpad: list[ScratchpadEntry], *, rows: Sequence[Any] = (),
        shown: bool = False, allow_record_name_mismatch: bool = True,
        log: bool = True) -> tuple[str | None, bool]:
    issue = _risk_events_unsourced(final_answer, reported_metrics, scratchpad)
    if issue is not None:
        return issue, False
    events = _reported_risk_events(reported_metrics)
    if isinstance(events, str):
        return events, False
    name_parts: list[str] = []
    for event in events:
        found, once = _event_source_issue(
            event, scratchpad, rows=rows, shown=shown,
            allow_record_name_mismatch=allow_record_name_mismatch, log=log)
        if found is not None and not once:
            return found, False
        if found is not None:
            name_parts.append(found)
    if name_parts:
        return RECORD_NAME_MESSAGE.format(parts="; ".join(name_parts)), True
    return None, False


_RISK_VALIDATION_SUFFIX = (
    "Fix only what the check above names, using the tool results already in "
    "this scratchpad, keep every other correct fact, then set finish_answer "
    "again.")
RISK_REUSED_DISCLOSURE = (
    "This risk section largely repeats an earlier AI-written risk note; "
    "treat any status or event in it that this run's filings do not "
    "confirm as unverified.")


def _prior_note_copy_ratio(
        final_answer: str,
        scratchpad: Sequence[ScratchpadEntry]) -> float | None:
    try:
        return prior_note_similarity(final_answer, scratchpad)
    except Exception as exc:
        logger.warning("risk.prior_note_similarity_failed", error=str(exc))
        return None


def _sourced_risk_events(
        reported_metrics: dict[str, Any] | None,
        scratchpad: Sequence[ScratchpadEntry], *, rows: Sequence[Any] = (),
        shown: bool = False) -> list[dict[str, str]]:
    raw = (reported_metrics or {}).get("risk_events")
    if not isinstance(raw, list):
        return []
    events = [
        e for e in (_normalize_risk_event(item) for item in raw)
        if not isinstance(e, str)
    ]
    return [
        e for e in events if _risk_event_issue(e, scratchpad) is None
        and _cited_filing(e, rows, shown, scratchpad, log=False)[0] is None
    ]


class _CapturingRiskEventsValidator(InternalTokenValidator):

    def __init__(
            self, records: FilingRecordsCache | None = None,
            extra_reference: str = "") -> None:
        super().__init__(
            "risk", base=self._events_issue,
            once_checks=[(_RECORD_NAME_CHECK, self._pending_record_name_issue),
                         (
                             OWN_EVIDENCE_CHECK,
                             own_evidence_figure_check(extra_reference)),
                         (_EMPTY_EVENTS_CHECK, self._empty_events_issue)])
        self.records = records or FilingRecordsCache()
        self.last_reported_metrics: dict[str, Any] | None = None
        self._record_name_issue: str | None = None

    def __call__(
            self, final_answer: str, reported_metrics: dict[str, Any] | None,
            scratchpad: list[ScratchpadEntry]) -> str | None:
        self.last_reported_metrics = reported_metrics
        return super().__call__(final_answer, reported_metrics, scratchpad)

    def _events_issue(
            self, final_answer: str, reported_metrics: dict[str, Any] | None,
            scratchpad: list[ScratchpadEntry]) -> str | None:
        issue, once = _risk_answer_issue(
            final_answer, reported_metrics, scratchpad, rows=self.records.rows
            or [], shown=self.records.shown, allow_record_name_mismatch=(
                _RECORD_NAME_CHECK in self.fired or not self.budget_left))
        self._record_name_issue = issue if once else None
        if issue is None:
            return None
        logger.info("risk.event_rejected", issue=issue[:160], record_name=once)
        return None if once else issue

    def _pending_record_name_issue(
            self, final_answer: str, reported_metrics: dict[str, Any] | None,
            scratchpad: list[ScratchpadEntry]) -> str | None:
        del final_answer, reported_metrics, scratchpad
        return self._record_name_issue

    def _empty_events_issue(
            self, final_answer: str, reported_metrics: dict[str, Any] | None,
            scratchpad: list[ScratchpadEntry]) -> str | None:
        del final_answer, scratchpad
        events = _reported_risk_events(reported_metrics)
        if isinstance(events, str) or events or not self.records.shown:
            return None
        count = legal_record_count(self.records.rows or [])
        if count == 0:
            return None
        logger.info(
            "risk.empty_events_with_legal_records", legal_records=count)
        return EMPTY_EVENTS_MESSAGE.format(count=count)


_RECORD_NAME_CHECK = "record_name"
_EMPTY_EVENTS_CHECK = "empty_risk_events"
EMPTY_EVENTS_MESSAGE = (
    "Risk events check: reported_metrics.risk_events is empty, but the "
    "filing records shown to you this run list {count} legal matter(s). "
    "Add one risk_events object for every named risk event your answer "
    "mentions (a lawsuit or case, a regulator or agency action, a deal, a "
    "named counterparty or an incident), with the record id of that matter "
    "as source_id when a record names it. Keep the list empty only if your "
    "answer names no such event.")


def _plain_source(
        event: dict[str, str], rows: Sequence[Any], shown: bool) -> str:
    source = event["source_id"]
    if shown and _FILING_LABEL_RE.match(source):
        row = _labelled_filing(rows, source)
        return (
            f"{row.form} filed {row.filing_date.isoformat()}"
            if row is not None else "")
    if _RECORD_ID_RE.match(source):
        record = record_by_id(rows, source) if shown else None
        if record is None:
            return ""
        filing = f"{record['form']} filed {record['filing_date']}"
        if not _record_is_of_entity(event["entity"], record, rows):
            return filing
        title = (
            "" if _name_forms(event["entity"])
            & _name_forms(str(record.get("name") or "")) else
            record_title(record))
        return f"{filing}, {title}" if title else filing
    parsed = parse_filing_filename(source)
    return f"{parsed[1]} filed {parsed[2]}" if parsed is not None else ""


def _event_line(
        event: dict[str, str], rows: Sequence[Any], shown: bool) -> str:
    parts: list[str] = []
    if event["status"] != "not_applicable":
        parts.append(f"status {event['status']}")
    source = _plain_source(event, rows, shown)
    if source:
        parts.append(f"source: {source}")
    return (
        f"{event['entity']} ({'; '.join(parts)})"
        if parts else event["entity"])


def _risk_events_line(
        events: list[dict[str, str]], rows: Sequence[Any] = (),
        shown: bool = False) -> str:
    if not events:
        return (
            "Named risk events (sourced): none reported with a tool source "
            "this run.")
    return "Named risk events (sourced): " + "; ".join(
        _event_line(e, rows, shown) for e in events)


class RiskAgent(BaseAgent):

    async def run(self, state: AnalysisState) -> AgentOutput:
        rag_visibility = visibility_for_state(state)
        feedback = state.reflection_feedback.get(self.role)
        records = FilingRecordsCache()
        kg_context = state.kg_context
        injected_context = combine_context(
            state.fresh_filing_context, kg_context)
        validator = _CapturingRiskEventsValidator(
            records, extra_reference=injected_context or "")
        volatility_tool = GetVolatilityAndDrawdownTool(
            analysis_ticker=state.ticker)
        prior_notes_tool = SearchResearchArchiveTool(
            visibility=rag_visibility, name="search_prior_risk_notes",
            description=(
                "Call this ONCE, right after get_volatility_and_"
                "drawdown/query_knowledge_graph and before "
                "web_search -- checking your own last real risk "
                "read on this ticker is cheap and may make a fresh "
                "web_search unnecessary. Search AlphaMind's own "
                "prior Risk analysis notes for this ticker "
                "(archived automatically after each real analysis "
                'run). action_input: {"query": '
                '"litigation and regulatory exposure"}. May return '
                "nothing if this ticker has "
                "never been analyzed before -- that's expected, "
                "not an error; don't retry this call, just move on "
                "to web_search. A result older than roughly two "
                "quarters is flagged [STALE ...] -- a referenced "
                "risk event may have already resolved; treat it as "
                "background and proceed to web_search for a "
                "current read."), category=DocumentCategory.RISK_ANALYSIS,
            ticker=state.ticker)
        filings_tool = SearchFilingsTool(
            ticker=state.ticker, visibility=rag_visibility,
            records_loader=records)
        transcripts_tool = SearchResearchArchiveTool(
            visibility=rag_visibility, name="search_earnings_call_transcripts",
            description=(
                "Search manually-sourced earnings call transcripts "
                "for this ticker (a separate, human-curated "
                "archive, not agent-generated). Useful for a "
                "risk factor management themselves flagged on a "
                "recent call (litigation, supply disruption, "
                'guidance risk). action_input: {"query": "risks '
                'management called out this quarter"}. May return '
                "nothing if no transcript has "
                "been uploaded for this ticker, which is expected, "
                "not an error -- not every ticker has one."),
            category=DocumentCategory.EARNINGS_CALL, ticker=state.ticker)
        prefetched = await prefetch_tool_calls([
            (volatility_tool, {
                "ticker": state.ticker
            }), (prior_notes_tool, {
                "query": PREFETCH_RISK_NOTES_QUERY
            }), (filings_tool, {
                "query": PREFETCH_RISK_FILING_QUERY
            }), (transcripts_tool, {
                "query": PREFETCH_RISK_TRANSCRIPT_QUERY
            })
        ])
        loop = ReActAgentLoop(
            role=self.role, tools=[
                volatility_tool,
                GetRecentPriceHistoryTool(analysis_ticker=state.ticker),
                QueryKnowledgeGraphTool(viewer_user_id=state.user_id or None),
                filings_tool, prior_notes_tool, transcripts_tool,
                WebSearchTool()
            ], system_prompt=_SYSTEM_PROMPT, max_iterations=MAX_ITERATIONS,
            finish_answer_validator=validator,
            reported_metrics_schema=_RISK_REPORTED_SCHEMA,
            finish_validation_suffix=_RISK_VALIDATION_SUFFIX)
        result = await loop.run(
            f"Assess {state.ticker}'s single-ticker volatility, drawdown, "
            "and sector risk.", extra_context=combine_context(
                feedback, state.procedural_hints.get(self.role),
                state.fresh_filing_context, kg_context),
            prefetched_evidence=prefetched)
        accepted_metrics = (
            validator.last_reported_metrics if result.finish_meta is not None
            and not result.hit_iteration_cap else None)
        rows = records.rows or []
        sourced_events = _sourced_risk_events(
            accepted_metrics, result.scratchpad, rows=rows,
            shown=records.shown)
        summary = (
            f"{result.final_answer}\n\n"
            f"{_risk_events_line(sourced_events, rows, records.shown)}")
        verification_warnings: list[str] = []
        if result.unresolved_finish_validation_issue is not None:
            logger.warning(
                "risk.evidence_validator_exhausted", ticker=state.ticker,
                issue=result.unresolved_finish_validation_issue)
            events_issue = (
                _risk_answer_issue(
                    result.final_answer, accepted_metrics, result.scratchpad,
                    rows=rows, shown=records.shown, log=False)[0]
                if accepted_metrics is not None else None)
            if events_issue is not None:
                reason = (
                    f"{events_issue} -- this was not corrected after being "
                    "flagged; treat the flagged statement, and any named risk "
                    "event below that is not in the sourced list, as "
                    "unverified.")
                summary = wrap_verification_warning(reason, summary)
                verification_warnings.append(reason)
        if _skipped_all_risk_evidence(result.scratchpad):
            logger.warning(
                "risk.finished_without_any_evidence", ticker=state.ticker)
            reason = (
                "This run finished without ever calling any real tool -- "
                "the summary relies entirely on background context, which "
                "may be stale.")
            summary = wrap_verification_warning(reason, summary)
            verification_warnings.append(reason)
        copy_ratio = _prior_note_copy_ratio(
            result.final_answer, result.scratchpad)
        reused_prior_note = (
            copy_ratio is not None
            and copy_ratio >= settings.PRIOR_NOTE_MAX_SIMILARITY)
        disclosures: list[str] = []
        if reused_prior_note:
            logger.info(
                "risk.final_answer_repeats_prior_note", ticker=state.ticker,
                similarity=round(copy_ratio or 0.0, 3))
            disclosures.append(RISK_REUSED_DISCLOSURE)
        return self.completed_output(
            result, feedback=feedback, summary=summary,
            verification_warnings=verification_warnings,
            recycled=reused_prior_note, raw_output={
                "risk_events": sourced_events,
                "final_answer": result.final_answer,
                "injected_context": injected_context,
                "reused_prior_note": reused_prior_note,
                "prior_note_similarity": (
                    round(copy_ratio, 3) if copy_ratio is not None else None),
                "disclosures": disclosures
            })
