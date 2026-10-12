import re
from typing import Any
import structlog
from ai.agents.filing import FilingRecordsCache, one_off_items_for
from ai.agents.finish_checks import (OWN_EVIDENCE_CHECK,
                                     InternalTokenValidator,
                                     own_evidence_figure_check,
                                     reported_number_issue)
from ai.agents.loop import (BaseAgent, ReActAgentLoop, ScratchpadEntry,
                            combine_context, flag_unresolved_issue,
                            is_successful_tool_entry, prefetch_tool_calls)
from ai.graph.state import AgentOutput, AnalysisState
from ai.rag.visibility import visibility_for_state
from ai.tools.filing_tool import SearchFilingsTool
from ai.tools.fundamentals_tool import GetFundamentalsTool
from ai.tools.knowledge_graph_tool import QueryKnowledgeGraphTool
from ai.tools.research_archive_tool import SearchResearchArchiveTool
from ai.tools.web_search_tool import WebSearchTool
from shared.enums import DocumentCategory

logger = structlog.get_logger(__name__)

PREFETCH_LATEST_RESULTS_QUERY = (
    "latest quarter results revenue operating income net income net loss")
PREFETCH_TRANSCRIPT_QUERY = (
    "results drivers guidance outlook margins capital spending")
MAX_ITERATIONS = 8

_SYSTEM_PROMPT = (
    "You are the Fundamental Agent in AlphaMind's multi-agent investment "
    "research system. Your job is to assess valuation: P/E, P/B, revenue "
    "growth, and margins. Use get_fundamentals for computed ratios, "
    "search_filings for qualitative detail behind the numbers (note: "
    "search_filings may return no relevant passages if nothing has been "
    "indexed for this ticker yet -- if so, rely on get_fundamentals and say "
    "so explicitly rather than fabricating filing detail), query_knowledge_"
    "graph to check for previously-established relationships involving this "
    "ticker (e.g. known competitors, suppliers, customers -- may return "
    "nothing if none have been extracted yet, which is normal, not an "
    "error). Some knowledge-graph facts are dated readings of a metric that "
    "changes over time rather than a stable relationship -- when the same "
    "relation type appears more than once with different dates, treat the "
    "one tagged [MOST RECENT reading] (or otherwise the latest date shown) "
    "as current, and don't cite one marked [SUPERSEDED ...] as if it were "
    "today's value. search_prior_analysis to check your own last real "
    "valuation "
    "judgment on this ticker BEFORE reaching for web_search (call it once, "
    "right after get_fundamentals/search_filings -- it may return nothing "
    "if this ticker has never been analyzed before, which is expected, "
    "don't retry it, just move on), and web_search for recent, real "
    "analyst commentary or company developments that give color beyond the "
    "raw ratios and your own prior notes. If search_prior_analysis already "
    "gives you a current, non-stale valuation judgment, you may not need "
    "web_search at all -- don't spend a call re-deriving something you "
    "already know. Each web_search result line ends with catalog=yes (an "
    "outlet in NewsAPI's source catalog) or catalog=no: do not present a "
    "figure from a catalog=no source with the same confidence as one from "
    "a catalog=yes source. Do NOT "
    "state a "
    "specific dollar share-price figure anywhere in your summary -- raw "
    "price is the Market Agent's job, not yours, and you have no tool that "
    "gives you one to verify. If you need to reference price-related "
    "context, describe it in relative/qualitative terms instead (e.g. "
    "'trading above its 5-year average P/E'), never a dollar amount. Write "
    "any web_search-derived commentary in your own words, citing sources, "
    "not quoted at length. Valuation conclusions typically need more than "
    "one lookup -- don't finish after a single tool call unless the picture "
    "is already clear. "
    "For forward-looking guidance/outlook figures specifically (e.g. next-"
    "year NII, expense, or charge-off targets), prefer search_filings over "
    "web_search when both are available: the company's own most recent "
    "filing is the single most current, authoritative statement of its own "
    "guidance, while a web_search result may be commentary on an EARLIER "
    "quarter's guidance that has since been revised -- a real, confirmed "
    "failure mode: guidance figures get revised practically every quarter, "
    "so a headline-sounding web article can already be stale by the time "
    "you read it, with nothing in the article itself flagging that. If you "
    "do rely on a web_search-derived guidance figure, name which quarter's "
    "earnings/filing it's reporting on and note if a more recent filing may "
    "have already superseded it -- don't present it as this ticker's "
    "current guidance without that check. If a web_search result is marked "
    "[STALE SOURCE ...], do not use it as current commentary -- search "
    "again with a more specific/recent query instead. "
    "Concretely: when checking guidance, run search_filings with a query "
    'that explicitly includes a recency term (e.g. "latest quarter '
    'outlook guidance" or "most recent 10-Q full-year outlook"), not just '
    "generic ratio/margin terms -- a generic query can surface an older "
    "filing's passage over a more recent one even when both are indexed. "
    "If search_filings and web_search disagree on a guidance number, trust "
    "search_filings -- it reflects this ticker's own actual disclosure "
    "already in this system, while a web article's number may itself be "
    "reporting on an earlier quarter. Only fall back to a web_search "
    "figure for guidance if search_filings genuinely returns nothing "
    "relevant. More generally, if get_fundamentals and a search_filings/"
    "web_search-derived figure disagree on a ratio like P/E, state both "
    "explicitly with their source and note which one you're treating as "
    "primary, rather than silently picking one and dropping the other. "
    "A search_earnings_call_transcripts result (management's own words "
    "on an earnings call, when one has been uploaded for this ticker) is "
    "already in your scratchpad (fetched in code; it may say none was "
    "found, which is expected, not an error); you may call it again with "
    "another query. Use it for what management said about the results' "
    "drivers, guidance, margins and capital spending, rather than a "
    "secondhand web_search summary. Transcripts are management's own "
    'words: attribute them with the call they come from (e.g. "management '
    'said on the Q2 2026 call"), and for a reported figure prefer the '
    "filing or get_fundamentals over the call. A flagged [STALE ...] "
    "result from search_prior_analysis is historical context, not "
    "this ticker's current state -- verify against get_fundamentals/"
    "search_filings before relying on it. "
    "Compute any derived percentage or ratio (a margin, a growth rate) "
    "yourself from this run's raw figures when you have them; never copy "
    "one from a search_prior_analysis note (P/E, forward P/E and P/B come "
    "pre-computed from get_fundamentals). "
    "Whenever you set finish_answer, also fill in reported_metrics with "
    "this run's own get_fundamentals values for every one of these 7 exact "
    "field names you cite in your answer, and always at least one of them "
    "that get_fundamentals printed this run (cite it in your answer too): "
    "trailing_pe, forward_pe, pb, "
    "revenue_growth_yoy_pct, gross_margin_pct, operating_margin_pct, "
    'net_margin_pct -- e.g. {"net_margin_pct": 12.5} for a get_fundamentals '
    'line reading "net margin (TTM)=12.5%" (use the plain number shown, not a '
    "fraction). This is checked mechanically against get_fundamentals' own "
    "output for this run, so it must be this run's real number, never a "
    "figure copied from an older search_prior_analysis/search_earnings_"
    "call_transcripts note. Do not put any OTHER number in reported_metrics "
    "-- a segment-level figure (e.g. a specific business line's revenue "
    "growth) or anything else not in this exact list of 7 names belongs "
    "only in your prose, never in reported_metrics. When you report "
    'revenue_growth_yoy_pct, also set revenue_growth_period to "annual" or '
    '"quarterly" as get_fundamentals labels that figure, and keep that '
    "period next to the figure in your prose. "
    "Use the code-rendered values and the filing records: get_fundamentals "
    "and the first search_filings result list the latest quarter's results "
    "computed from SEC XBRL (with the year-ago quarter), and search_filings "
    "lists the structured filing records (guidance, unusual items, legal "
    "matters; newest filing first). When the latest quarter differs "
    "materially from the annual figures, report that quarter too, with its "
    "period and year-over-year change. Guidance from an older filing is "
    "history: give its filing date. Name an unusual item next to the figure "
    "it affects, as its record states it (it is recorded in that period's "
    "results and may recur; do not call it one-time unless the filing "
    "does). When the latest-quarter line says the newer 10-Q or 10-K is not "
    "in SEC XBRL yet, that line is the quarter before the newest filing: "
    "take the newest quarter from the newest filing's passages and do not "
    "call the XBRL quarter the latest. Passages tagged [agent:...] are "
    "prior AI-written notes: confirm any fact or date from them with fresh "
    "tool data before stating it. "
    "Filing-record ids (e.g. F1.O2), bracketed markers and catalog= fields "
    "are for you, not the reader: name a filing by its form "
    "and filing date and a source by its own name.")

_FRESH_FUNDAMENTALS_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "trailing_pe": re.compile(r"(?<!forward )P/E=(-?\d+(?:\.\d+)?)"),
    "forward_pe": re.compile(r"forward P/E=(-?\d+(?:\.\d+)?)"),
    "pb": re.compile(r"P/B=(-?\d+(?:\.\d+)?)"),
    "revenue_growth_yoy_pct": re.compile(
        r"revenue growth YoY \([^)]*\)=(-?\d+(?:\.\d+)?)%"),
    "gross_margin_pct": re.compile(
        r"gross margin(?: \([^)]*\))?=(-?\d+(?:\.\d+)?)%"),
    "operating_margin_pct": re.compile(
        r"operating margin(?: \([^)]*\))?=(-?\d+(?:\.\d+)?)%"),
    "net_margin_pct": re.compile(
        r"net margin(?: \([^)]*\))?=(-?\d+(?:\.\d+)?)%")
}

_TOOL_NOTE_RE = re.compile(r"(?:^|\|\s|;\s)(note: [^;\n]+)")


def _fundamentals_data_caveats(scratchpad: list[ScratchpadEntry]) -> list[str]:
    caveats: list[str] = []
    for entry in scratchpad:
        if _is_good_fundamentals_entry(entry):
            for match in _TOOL_NOTE_RE.finditer(entry.observation or ""):
                note = match.group(1).strip()
                if note not in caveats:
                    caveats.append(note)
    return caveats


_REVENUE_GROWTH_PERIODS = ("annual", "quarterly")
_REPORTED_METRICS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{
            key: {
                "type": "number"
            }
            for key in _FRESH_FUNDAMENTALS_FIELD_PATTERNS
        }, "revenue_growth_period": {
            "type": "string",
            "enum": list(_REVENUE_GROWTH_PERIODS)
        }
    },
    "additionalProperties": False
}

_METRICS_TOLERANCE = 0.05


def _parse_fresh_fundamentals(observation: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for key, pattern in _FRESH_FUNDAMENTALS_FIELD_PATTERNS.items():
        match = pattern.search(observation)
        if match:
            values[key] = float(match.group(1))
    return values


def _is_good_fundamentals_entry(entry: ScratchpadEntry) -> bool:
    return is_successful_tool_entry(entry, "get_fundamentals")


def _latest_get_fundamentals_metrics(
        scratchpad: list[ScratchpadEntry]) -> dict[str, float]:
    for entry in reversed(scratchpad):
        if _is_good_fundamentals_entry(entry):
            return _parse_fresh_fundamentals(entry.observation or "")
    return {}


def _reported_metrics_disagree_with_fundamentals(
        final_answer: str, reported_metrics: dict[str, Any] | None,
        scratchpad: list[ScratchpadEntry]) -> str | None:
    del final_answer
    fresh: dict[str, float] | None = None
    for entry in scratchpad:
        if _is_good_fundamentals_entry(entry):
            fresh = _parse_fresh_fundamentals(entry.observation or "")
    if not fresh:
        return None
    if not reported_metrics:
        return (
            "finish_answer must be accompanied by a reported_metrics "
            "object stating this run's own trailing_pe/forward_pe/pb/"
            "revenue_growth_yoy_pct/gross_margin_pct/operating_margin_pct/"
            "net_margin_pct values (at least one, and every one you cite in "
            "your answer), read directly from get_fundamentals' output above "
            "-- it was missing entirely from your last attempt.")
    for key, claimed in reported_metrics.items():
        if key not in fresh:
            continue
        issue = reported_number_issue(
            key, claimed, [fresh[key]], _METRICS_TOLERANCE,
            f"reported_metrics.{key}={{value}} does not match "
            f"this run's own get_fundamentals value of {fresh[key]} "
            "for the same field -- use get_fundamentals' real number "
            "for this run, not one carried over from an older note.")
        if issue is not None:
            return issue
    return None


_TOOL_GROWTH_RE = re.compile(
    r"revenue growth YoY \((?P<label>[^)]*)\)=(?P<value>-?\d+(?:\.\d+)?)%")


def _tool_revenue_growth(
        scratchpad: list[ScratchpadEntry]) -> re.Match[str] | None:
    found: re.Match[str] | None = None
    for entry in scratchpad:
        if _is_good_fundamentals_entry(entry):
            found = _TOOL_GROWTH_RE.search(entry.observation or "") or found
    return found


def _growth_period_of(label: str) -> str:
    return "quarterly" if label.startswith("quarterly") else "annual"


def revenue_growth_period_issue(
        final_answer: str, reported_metrics: dict[str, Any] | None,
        scratchpad: list[ScratchpadEntry]) -> str | None:
    del final_answer
    if (not reported_metrics
            or "revenue_growth_yoy_pct" not in reported_metrics):
        return None
    tool_match = _tool_revenue_growth(scratchpad)
    if tool_match is None:
        return None
    expected = _growth_period_of(tool_match.group("label"))
    stated = reported_metrics.get("revenue_growth_period")
    if stated == expected:
        return None
    logger.info(
        "fundamental.revenue_growth_period_rejected", stated=stated,
        expected=expected)
    return (
        f'reported_metrics.revenue_growth_period must be "{expected}": '
        f"get_fundamentals labels revenue growth "
        f"\"{tool_match.group('label')}\" "
        f"(got {stated!r}). Set it, and keep that period next to the figure "
        "in your answer.")


def revenue_growth_line(scratchpad: list[ScratchpadEntry]) -> str:
    tool_match = _tool_revenue_growth(scratchpad)
    if tool_match is None:
        return ""
    return (
        f"Revenue growth YoY (get_fundamentals, {tool_match.group('label')}): "
        f"{tool_match.group('value')}%")


def fundamental_answer_issue(
        final_answer: str, reported_metrics: dict[str, Any] | None,
        scratchpad: list[ScratchpadEntry]) -> str | None:
    return _reported_metrics_disagree_with_fundamentals(
        final_answer, reported_metrics,
        scratchpad) or revenue_growth_period_issue(
            final_answer, reported_metrics, scratchpad)


class FundamentalAgent(BaseAgent):

    async def run(self, state: AnalysisState) -> AgentOutput:
        rag_visibility = visibility_for_state(state)
        feedback = state.reflection_feedback.get(self.role)
        fundamentals_tool = GetFundamentalsTool(analysis_ticker=state.ticker)
        records = FilingRecordsCache()
        filings_tool = SearchFilingsTool(
            ticker=state.ticker, visibility=rag_visibility,
            records_loader=records)
        transcripts_tool = SearchResearchArchiveTool(
            visibility=rag_visibility, name="search_earnings_call_transcripts",
            description=(
                "Search manually-sourced earnings call transcripts "
                "for this ticker (a separate, human-curated "
                "archive, not agent-generated). Useful for what "
                "management actually said about guidance, "
                "strategy, or specific risks in a recent quarter. "
                'action_input: {"query": "forward guidance for '
                'next quarter"}. May return '
                "nothing if no transcript has been uploaded for "
                "this ticker, which is expected, not an error -- "
                "not every ticker has one. Each result shows the "
                "call's date -- always check it; a specific "
                "guidance figure from an older call may since have "
                "been revised in a more recent one."),
            category=DocumentCategory.EARNINGS_CALL, ticker=state.ticker)
        prefetched = await prefetch_tool_calls([
            (fundamentals_tool, {
                "ticker": state.ticker
            }), (filings_tool, {
                "query": PREFETCH_LATEST_RESULTS_QUERY
            }), (transcripts_tool, {
                "query": PREFETCH_TRANSCRIPT_QUERY
            })
        ])
        kg_context = state.kg_context
        loop = ReActAgentLoop(
            role=self.role, tools=[
                filings_tool, fundamentals_tool,
                QueryKnowledgeGraphTool(viewer_user_id=state.user_id or None),
                SearchResearchArchiveTool(
                    visibility=rag_visibility, name="search_prior_analysis",
                    description=(
                        "Call this ONCE, right after get_fundamentals/"
                        "search_filings and before web_search -- checking "
                        "your own last real valuation judgment is cheap "
                        "and may make a fresh web_search unnecessary. "
                        "Search AlphaMind's own prior Fundamental analysis "
                        "write-ups for this ticker (archived automatically "
                        "after each real analysis run). "
                        'action_input: {"query": "valuation judgment and '
                        'margin trend"}. May return '
                        "nothing if this ticker has never been analyzed "
                        "before -- that's expected, not an error; don't "
                        "retry this call, just move on to web_search. A "
                        "result older than roughly one quarter is flagged "
                        "[STALE ...] -- treat it as historical context, not "
                        "this ticker's current valuation, and proceed to "
                        "web_search for a current read."),
                    category=DocumentCategory.ANALYST_REPORT,
                    ticker=state.ticker), transcripts_tool,
                WebSearchTool()
            ], system_prompt=_SYSTEM_PROMPT, max_iterations=MAX_ITERATIONS,
            finish_answer_validator=InternalTokenValidator(
                self.role.value, fundamental_answer_issue, once_checks=[(
                    OWN_EVIDENCE_CHECK,
                    own_evidence_figure_check(
                        combine_context(
                            state.fresh_filing_context, kg_context) or ""))]),
            reported_metrics_schema=_REPORTED_METRICS_SCHEMA)
        result = await loop.run(
            f"Assess {state.ticker}'s valuation: P/E, P/B, revenue growth, "
            "and margins.", extra_context=combine_context(
                feedback, state.procedural_hints.get(self.role),
                state.fresh_filing_context, kg_context),
            prefetched_evidence=prefetched)
        verification_warnings: list[str] = []
        summary = flag_unresolved_issue(
            result, result.final_answer, verification_warnings,
            event="fundamental.unresolved_finish_issue_after_retries",
            ticker=state.ticker, consequence=(
                " -- this was not corrected even after being flagged -- treat "
                "the figures below as unverified until manually re-checked "
                "against this run's tool output."))
        data_caveats = _fundamentals_data_caveats(result.scratchpad)
        if data_caveats:
            summary = (
                f"{summary}\n\nData caveat (from get_fundamentals): " +
                "; ".join(data_caveats))
        latest_quarter = await fundamentals_tool.latest_quarter(state.ticker)
        appendix = [
            line for line in (
                str(latest_quarter["text"]) if latest_quarter else "",
                revenue_growth_line(result.scratchpad)) if line
        ]
        if appendix:
            summary = f"{summary}\n\n" + "\n".join(appendix)
        fresh_metrics = _latest_get_fundamentals_metrics(result.scratchpad)
        margin_key_facts = {
            key: value
            for key, value in fresh_metrics.items() if key in (
                "gross_margin_pct", "operating_margin_pct", "net_margin_pct")
        }
        return self.completed_output(
            result, feedback=feedback, summary=summary,
            verification_warnings=verification_warnings, raw_output={
                "key_facts": margin_key_facts,
                "data_caveats": data_caveats,
                **({
                    "latest_quarter": latest_quarter
                } if latest_quarter else {}), "one_off_items": await
                one_off_items_for(state.ticker, records, role=self.role.value),
                "final_answer": result.final_answer,
                "injected_context": combine_context(
                    state.fresh_filing_context, kg_context)
            })
