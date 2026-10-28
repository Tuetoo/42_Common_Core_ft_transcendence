import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
import structlog
from ai.agents.evidence_grounding import wrap_verification_warning
from ai.agents.finish_checks import (OWN_EVIDENCE_CHECK,
                                     InternalTokenValidator,
                                     own_evidence_figure_check)
from ai.agents.loop import (BaseAgent, ReActAgentLoop, ScratchpadEntry,
                            combine_context, is_successful_tool_entry,
                            is_thin_evidence, prefetch_tool_call,
                            prefetch_tool_calls, tool_reported_error)
from ai.graph.state import AgentOutput, AnalysisState
from ai.knowledge.recycled_content import (prior_note_only_claims_in_run,
                                           prior_note_similarity)
from ai.providers.news_relevance import lookup_search_name
from ai.rag.visibility import visibility_for_state
from ai.tools.base import Tool
from ai.tools.news_tool import (SearchNewsTool, live_search_yield,
                                parse_results_header, ran_live_search)
from ai.tools.research_archive_tool import SearchResearchArchiveTool
from ai.tools.web_search_tool import WebSearchTool
from config import settings
from shared.enums import DocumentCategory

logger = structlog.get_logger(__name__)

PREFETCH_PRIOR_NOTES_QUERY = "recent material developments"
PREFETCH_NEWS_QUERY = "latest news"
MAX_ITERATIONS = 7

_SYSTEM_PROMPT = (
    "You are the News Agent in AlphaMind's multi-agent investment research "
    "system. Your job is to search recent news about a ticker, deduplicate "
    "overlapping coverage, and weigh items by source quality.\n\n"
    "Order: your very first action is search_prior_news_notes (what earlier "
    "runs already covered for this ticker); your very next action must be "
    "search_news, not finish_answer, however complete the prior notes look: "
    "they tell you what was known at "
    "an earlier run, never what is new today. Finishing before search_news "
    "or web_search has run is rejected. Only after search_news, decide from "
    "its results whether a web_search is warranted: search_news gives "
    "headline+summary snippets from a news index, web_search gives fuller "
    "context on a specific development.\n\n"
    "Discard any result that is not materially about this company, even "
    "when a tool returned it. Every search_news and web_search line ends "
    "with catalog=yes (an outlet in NewsAPI's source catalog) or "
    "catalog=no: give a claim from a catalog=no source less weight than "
    "one from a catalog=yes source, and say so when sources "
    "disagree. Items tagged [consumer product] or [opinion/listicle] are "
    "weaker evidence than business reporting. Items tagged [unverified] "
    "could not be checked for relevance and do not count as evidence: use "
    "one only when its own text is clearly about this company. The "
    'bracketed markers, the catalog= fields and the words "link '
    'unavailable" in tool results are labels for you, not for the reader: '
    "never write them in your answer; name the outlet itself. "
    "Write in your own words and "
    "do not quote any source at length. A web_search result marked "
    "[STALE SOURCE ...] is not current news: search again with a more "
    "specific query instead.\n\n"
    "Figures: when two figures could be about different scopes (a total "
    "vs. one part of it, a segment vs. one product), name the scope of each "
    "and state them separately. Never write a period-over-period change as "
    '"rose X% to $Y": name the current figure first and the prior figure '
    'as the base, e.g. "current-quarter revenue is $11.5B, up ~50% from '
    '$7.69B a year earlier".\n\n'
    "Prior coverage: an item search_prior_news_notes already covered is not "
    "a new catalyst unless a fresh search shows a material update since "
    "(a new number, a resolution, an escalation). A prior note flagged "
    "[STALE ...] is background you already know. Write the summary from "
    "this run's search results, not from a prior note: do not reuse a "
    "prior note's wording, and do not carry over a detail (a place, a "
    "date, a figure) that only the prior note states.\n\n"
    "When the prefetched search_news failed, a web_search for the company "
    "was already run in code and is in your scratchpad: use it, and if "
    "search_news says the request limit was reached, do not call it again "
    "this run.\n\n"
    "Each search_news item starts with its publication date and weekday, "
    'e.g. [2026-10-01 Thu]. When an item says something happened "on '
    'Monday" (or another weekday), resolve it to the latest such weekday '
    "on or before the item's date (an item dated [2026-10-01 Thu] saying "
    '"on Wednesday" means 2026-09-30) and write that exact date; never '
    "replace a weekday-dated event with a month-level phrase such as "
    '"early October". Write every date as YYYY-MM-DD, without weekday '
    "names.")


def _skipped_live_search(scratchpad: list[ScratchpadEntry]) -> bool:
    return not ran_live_search(scratchpad)


_NEWS_VALIDATION_SUFFIX = (
    "Fix only what the check above names, using the tool results already in "
    "this scratchpad (or one new search if it asks for one), keep every other "
    "correct fact, then set finish_answer again.")


def _reject_finish_before_live_search(
        final_answer: str, reported_metrics: dict[str, Any] | None,
        scratchpad: list[ScratchpadEntry]) -> str | None:
    if _skipped_live_search(scratchpad):
        return (
            "You are trying to finish without ever running search_news or "
            "web_search this run (a call that errored before searching does "
            "not count) -- only search_prior_news_notes, which "
            "tells you what a PRIOR run already knew, not what's new "
            "today. Call search_news now with a real query about this "
            "ticker's recent developments, look at its actual results, "
            "then decide whether to finish or run one more search. Do not "
            "set finish_answer again until you have done that.")
    return None


def fallback_query(ticker: str, company_name: str | None) -> str:
    return f"{company_name or ticker} company news"


async def prefetch_web_fallback(
        prefetched: list[ScratchpadEntry], ticker: str, web_tool: Tool,
        company_name: str | None = None) -> list[ScratchpadEntry]:
    news = next((e for e in prefetched if e.action == "search_news"), None)
    if news is None or not tool_reported_error(str(news.observation or "")):
        return []
    query = fallback_query(ticker, company_name)
    logger.warning("news.web_search_fallback", ticker=ticker, query=query)
    return [await prefetch_tool_call(web_tool, {"query": query, "limit": 5})]


FALLBACK_UNVERIFIED_REASON = (
    "NewsAPI was unavailable this run and no web source behind this "
    "summary is in NewsAPI's source catalog; treat its claims and its "
    "sentiment reading as unconfirmed.")


def fallback_sources_unverified(scratchpad: list[ScratchpadEntry]) -> bool:
    if any(is_successful_tool_entry(e, "search_news") for e in scratchpad):
        return False
    headers = [
        parse_results_header(str(e.observation or "")) for e in scratchpad
        if is_successful_tool_entry(e, "web_search")
    ]
    with_results = [h for h in headers if h is not None and h.total > 0]
    if not with_results:
        return False
    counts = [h.catalog_outlets for h in with_results]
    return (
        all(c is not None for c in counts)
        and sum(c or 0 for c in counts) == 0)


def prior_news_note_similarity(
        final_answer: str, scratchpad: list[ScratchpadEntry]) -> float | None:
    return prior_note_similarity(
        final_answer,
        [
            e for e in scratchpad
            if is_successful_tool_entry(e, "search_prior_news_notes")
        ])


def _prior_note_ratio(
        final_answer: str, scratchpad: list[ScratchpadEntry]) -> float | None:
    try:
        return prior_news_note_similarity(final_answer, scratchpad)
    except Exception as exc:
        logger.warning("news.prior_note_similarity_failed", error=str(exc))
        return None


def _prior_note_claims(final_answer: str,
                       scratchpad: list[ScratchpadEntry]) -> list[str]:
    try:
        return prior_note_only_claims_in_run(final_answer, scratchpad)
    except Exception as exc:
        logger.warning("news.prior_note_claims_failed", error=str(exc))
        return []


_WEEKDAY_NAMES = tuple(calendar.day_name)
_WEEKDAY_ABBREVIATIONS: dict[str, int] = {
    "Mon": 0,
    "Tue": 1,
    "Tues": 1,
    "Wed": 2,
    "Thu": 3,
    "Thur": 3,
    "Thurs": 3,
    "Fri": 4,
    "Sat": 5,
    "Sun": 6
}
_WEEKDAY_INDEX: dict[str, int] = {
    **{
        name: i
        for i, name in enumerate(_WEEKDAY_NAMES)
    },
    **_WEEKDAY_ABBREVIATIONS
}
_MONTH_INDEX: dict[str, int] = {
    **{
        name: i
        for i, name in enumerate(calendar.month_name) if name
    },
    **{
        name: i
        for i, name in enumerate(calendar.month_abbr) if name
    }, "Sept": 9
}


def _alternation(names: Any) -> str:
    return "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))


_WD = rf"(?P<wd>{_alternation(_WEEKDAY_INDEX)})\.?"
_MON = rf"(?P<mon>{_alternation(_MONTH_INDEX)})\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?!\d)(?!\s?[-\u2013]\s?\d)"
_YEAR = r"(?:,?\s+(?P<year>\d{4})(?!\d))?"
_MONTH_DAY = rf"{_MON}\s+{_DAY}{_YEAR}"
_DAY_MONTH = rf"{_DAY}\s+{_MON}{_YEAR}"
_ISO_DATE = r"(?P<iso>\d{4}-\d{2}-\d{2})(?!\d)"
_WEEKDAY_DATE_RES = tuple(
    re.compile(pattern) for pattern in (
        *(
            form for date_form in (_MONTH_DAY, _DAY_MONTH, _ISO_DATE)
            for form in (
                rf"(?<![A-Za-z]){_WD},?\s+(?:the\s+)?{date_form}",
                rf"(?<![A-Za-z]){_WD}\s*\(\s*{date_form}\s*\)",
                rf"{date_form}\s*\(\s*{_WD}\s*\)")),
        rf"{_ISO_DATE},?\s+{_WD}(?![A-Za-z])"))


@dataclass(frozen=True)
class WeekdayMismatch:
    written: str
    actual_weekday: str


def _resolve_year(month: int, day: int, reference: date) -> date | None:
    candidates: list[date] = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - reference).days))


def _match_date(match: re.Match[str], reference: date) -> date | None:
    groups = match.groupdict()
    try:
        if groups.get("iso"):
            return date.fromisoformat(groups["iso"])
        month = _MONTH_INDEX[groups["mon"]]
        day = int(groups["day"])
        if groups.get("year"):
            return date(int(groups["year"]), month, day)
    except ValueError:
        return None
    return _resolve_year(month, day, reference)


def weekday_date_mismatches(text: str,
                            reference: date) -> list[WeekdayMismatch]:
    found: dict[tuple[int, int], WeekdayMismatch] = {}
    seen_spans: list[tuple[int, int]] = []
    for pattern in _WEEKDAY_DATE_RES:
        for match in pattern.finditer(text):
            span = match.span()
            if any(s <= span[0] < e or s < span[1] <= e
                   for s, e in seen_spans):
                continue
            day = _match_date(match, reference)
            if day is None:
                continue
            seen_spans.append(span)
            written_index = _WEEKDAY_INDEX[match.group("wd")]
            if written_index == day.weekday():
                continue
            found[span] = WeekdayMismatch(
                written=match.group(0),
                actual_weekday=_WEEKDAY_NAMES[day.weekday()])
    return [found[span] for span in sorted(found)]


NEWS_PRIOR_NOTE_COPY_MESSAGE = (
    "Prior-note copy check: your summary repeats a prior AI-written News "
    "note returned this run almost word for word (similarity {ratio:.2f}). "
    "Prior notes are unverified and have carried wrong dates and details "
    "forward. Rewrite the summary from this run's search_news / web_search "
    "results: state each development as an item from this run reports it, "
    "with that item's date, drop any detail only the prior note states, "
    "and do not copy the note's wording.")

PRIOR_NOTE_CLAIMS_MESSAGE = (
    "Prior-note check: {claims} in your summary appear only in a prior "
    "AI-written News note returned this run, not in any search result of "
    "this run. Prior notes are unverified leads. For each of them either "
    "drop it or replace it with what a search_news / web_search item of "
    "this run states (one new search is allowed to confirm it), and check "
    "the other figures you took from that note the same way.")


def _prior_note_copy_issue(
        final_answer: str, reported_metrics: dict[str, Any] | None,
        scratchpad: list[ScratchpadEntry]) -> str | None:
    del reported_metrics
    ratio = _prior_note_ratio(final_answer, scratchpad)
    if ratio is None or ratio < settings.PRIOR_NOTE_MAX_SIMILARITY:
        return None
    return NEWS_PRIOR_NOTE_COPY_MESSAGE.format(ratio=ratio)


def _prior_note_claims_issue(
        final_answer: str, reported_metrics: dict[str, Any] | None,
        scratchpad: list[ScratchpadEntry]) -> str | None:
    del reported_metrics
    claims = _prior_note_claims(final_answer, scratchpad)
    if not claims:
        return None
    return PRIOR_NOTE_CLAIMS_MESSAGE.format(
        claims=", ".join(f'"{c}"' for c in claims[:6]))


class _NewsFinishValidator(InternalTokenValidator):

    def __init__(self) -> None:
        super().__init__(
            "news", base=_reject_finish_before_live_search,
            once_checks=[("prior_note_copy", _prior_note_copy_issue),
                         ("prior_note_claims", _prior_note_claims_issue),
                         (OWN_EVIDENCE_CHECK, own_evidence_figure_check())])


class NewsAgent(BaseAgent):

    async def run(self, state: AnalysisState) -> AgentOutput:
        rag_visibility = visibility_for_state(state)
        feedback = state.reflection_feedback.get(self.role)
        prior_notes_tool = SearchResearchArchiveTool(
            visibility=rag_visibility, name="search_prior_news_notes",
            description=(
                "Call this FIRST, before search_news/web_search -- "
                "checking your own prior coverage of this ticker is "
                "cheap and may show a headline you're about to "
                "surface has already been covered and assessed. "
                "Search AlphaMind's own prior News analysis notes "
                "for this ticker (archived automatically after each "
                'real analysis run). action_input: {"query": '
                '"recent material developments"}. May return '
                "nothing if this ticker has "
                "never been analyzed before -- that's expected, not "
                "an error; don't retry, just move on to search_news. "
                "A result older than 3 days is flagged [STALE ...] "
                "-- treat it as something you already know about, "
                "not as a current development, unless a fresh "
                "search shows it has genuinely moved since."),
            category=DocumentCategory.NEWS, ticker=state.ticker)
        web_tool = WebSearchTool(ticker=state.ticker)
        news_tool = SearchNewsTool(
            ticker=state.ticker, fallback_tool=web_tool.name)
        fallback_web_tool = WebSearchTool(
            ticker=state.ticker, news_days=settings.NEWS_WEB_FALLBACK_DAYS)
        prefetched = await prefetch_tool_calls([
            (prior_notes_tool, {
                "query": PREFETCH_PRIOR_NOTES_QUERY
            }), (news_tool, {
                "query": PREFETCH_NEWS_QUERY,
                "limit": 10
            })
        ])
        if any(e.action == "search_news"
               and tool_reported_error(str(e.observation or ""))
               for e in prefetched):
            prefetched += await prefetch_web_fallback(
                prefetched, state.ticker, fallback_web_tool,
                company_name=await lookup_search_name(state.ticker))
        loop = ReActAgentLoop(
            role=self.role, tools=[prior_notes_tool, news_tool, web_tool],
            system_prompt=_SYSTEM_PROMPT, max_iterations=MAX_ITERATIONS,
            finish_answer_validator=_NewsFinishValidator(),
            finish_validation_suffix=_NEWS_VALIDATION_SUFFIX,
            evidence_yield=live_search_yield)
        result = await loop.run(
            f"Summarize recent news sentiment and material developments for "
            f"{state.ticker}.", extra_context=combine_context(
                feedback, state.procedural_hints.get(self.role)),
            prefetched_evidence=prefetched)
        summary = result.final_answer
        verification_warnings: list[str] = []
        if result.unresolved_finish_validation_issue is not None:
            logger.warning(
                "news.finish_issue_unresolved", ticker=state.ticker, kind=(
                    "live_search"
                    if _skipped_live_search(result.scratchpad) else "generic"),
                issue=result.unresolved_finish_validation_issue)
        if _skipped_live_search(result.scratchpad):
            logger.warning(
                "news.finished_without_live_search", ticker=state.ticker)
            reason = (
                "This run finished without ever running search_news or "
                "web_search -- the summary relies entirely on previously "
                "archived coverage (search_prior_news_notes), which may be "
                "stale.")
            summary = wrap_verification_warning(reason, summary)
            verification_warnings.append(reason)
        evidence_yield = live_search_yield(result.scratchpad)
        no_evidence = evidence_yield == 0
        if no_evidence:
            logger.warning("news.live_search_no_evidence", ticker=state.ticker)
            summary = (
                "[NO EVIDENCE: this run's live news search (search_news/"
                "web_search) returned 0 relevant articles.]\n\n" + summary)
        unverified_fallback = not no_evidence and fallback_sources_unverified(
            result.scratchpad)
        if unverified_fallback:
            logger.warning(
                "news.fallback_sources_unverified", ticker=state.ticker,
                evidence_yield=evidence_yield)
            summary = wrap_verification_warning(
                FALLBACK_UNVERIFIED_REASON, summary)
            verification_warnings.append(FALLBACK_UNVERIFIED_REASON)
        prior_ratio = _prior_note_ratio(result.final_answer, result.scratchpad)
        recycled = (
            prior_ratio is not None
            and prior_ratio >= settings.PRIOR_NOTE_MAX_SIMILARITY)
        if recycled:
            logger.info(
                "news.summary_near_duplicate_of_prior_note",
                ticker=state.ticker,
                similarity_ratio=round(prior_ratio or 0.0, 2),
                summary_snippet=result.final_answer[:120])
        mismatches = weekday_date_mismatches(
            result.final_answer,
            datetime.now(UTC).date())
        if mismatches:
            logger.warning(
                "news.weekday_date_mismatch", ticker=state.ticker,
                mismatches=[{
                    "written": m.written,
                    "actual_weekday": m.actual_weekday
                } for m in mismatches])
        return self.completed_output(
            result, feedback=feedback, summary=summary,
            verification_warnings=verification_warnings,
            no_evidence=no_evidence,
            thin_evidence=is_thin_evidence(evidence_yield)
            or unverified_fallback, recycled=recycled, raw_output={
                "no_evidence": no_evidence,
                "evidence_yield": evidence_yield,
                "fallback_sources_unverified": unverified_fallback,
                "reused_prior_note": recycled
            })
