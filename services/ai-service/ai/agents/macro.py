import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
import structlog
from ai.agents.finish_checks import (OWN_EVIDENCE_CHECK,
                                     InternalTokenValidator,
                                     own_evidence_figure_check,
                                     reported_number_issue)
from ai.agents.loop import (BaseAgent, ReActAgentLoop, ScratchpadEntry,
                            combine_context, flag_unresolved_issue,
                            is_successful_tool_entry, prefetch_tool_call,
                            prefetch_tool_calls)
from ai.graph.state import AgentOutput, AnalysisState
from ai.knowledge.recycled_content import recycled_share
from ai.rag.visibility import visibility_for_state
from ai.tools.macro_tool import YOY_MIN_LOOKBACK_DAYS, GetMacroIndicatorsTool
from ai.tools.research_archive_tool import SearchResearchArchiveTool
from ai.tools.web_search_tool import WebSearchTool
from config import settings
from shared.enums import DocumentCategory

logger = structlog.get_logger(__name__)

PREFETCH_MACRO_NOTES_QUERY = "Fed policy stance and rate trajectory"
MAX_ITERATIONS = 7

_SYSTEM_PROMPT = (
    "You are the Macro Agent in AlphaMind's multi-agent investment research "
    "system. Your job is to summarize the macroeconomic context relevant to "
    "this ticker: interest rates, sector rotation signals, and overall market "
    "regime. Use get_macro_indicators for hard numbers (rates, CPI, GDP), "
    "search_prior_macro_notes to check your own last real macro read before "
    "searching the web again, and web_search for the policy narrative and "
    "events behind those numbers (e.g. the latest FOMC decision, why "
    "inflation moved the way it did) -- numbers alone don't capture that "
    "context. The daily effective fed funds rate (DFF) and the 10-year "
    "Treasury yield (DGS10) are fetched live for you in code before your "
    "first turn -- their get_macro_indicators results are already in your "
    "scratchpad, so do not fetch them again (if one of those lookups "
    "failed you may retry it once). Follow this order: (1) "
    "get_macro_indicators for any OTHER indicator you need (e.g. cpi, "
    "unemployment_rate); (2) "
    "search_prior_macro_notes ONCE, right after, even though it may return "
    "nothing (expected and fine if no macro note has been archived yet -- "
    "don't retry it, just move on to step 3); (3) web_search, only if "
    "step 2 didn't already give you a current, non-stale policy read. "
    "Skipping straight from step 1 to web_search wastes a call you've "
    "already paid for elsewhere in this system -- your own most recent "
    "macro note may already answer this without needing a fresh search at "
    "all -- but a prior note's rate or yield figures are history: the "
    "current values are the live ones in your scratchpad. The live DFF "
    "result states the last policy-rate move and its date: describe the "
    "Fed's current stance from that move (e.g. a hike a week ago is not "
    "a hold), never from a prior note. The cpi result's latest= value is "
    "an index level, not an inflation rate: quote the year-over-year change "
    "that result states as the inflation rate. When you use "
    "web_search, write your summary in your "
    "own words based on multiple results; do not quote any single source at "
    "length, and mention which source(s) a claim is based on. Macro context "
    "changes slowly -- don't over-search.\n\n"
    "Write specific web_search queries (the event and its month, e.g. the "
    "latest FOMC decision), not broad ones. Each web_search result line "
    "ends with catalog=yes (an outlet in NewsAPI's source catalog) or "
    "catalog=no; these fields are for you, not the reader: never "
    "write them in your answer, name the source itself. "
    "A web_search result can be years old (for example an FOMC statement "
    "from long ago used as if it described current policy) -- always "
    "check the date on any web_search result before using "
    "its content, and if a result is marked [STALE SOURCE ...], do not use "
    "it as your basis for current conditions; search again with a more "
    "specific/recent query instead. When you cite a specific meeting or "
    "release date, quote it exactly as sourced -- don't approximate or "
    "recall it from memory. Do not present a claim from a catalog=no "
    "source with the same confidence as one from a catalog=yes source or "
    "from the issuing institution's own page. A result "
    "flagged [STALE ...] means the real policy stance may have shifted "
    "since -- treat it as background, not this run's current stance.\n\n"
    "search_prior_macro_notes deliberately returns notes from ANY ticker's "
    "prior run (macro commentary isn't ticker-specific, see that tool's own "
    "description), so carrying a retrieved note over essentially verbatim "
    "gives completely different tickers across unrelated industries (a "
    "beverage company, a chipmaker, a health insurer, a bank) macro "
    "sections that read as nearly identical prose, just with the company "
    "name swapped. Reusing a genuinely still-current macro "
    "backdrop is fine and expected -- macro conditions really don't "
    "change ticker to ticker -- but your final answer must ALWAYS include "
    "at least one distinct paragraph or sentence connecting that backdrop "
    "specifically to THIS ticker's own industry/sector (e.g. how the "
    "current rate environment specifically affects a bank's net interest "
    "margin vs. how it affects a semiconductor company's capex plans vs. "
    "a health insurer's investment income -- these are genuinely "
    "different mechanisms, not interchangeable prose). Carrying over the "
    "shared macro backdrop without this ticker-specific connection is "
    "not acceptable, even when the backdrop itself is accurate and not "
    "stale. You have no company data (no filings, results or cash-flow "
    "figures): do not state this company's own financials -- its revenue, "
    "earnings, margins, cash flow, capital spending or balance sheet, in "
    'figures or in words such as "robust cash flow" -- describe how the '
    "macro backdrop bears on its industry and business model instead.\n\n"
    "Whenever you set finish_answer, also fill in reported_metrics with "
    "exactly these two fields: fed_funds_rate_pct (the DFF latest= "
    "value from this run's get_macro_indicators result, as a plain number, "
    "e.g. 4.33) and ten_year_yield_pct (the DGS10 latest= value). Set a "
    "field to null when this run has no successful live lookup for that "
    "series -- never fill it from a prior note or a web page. Both are "
    "checked mechanically against this run's live FRED results.")

MACRO_REUSED_DISCLOSURE = (
    "This macro section is substantially reused from a prior analysis run's "
    "macro note; weigh its ticker-specific part accordingly.")


@dataclass(frozen=True)
class MacroReuse:
    reused: bool
    recycled_share: float

    def measures(self) -> dict[str, Any]:
        return {
            "reused": self.reused,
            "recycled_share": round(self.recycled_share, 2),
            "recycled_share_threshold": settings.ARCHIVAL_RECYCLED_SHARE_MAX
        }


def macro_reuse(summary: str, scratchpad: Sequence[Any]) -> MacroReuse:
    share = recycled_share(summary, scratchpad)
    return MacroReuse(
        reused=share >= settings.ARCHIVAL_RECYCLED_SHARE_MAX,
        recycled_share=share)


@dataclass(frozen=True)
class _LiveSeries:
    alias: str
    series_id: str
    metric: str


_LIVE_SERIES: tuple[_LiveSeries, ...] = (
    _LiveSeries("fed_funds_rate", "DFF", "fed_funds_rate_pct"),
    _LiveSeries("10y_treasury_yield", "DGS10", "ten_year_yield_pct"))
_FRED_LATEST_RE = re.compile(
    r"\((?P<series_id>[A-Z0-9]+)\): latest=(?P<value>-?\d+(?:\.\d+)?)%? "
    r"as of (?P<as_of>\d{4}-\d{2}-\d{2})")
_MACRO_METRIC_TOLERANCE = 0.005

_MACRO_REPORTED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        series.metric: {
            "anyOf": [{
                "type": "number"
            }, {
                "type": "null"
            }]
        }
        for series in _LIVE_SERIES
    },
    "additionalProperties": False
}


@dataclass(frozen=True)
class _LiveReading:
    value: float
    as_of: str


def _live_fred_readings(
        scratchpad: Sequence[ScratchpadEntry]) -> dict[str, _LiveReading]:
    readings: dict[str, _LiveReading] = {}
    for entry in scratchpad:
        if not is_successful_tool_entry(entry, "get_macro_indicators"):
            continue
        match = _FRED_LATEST_RE.search(entry.observation or "")
        if match:
            readings[match["series_id"]] = _LiveReading(
                float(match["value"]), match["as_of"])
    return readings


def _live_fred_line(readings: dict[str, _LiveReading]) -> str:
    parts = [(
        f"{s.series_id} {readings[s.series_id].value:.2f}% "
        f"({readings[s.series_id].as_of})" if s.series_id in readings else
        f"{s.series_id}: lookup FAILED this run") for s in _LIVE_SERIES]
    return "Live FRED this run: " + "; ".join(parts)


def _macro_metrics_disagree_with_live_fred(
        final_answer: str, reported_metrics: dict[str, Any] | None,
        scratchpad: list[ScratchpadEntry]) -> str | None:
    del final_answer
    readings = _live_fred_readings(scratchpad)
    metrics = reported_metrics or {}
    for series in _LIVE_SERIES:
        live = readings.get(series.series_id)
        claimed = metrics.get(series.metric)
        if live is None:
            if claimed is not None:
                return (
                    f"reported_metrics.{series.metric} must be null: this run "
                    f"has no successful live {series.series_id} lookup, so "
                    "any value would come from somewhere other than this "
                    "run's data.")
            continue
        if claimed is None:
            return (
                f"reported_metrics.{series.metric} is missing -- copy the "
                f"{series.series_id} latest= value ({live.value}) from this "
                "run's get_macro_indicators result.")
        issue = reported_number_issue(
            series.metric, claimed, [live.value], _MACRO_METRIC_TOLERANCE,
            f"reported_metrics.{series.metric}={{value}} does not "
            f"match this run's live {series.series_id} value of "
            f"{live.value} (as of {live.as_of}) -- use the live value, "
            "not one from a prior note or web page.")
        if issue is not None:
            return issue
    return None


async def _prefetch_live_fred(
        tool: GetMacroIndicatorsTool) -> list[ScratchpadEntry]:
    return list(
        await asyncio.gather(
            *(
                prefetch_tool_call(tool, {"series": series.alias})
                for series in _LIVE_SERIES)))


class MacroAgent(BaseAgent):

    async def run(self, state: AnalysisState) -> AgentOutput:
        rag_visibility = visibility_for_state(state)
        feedback = state.reflection_feedback.get(self.role)
        macro_tool = GetMacroIndicatorsTool()
        prefetched = await _prefetch_live_fred(macro_tool)
        prior_notes_tool = SearchResearchArchiveTool(
            visibility=rag_visibility, name="search_prior_macro_notes",
            description=(
                "Call this ONCE, right after get_macro_indicators "
                "and before web_search -- checking your own last "
                "real macro read is cheap and may make a fresh "
                "web_search unnecessary. Search AlphaMind's own "
                "prior Macro analysis notes (archived automatically "
                "after each real analysis run, across ALL tickers "
                "-- macro commentary isn't ticker-specific, so this "
                "deliberately searches everything, not just the "
                'current ticker). action_input: {"query": "Fed '
                'policy stance and rate trajectory"}. No ticker '
                "parameter -- this always searches broadly. May "
                "return nothing if no macro note has been archived "
                "yet -- that's expected, not an error; don't retry "
                "this call, just move on to web_search. A result "
                "older than roughly 45 days is flagged [STALE ...] "
                "-- the real policy stance can shift meaningfully "
                "between FOMC meetings; treat an older note as "
                "historical background, not this run's current "
                "stance, and proceed to web_search for a current "
                "read."), category=DocumentCategory.MACRO_RESEARCH,
            ticker=None)
        prefetched += await prefetch_tool_calls([(
            macro_tool, {
                "series": "cpi",
                "lookback_days": YOY_MIN_LOOKBACK_DAYS
            }), (prior_notes_tool, {
                "query": PREFETCH_MACRO_NOTES_QUERY
            })])
        loop = ReActAgentLoop(
            role=self.role,
            tools=[macro_tool, prior_notes_tool,
                   WebSearchTool()], system_prompt=_SYSTEM_PROMPT,
            max_iterations=MAX_ITERATIONS,
            finish_answer_validator=InternalTokenValidator(
                self.role.value, _macro_metrics_disagree_with_live_fred,
                once_checks=[
                    (OWN_EVIDENCE_CHECK, own_evidence_figure_check())
                ]), reported_metrics_schema=_MACRO_REPORTED_SCHEMA)
        result = await loop.run(
            f"Summarize the macroeconomic context relevant to {state.ticker} "
            "(rates, sector rotation, market regime).",
            extra_context=combine_context(
                feedback, state.procedural_hints.get(self.role)),
            prefetched_evidence=prefetched)
        readings = _live_fred_readings(result.scratchpad)
        summary = f"{result.final_answer}\n\n{_live_fred_line(readings)}"
        verification_warnings: list[str] = []
        failed_series = [
            s.series_id for s in _LIVE_SERIES if s.series_id not in readings
        ]
        if failed_series:
            logger.warning(
                "macro.live_fred_lookup_failed", ticker=state.ticker,
                series=failed_series)
            verification_warnings.append(
                "Live FRED lookup failed this run for "
                f"{', '.join(failed_series)} -- any figure for "
                f"{'that series' if len(failed_series) == 1 else 'those series'} "  # noqa: E501
                "in this section is not from this run's data.")
        reuse = macro_reuse(summary, result.scratchpad)
        disclosures: list[str] = []
        reused_prior_note = reuse.reused
        reuse_measures = reuse.measures()
        if reused_prior_note:
            logger.info(
                "macro.summary_near_duplicate_of_retrieved_note",
                ticker=state.ticker, **reuse_measures)
            disclosures.append(MACRO_REUSED_DISCLOSURE)
        summary = flag_unresolved_issue(
            result, summary, verification_warnings,
            event="macro.unresolved_finish_issue_after_retries",
            ticker=state.ticker, consequence=(
                " -- this was not corrected even after being flagged -- treat "
                "the rate figures below as unverified until manually "
                "re-checked against this run's live FRED data."))
        return self.completed_output(
            result, feedback=feedback, summary=summary,
            verification_warnings=verification_warnings,
            recycled=reused_prior_note, raw_output={
                "live_fred": {
                    s.metric: ({
                        "value": readings[s.series_id].value,
                        "as_of": readings[s.series_id].as_of
                    } if s.series_id in readings else None)
                    for s in _LIVE_SERIES
                },
                "final_answer": result.final_answer,
                "reused_prior_note": reused_prior_note,
                "reuse_measures": reuse_measures,
                "disclosures": disclosures
            })
