import re
from typing import Any
import structlog
from ai.agents.loop import NUMBER_MATCH_REL_TOLERANCE
from ai.graph.state import (ISSUE_LLM_REVIEW, AgentOutput, AnalysisState,
                            DebateArgument, DebateSide)
from ai.llm.base import (AGENT_LLM_CALL_RETRIES, LLMError, LLMMessage,
                         LLMTier)
from ai.llm.router import (LLMRouter, fallback_call_count,
                           recording_served_models)
from shared.enums import AgentRole, AgentStatus

logger = structlog.get_logger(__name__)


def _is_flagged(out: AgentOutput) -> bool:
    return isinstance(out.raw_output, dict) and bool(
        out.raw_output.get("verification_warnings"))


def _evidence_lines(state: AnalysisState, *,
                    exclude_flagged: bool) -> list[str]:
    quarter = latest_quarter(state)
    return [
        f"- {out.role.value}: {_evidence_summary(out, quarter)}"
        f"{_disclosure_suffix(out)}" for out in state.agent_outputs.values()
        if not (exclude_flagged and _is_flagged(out))
    ]


# Fundamental's summary carries the latest-quarter text for the UI and the
# archived note; in the evidence the code-built line carries it once.
def _evidence_summary(
        out: AgentOutput, quarter: dict[str, Any] | None) -> str:
    if out.role != AgentRole.FUNDAMENTAL or quarter is None:
        return out.summary
    text = str(quarter["text"])
    for appended, rest in ((f"\n\n{text}\n", "\n\n"), (f"\n\n{text}", "")):
        if appended in out.summary:
            return out.summary.replace(appended, rest, 1)
    return out.summary


def _disclosure_suffix(out: AgentOutput) -> str:
    raw = out.raw_output if isinstance(out.raw_output, dict) else {}
    notes = [
        n for n in raw.get("disclosures") or [] if isinstance(n, str) and n
    ]
    if not notes:
        return ""
    return (
        " (System note, not part of the agent's text: " + " ".join(notes) +
        ")")


def format_evidence(state: AnalysisState) -> str:
    lines = _evidence_lines(state, exclude_flagged=False)
    flagged_roles = sorted(
        out.role.value for out in state.agent_outputs.values()
        if _is_flagged(out))
    if flagged_roles:
        lines.insert(
            0, "UNVERIFIED CLAIMS FLAGGED this run by: "
            f"{', '.join(flagged_roles)} -- see their own "
            "[VERIFICATION WARNING: ...] line(s) below before treating "
            "anything they reported as settled fact.")
    lines.extend(f"- {line}" for line in _code_built_evidence_lines(state))
    return "\n".join(lines)


def format_grounding_reference(state: AnalysisState) -> str:
    lines = _evidence_lines(state, exclude_flagged=True)
    lines.extend(_code_built_evidence_lines(state))
    return (
        "\n".join(lines)
        if lines else "(no unflagged agent evidence available)")


def format_prior_reflection_issues(
        state: AnalysisState, side: DebateSide) -> str | None:
    if not state.reflection_history:
        return None
    problems: list[str] = []
    unanswered: list[str] = []
    for issue in state.reflection_history[-1].issue_details:
        if issue.role != side or issue.code != ISSUE_LLM_REVIEW:
            continue
        (unanswered if issue.kind == "not_engaged" else problems).append(
            issue.text)
    if not problems and not unanswered:
        return None
    parts: list[str] = []
    if problems:
        bullets = "\n".join(f"- {issue}" for issue in problems)
        parts.append(
            "Reflection flagged these SPECIFIC problems in your own previous "
            "argument -- each one is a claim that could not be traced to any "
            "real evidence, a figure given for the wrong period, or an older "
            "figure presented as current:\n" + bullets + "\n\n"
            "For a wrong period, keep the figure only with the period the "
            "evidence gives it (or drop it); for an older figure presented as "
            "current, state the newest period's figure and its change "
            "instead. "
            "Hard rule: do not repeat any of these claims in this round, "
            "not even reworded, rephrased, or with a softened number. If, "
            "after reviewing the evidence again, you still cannot point to "
            "the exact sentence supporting one of these claims, the correct "
            "action is to OMIT it entirely -- a shorter argument missing that "
            "point is correct output; repeating a claim already identified as "
            "unsupported is not, regardless of how central it felt to your "
            "case.")
    if unanswered:
        bullets = "\n".join(f"- {issue}" for issue in unanswered)
        parts.append(
            "Reflection found that your previous argument did not answer "
            "these points of the other side (or sounded more certain than "
            "its evidence) -- answer them in this round, from the evidence:\n"
            + bullets)
    return "\n\n".join(parts)


_SCALE_WORDS = r"trillion|billion|million|trn|tn|bn|mln|mn|[BMKT]"
_DOLLAR = r"\$(?:[ \u00a0](?=\d))?"
_PAREN_AMOUNT = r"\$[ \u00a0]?\(\d[\d,]*(?:\.\d+)?\)"
_FIGURE_RE = re.compile(
    rf"(?:{_PAREN_AMOUNT}|{_DOLLAR}[\d,]+(?:\.\d+)?)\s*(?:{_SCALE_WORDS})\b"
    r"|\d+(?:\.\d+)?(?: ?%| percent\b)"
    rf"|{_PAREN_AMOUNT}"
    rf"|{_DOLLAR}\d[\d,]*(?:\.\d+)?", re.IGNORECASE)
_NUMBER = r"\d(?:[\d,]*\d)?(?:\.\d+)?"
_RANGE_DASH = r"\s?[-\u2013\u2014]\s?"
_RANGE_RE = re.compile(
    rf"(?<![\w.$])(?P<cur>{_DOLLAR})(?P<a>{_NUMBER})"
    rf"(?:{_RANGE_DASH}|\s+to\s+)(?:{_DOLLAR})?(?P<b>{_NUMBER})(?![.,]?\d)"
    rf"(?:\s*(?P<scale>{_SCALE_WORDS})\b)?(?!\s?%)"
    rf"|(?<![\w.$\-])(?P<pa>{_NUMBER})%?[-\u2013\u2014]"
    rf"(?P<pb>{_NUMBER})\s?%", re.IGNORECASE)


_PERCENT_WORD_RE = re.compile(r" percent$", re.IGNORECASE)


def _range_ends(match: re.Match[str]) -> tuple[str, str] | None:
    percent = match.group("pa") is not None
    low, high = match.group("pa", "pb") if percent else match.group("a", "b")
    if float(low.replace(",", "")) > float(high.replace(",", "")):
        return None
    if percent:
        return f"{low}%", f"{high}%"
    scale = f" {match.group('scale')}" if match.group("scale") else ""
    return f"${low}{scale}", f"${high}{scale}"


def _figure_readings(text: str) -> list[tuple[str, str]]:
    readings: list[tuple[str, str]] = []
    rest = list(text)
    for match in _RANGE_RE.finditer(text):
        ends = _range_ends(match)
        if ends is None:
            continue
        readings.extend((match.group(0), end) for end in ends)
        rest[match.start():match.end()] = " " * (match.end() - match.start())
    readings.extend(
        (m.group(0), _PERCENT_WORD_RE.sub("%", m.group(0)))
        for m in _FIGURE_RE.finditer("".join(rest)))
    return readings


def _range_plain_values(text: str) -> list[tuple[float, float]]:
    return [
        _evidence_number(figure) for _, figure in _figure_readings(text)
        if _is_plain_dollar(figure)
    ]


_CITATION_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")


def extract_citations(argument_text: str,
                      valid_roles: set[AgentRole]) -> list[str]:
    found: set[AgentRole] = set()
    for match in _CITATION_BRACKET_RE.finditer(argument_text):
        for raw_token in match.group(1).split(","):
            token = raw_token.strip().lower().replace(" ", "_")
            if not token:
                continue
            try:
                role = AgentRole(token)
            except ValueError:
                continue
            if role in valid_roles:
                found.add(role)
    return sorted(role.value for role in found)


_NUMERAL_CORE_RE = re.compile(r"[\d,]+(?:\.\d+)?")
_SCALE_MULTIPLIERS = {
    "trillion": 1_000_000_000_000,
    "t": 1_000_000_000_000,
    "tn": 1_000_000_000_000,
    "trn": 1_000_000_000_000,
    "billion": 1_000_000_000,
    "b": 1_000_000_000,
    "bn": 1_000_000_000,
    "million": 1_000_000,
    "m": 1_000_000,
    "mn": 1_000_000,
    "mln": 1_000_000,
    "k": 1_000
}
_SCALE_SUFFIX_RE = re.compile(rf"({_SCALE_WORDS})\b", re.IGNORECASE)


def _scale_multiplier(figure: str) -> int | None:
    scale_match = _SCALE_SUFFIX_RE.search(figure)
    if scale_match is None:
        return None
    return _SCALE_MULTIPLIERS.get(scale_match.group(0).lower())


def _numeral_core(figure: str) -> str:
    match = _NUMERAL_CORE_RE.search(figure)
    if match is None:
        return figure
    digits = match.group(0).replace(",", "")
    multiplier = _scale_multiplier(figure)
    if multiplier is None:
        return digits
    try:
        return str(float(digits) * multiplier)
    except ValueError:
        return digits


FIGURE_MATCH_REL_TOLERANCE = 0.015
_PRECISE_MIN_SIGNIFICANT_DIGITS = 3

_EvidenceNumber = tuple[float, float]


def _whole_and_decimals(figure: str) -> tuple[str, str]:
    match = _NUMERAL_CORE_RE.search(figure)
    digits = match.group(0).replace(",", "") if match else ""
    whole, _, decimals = digits.partition(".")
    return whole, decimals


def _precise_unit(figure: str) -> float | None:
    whole, decimals = _whole_and_decimals(figure)
    if decimals:
        significant = (whole + decimals).lstrip("0")
        unit = 10.0**-len(decimals)
    else:
        significant = whole.strip("0")
        unit = 10.0**(len(whole) - len(whole.rstrip("0")))
    if len(significant) < _PRECISE_MIN_SIGNIFICANT_DIGITS:
        return None
    return unit * (_scale_multiplier(figure) or 1)


def _evidence_number(figure: str) -> _EvidenceNumber:
    _, decimals = _whole_and_decimals(figure)
    unit = (
        10.0**-len(decimals) * (_scale_multiplier(figure) or 1)
        if decimals else 0.0)
    return float(_numeral_core(figure)), unit


def _figure_matches(figure: str, value: float, evidence: _EvidenceNumber,
                    rel_tolerance: float) -> bool:
    evidence_value, evidence_unit = evidence
    if value == evidence_value:
        return True
    unit = _precise_unit(figure)
    if unit is not None:
        return abs(value - evidence_value) <= max(
            unit, evidence_unit) * (1 + 1e-9)
    return evidence_value != 0 and abs(
        value - evidence_value) <= rel_tolerance * abs(evidence_value)


def find_ungrounded_figures(argument_text: str,
                            evidence_text: str) -> set[str]:
    argument_figures = _figure_readings(argument_text)
    if not argument_figures:
        return set()
    evidence_figures = [
        figure for _, figure in _figure_readings(evidence_text)
        if not _is_plain_dollar(figure)
    ]
    evidence_cores = {_numeral_core(figure) for figure in evidence_figures}
    evidence_numbers: list[_EvidenceNumber] = []
    for figure in evidence_figures:
        try:
            evidence_numbers.append(_evidence_number(figure))
        except ValueError:
            continue
    evidence_plain_numbers: list[_EvidenceNumber] | None = None

    def _is_grounded(fig: str) -> bool:
        nonlocal evidence_plain_numbers
        if _is_plain_dollar(fig):
            if evidence_plain_numbers is None:
                evidence_plain_numbers = _plain_numbers(
                    evidence_text) + _range_plain_values(evidence_text)
            return _plain_dollar_grounded(fig, evidence_plain_numbers)
        core = _numeral_core(fig)
        if core in evidence_cores:
            return True
        try:
            value = float(core)
        except ValueError:
            return False
        return any(
            _figure_matches(fig, value, evidence, FIGURE_MATCH_REL_TOLERANCE)
            for evidence in evidence_numbers)

    return {shown for shown, fig in argument_figures if not _is_grounded(fig)}


def scaled_figures_matching_plain_dollars(
        figures: set[str], evidence_text: str) -> set[str]:
    dollars: list[_EvidenceNumber] = []
    for _, figure in _figure_readings(evidence_text):
        if _is_plain_dollar(figure):
            try:
                dollars.append((float(_numeral_core(figure)), 0.0))
            except ValueError:
                continue
    matched: set[str] = set()
    for shown in figures:
        readings = _figure_readings(shown)
        if len(readings) != 1:
            continue
        fig = readings[0][1]
        if not fig.startswith("$") or _scale_multiplier(fig) is None:
            continue
        try:
            value = float(_numeral_core(fig))
        except ValueError:
            continue
        if any(
                _figure_matches(fig, value, number, FIGURE_MATCH_REL_TOLERANCE)
                for number in dollars):
            matched.add(shown)
    return matched


_PLAIN_NUMBER_RE = re.compile(
    r"(?<![\w.\-])\d[\d,]*(?:\.\d+)?(?![\w%]|-\d|\.\d)")


def _is_plain_dollar(fig: str) -> bool:
    return fig.startswith("$") and _SCALE_SUFFIX_RE.search(fig) is None


def _plain_numbers(text: str) -> list[_EvidenceNumber]:
    return [_evidence_number(raw) for raw in _PLAIN_NUMBER_RE.findall(text)]


def _plain_dollar_grounded(fig: str,
                           evidence_numbers: list[_EvidenceNumber]) -> bool:
    try:
        value = float(_numeral_core(fig))
    except ValueError:
        return False
    return any(
        _figure_matches(fig, value, evidence, NUMBER_MATCH_REL_TOLERANCE)
        for evidence in evidence_numbers)


def _quote_is_grounded(quote: str, evidence_text: str) -> bool:
    normalized_quote = " ".join(quote.split()).casefold()
    normalized_evidence = " ".join(evidence_text.split()).casefold()
    if not normalized_quote:
        return False
    return normalized_quote in normalized_evidence


DEBATE_MAX_REGENERATIONS = 1

_OTHER_SIDE: dict[str, DebateSide] = {
    "bull": "bear",
    "bear": "bull"
}


def _ungrounded_issue(figures: set[str]) -> str:
    return (
        "These figures have no matching number anywhere in the evidence: "
        f"{', '.join(sorted(figures))}.")


async def generate_debate_argument(
        *, side: DebateSide, state: AnalysisState, llm_router: LLMRouter,
        llm_tier: LLMTier, system_prompt: str) -> DebateArgument:
    with recording_served_models() as served:
        argument = await _generate_debate_argument(
            side=side, state=state, llm_router=llm_router, llm_tier=llm_tier,
            system_prompt=system_prompt)
    return argument.model_copy(
        update={"fallback_calls": fallback_call_count(served)})


async def _generate_debate_argument(
        *, side: DebateSide, state: AnalysisState, llm_router: LLMRouter,
        llm_tier: LLMTier, system_prompt: str) -> DebateArgument:
    round_number = state.debate.round_number + 1
    other = _OTHER_SIDE[side]
    evidence_text = format_evidence(state)
    user_parts = [
        f"Ticker: {state.ticker}", f"Research agent evidence:\n{evidence_text}"
    ]
    prior_other = state.debate.previous_round_argument(other)
    if prior_other is not None:
        user_parts.append(
            f"{other.capitalize()}'s previous-round argument to respond "
            f"to:\n{prior_other.argument}")
    prior_issues = format_prior_reflection_issues(state, side)
    if prior_issues is not None:
        user_parts.append(prior_issues)
    base_user_content = "\n\n".join(user_parts)

    async def _write(user_content: str) -> str:
        response = await llm_router.complete(
            llm_tier, [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(
                    role="user", content=user_content,
                    shared_evidence=evidence_text)
            ], retries=AGENT_LLM_CALL_RETRIES)
        return response.content.strip()

    grounding_text = format_grounding_reference(state)

    def _ungrounded(text: str, draft: int) -> set[str]:
        ungrounded = find_ungrounded_figures(text, grounding_text)
        if ungrounded:
            logger.warning(
                f"{side}.ungrounded_figures", ticker=state.ticker,
                round_number=round_number, draft=draft,
                ungrounded_figures=sorted(ungrounded))
        return ungrounded

    argument_text = await _write(base_user_content)
    ungrounded = _ungrounded(argument_text, 0)
    for attempt in range(1, DEBATE_MAX_REGENERATIONS + 1):
        if not ungrounded:
            break
        try:
            argument_text = await _write(
                base_user_content + "\n\n"
                "Your draft has these specific, confirmed problems: " +
                _ungrounded_issue(ungrounded) +
                " Write your argument again, fixing all of these.")
        except LLMError as exc:
            logger.warning(
                f"{side}.regeneration_failed_kept_draft", ticker=state.ticker,
                round_number=round_number, attempt=attempt,
                error_type=type(exc).__name__, error=str(exc)[:300],
                ungrounded_figures=sorted(ungrounded))
            break
        logger.info(
            f"{side}.combined_regeneration_done", ticker=state.ticker,
            round_number=round_number, attempt=attempt,
            max_regenerations=DEBATE_MAX_REGENERATIONS)
        ungrounded = _ungrounded(argument_text, attempt)
        if ungrounded and attempt == DEBATE_MAX_REGENERATIONS:
            logger.warning(
                f"{side}.final_draft_still_flagged", ticker=state.ticker,
                round_number=round_number, regenerations=attempt,
                ungrounded_figures=sorted(ungrounded))

    citations = extract_citations(
        argument_text, valid_roles=set(state.agent_outputs.keys()))
    return DebateArgument(
        round_number=round_number, side=side, argument=argument_text,
        citations=citations)


PERIOD_LABEL_RULE = (
    "Every figure you cite must carry the period the evidence gives it "
    "(a quarter such as Q2 2026, a fiscal year such as FY2025, TTM, six or "
    "nine months, or a date). Never place an annual or TTM figure next to a "
    "quarterly one as if both covered the same period, and never present an "
    "annual or older growth rate as current momentum when the evidence shows "
    "a different latest-quarter rate. If the evidence says a comparison base "
    "was unusually low or high (a strike, an unusual charge or gain, a "
    "divestiture), say so next to the figure.")
YEAR_TO_DATE_AND_DRIVERS_RULE = (
    "When the latest-quarter results in the evidence also give a "
    "year-to-date comparison, state the year-to-date change next to the "
    "latest-quarter change for the same metric (for example: down in the "
    "quarter but up over the six months), and do not let one stand for the "
    "other. Attribute a change to a driver (a segment's results, a price, "
    "volume or cost effect, an unusual item) only as the filing passages or "
    "records in the evidence state it; when the evidence states no cause, "
    "do not supply one.")
PROFIT_TREND_RULE = (
    "Do not present an older-period figure as the current state when the "
    "newest quarter in the evidence shows something different. When you "
    "cite the newest quarter's revenue, operating income or net income, give "
    "its year-over-year change as the latest-quarter results computed from "
    "SEC XBRL data in the evidence state it (and the unusual items the "
    "evidence names inside that figure, if any). A figure that the evidence "
    "shows only as a knowledge-graph fact "
    "marked [agent-derived, not independently sourced] is AlphaMind's own "
    "earlier output, not a fact: do not cite it unless a fresh tool result "
    "in this run states it. " + YEAR_TO_DATE_AND_DRIVERS_RULE)

UNUSUAL_ITEMS_RULE = (
    "The evidence may list unusual items from the filing records (gains, "
    "charges or tax effects the company reported; they can recur). The "
    "latest-quarter line also lists SEC XBRL items below operating income "
    "for the quarter, the year-ago quarter and the year to date "
    "(non-operating "
    "totals, equity-investment gains, impairments, restructuring, income "
    "tax); "
    "name the material ones like unusual items, with their period. When you "
    "cite one, say which reported line it sits in -- revenue, operating "
    "income (an operating cost or gain), non-operating income, or income tax "
    "-- as its quote or the XBRL line states it; when neither states the "
    "line, do not guess it. Do not cite the growth of a line (revenue, "
    "operating "
    "income or net income) as underlying growth without naming the unusual "
    "items the evidence places inside that line. "
    "When you cite a year-over-year swing or a large change, also name the "
    "unusual items in the comparison period a year earlier as the evidence "
    "(record quotes, filing passages) shows them -- a charge in the "
    "year-ago period makes the change look larger, a gain there makes it "
    "look smaller; when the evidence shows none, do not assume any.")
OVERLAP_RULE = (
    "Unusual items and XBRL items can overlap: one charge or gain can carry "
    "several concepts (for example an impairment also tagged as "
    "restructuring, or an upward adjustment that is part of the equity "
    "securities gains), and the same event can appear both in the XBRL lines "
    "and in a filing record. Count each event once: never add overlapping "
    "items together; cite a single item or the total the evidence gives. A "
    "year-to-date (six- or nine-month) amount already contains the quarters "
    "inside it: cite either that amount or its quarterly parts, never both "
    "side by side.")
FREE_CASH_FLOW_RULE = (
    "When you use cash flow as an argument and the cash-flow line gives free "
    "cash flow (operating cash flow minus capital expenditures), cite that "
    "figure with its period. If it is negative, say so, do not call capital "
    "spending covered or supported by operating cash flow, and say how the "
    "gap was funded. Operating cash flow, not free cash flow, is what covers "
    "capital spending: when free cash flow is positive, 'operating cash "
    "flow covered capital spending' is correct and must not be flagged; "
    "only 'free cash flow covered (or funded) capital spending' is wrong, "
    "because free cash flow is already after capital spending. When free "
    "cash flow is positive, do not present debt issued as funding capital "
    "spending, operations or a gap. If free "
    "cash flow after buybacks and/or dividends (or free cash flow, when no "
    "return line is shown) is positive, there is no funding gap: a fall in "
    "cash then reflects other uses the line does not show, so do not "
    "describe any funding as not shown. If free cash flow after buybacks "
    "and/or dividends is "
    "negative, say so, never say cash flow funds or supports those "
    "returns, and say how the gap was funded. That line covers only the "
    "returns its label names: if the evidence shows another return the "
    "line leaves out, do not call the line the result after all returns. "
    "Name a funding source only with a figure the evidence gives for it "
    "(for example debt issued or a fall in cash, with its amount and "
    "period); a source covers only its own amount -- if it is smaller than "
    "the gap, say the rest of the funding is not shown. Debt repaid is a "
    "use of cash, not a source. If the evidence gives no such figure, say "
    "the funding is not shown -- never guess it.")
LEGAL_AMOUNT_RULE = (
    "A legal matter's 'amount stated' in the evidence is the amount claimed, "
    "sought, assessed or awarded in that matter -- not a loss the company "
    "has recorded, and not the amount the company will pay. An amount the "
    "filing says was accrued, paid or escrowed is already in the reported "
    "results: do not present it as a new or future charge.")
SOURCE_WORDING_RULE = (
    "Refer to sources in plain words a reader understands -- for example "
    "'the latest 10-Q', 'the FY2025 10-K', 'the quarterly results' or "
    "'recent news'. Never write the internal record ids the evidence uses "
    "(codes such as F1.O2 or F2.L7), the evidence's internal line labels "
    "(such as 'system', 'computed in code', 'SEC XBRL line' or "
    "'latest-quarter line') or tool names.")
CODE_BUILT_LINES_ARE_EVIDENCE = (
    "Lines rendered by code from SEC XBRL financial data or from the filing "
    "records (records carry an id such as F1.O1 and were checked in code "
    "against a quote from the filing's own text) are evidence, as reliable "
    "as the filing itself: never treat them as unverified, as metadata, or "
    "as weaker than an agent's summary.")


def latest_quarter(state: AnalysisState) -> dict[str, Any] | None:
    out = state.agent_outputs.get(AgentRole.FUNDAMENTAL)
    if out is None or out.status == AgentStatus.FAILED:
        return None
    raw = out.raw_output if isinstance(out.raw_output, dict) else {}
    payload = raw.get("latest_quarter")
    if not isinstance(payload, dict):
        return None
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return payload


def latest_quarter_line(state: AnalysisState) -> str:
    payload = latest_quarter(state)
    return (
        " ".join(str(payload["text"]).split()) if payload is not None else "")


def one_off_items(state: AnalysisState) -> list[str]:
    items: list[str] = []
    for role in (AgentRole.FILING, AgentRole.FUNDAMENTAL):
        out = state.agent_outputs.get(role)
        raw = (
            out.raw_output
            if out is not None and isinstance(out.raw_output, dict) else {})
        values = raw.get("one_off_items")
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            text = " ".join(value.split())
            if len(text) > _FILING_LIST_ITEM_CHARS:
                logger.info(
                    "debate.one_off_item_cut", role=role.value,
                    length=len(text), kept=_FILING_LIST_ITEM_CHARS)
                text = text[:_FILING_LIST_ITEM_CHARS]
            if text and text not in items:
                items.append(text)
    return items


def _code_built_evidence_lines(state: AnalysisState) -> list[str]:
    lines: list[str] = []
    quarter = latest_quarter_line(state)
    if quarter:
        lines.append(
            "fundamental (computed in code from the company's SEC XBRL "
            f"financial data): {quarter}")
    items = one_off_items(state)
    if len(items) > _FILING_LIST_MAX_ITEMS:
        logger.info(
            "debate.one_off_items_dropped", shown=_FILING_LIST_MAX_ITEMS,
            dropped=len(items) - _FILING_LIST_MAX_ITEMS)
    if items:
        lines.append(
            "filing (unusual items from the filing records, each checked in "
            "code against a quote from the filing; recorded in results and "
            "they may recur): " + "; ".join(items[:_FILING_LIST_MAX_ITEMS]) +
            ".")
    records = key_records(state)
    if records:
        lines.append(
            "filing (key records from the newest filing, checked against "
            "quotes): " + "; ".join(records))
    lines.extend(filing_period_lines(state))
    return lines


YEAR_TO_DATE_UNUSUAL_KEY = "year_to_date_unusual_items"
PRIOR_YEAR_UNUSUAL_KEY = "prior_year_unusual_items"
_FILING_PERIOD_TITLES: tuple[tuple[str, str], ...] = ((
    YEAR_TO_DATE_UNUSUAL_KEY,
    "Unusual items in earlier quarters of this year-to-date window (they "
    "are inside the year-to-date figures)"), (
        PRIOR_YEAR_UNUSUAL_KEY,
        "Unusual items in the year-ago period (inside the year-ago figures "
        "the latest quarter is compared with)"))
_FILING_LIST_MAX_ITEMS = 6
_FILING_LIST_ITEM_CHARS = 500


def filing_raw_list(state: AnalysisState, key: str) -> list[str]:
    out = state.agent_outputs.get(AgentRole.FILING)
    if out is None or out.status == AgentStatus.FAILED:
        return []
    raw = out.raw_output if isinstance(out.raw_output, dict) else {}
    values = raw.get(key)
    if not isinstance(values, list):
        return []
    items: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = " ".join(value.split())[:_FILING_LIST_ITEM_CHARS]
        if text and text not in items:
            items.append(text)
    return items[:_FILING_LIST_MAX_ITEMS]


def filing_period_texts(state: AnalysisState) -> list[str]:
    texts: list[str] = []
    for key, title in _FILING_PERIOD_TITLES:
        items = filing_raw_list(state, key)
        if items:
            texts.append(f"{title}: " + "; ".join(items))
    return texts


def filing_period_titles(state: AnalysisState) -> list[str]:
    return [
        title for key, title in _FILING_PERIOD_TITLES
        if filing_raw_list(state, key)
    ]


def filing_period_lines(state: AnalysisState) -> list[str]:
    return [
        "filing (from the filing records, checked in code against quotes) -- "
        + text for text in filing_period_texts(state)
    ]


def key_records(state: AnalysisState) -> list[str]:
    out = state.agent_outputs.get(AgentRole.FILING)
    if out is None or out.status == AgentStatus.FAILED:
        return []
    raw = out.raw_output if isinstance(out.raw_output, dict) else {}
    values = raw.get("key_records")
    if not isinstance(values, list):
        return []
    records: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = " ".join(value.split())
        if text and text not in records:
            records.append(text)
    return records


_TAG_RULE = (
    "The bracket tag names only research agent roles (market, technical, "
    "fundamental, news, sentiment, filing, macro, risk, portfolio): a line "
    "computed from SEC XBRL data is [fundamental], a filing-record line is "
    "[filing]; never put 'system', a record id or a data-source name in it.")

_LENGTH_RULE = (
    "Respond with only your argument, 3-6 sentences (the statements the "
    "rules above require for what you cite count towards them), no preamble.")


def _side_prompt(
        name: str, stance: str, inflated: str, modest_case: str,
        example: str) -> str:
    return (
        "You are the " + name + " side of an investment debate in "
        "AlphaMind's multi-agent research system. Build the strongest honest "
        "case " + stance + " buying/holding the ticker, using ONLY facts "
        "already reported by the research agents below.\n\n"
        "Hard rule, not a suggestion: every specific number, date, deal name, "
        "analyst rating, or price target you state must be traceable to an "
        "actual sentence in the evidence below. If you cannot point to where "
        "a claim came from, do not write it -- not even if it sounds "
        "plausible or is something you'd expect to be true for a company "
        "like this. Do not fill gaps with your own general knowledge, round "
        "numbers up to sound more " + inflated + ", or state a "
        "consensus/average that isn't explicitly given. A short argument "
        "built only from real evidence is correct output; a longer one "
        "padded with invented specifics is not, regardless of how it reads. "
        "If the evidence is genuinely " + modest_case + " is what's wanted, "
        "not a stronger-sounding one built by adding detail the evidence "
        "doesn't support.\n\n"
        "Tag every specific factual claim inline with which agent's evidence "
        "it came from, right after the claim, in square brackets -- e.g. " +
        example +
        ". This isn't decoration: it forces you to actually locate a real "
        "source for each claim as you write it, rather than writing freely "
        "and checking later. If you can't identify which agent's evidence a "
        "claim would cite, that's the signal it doesn't belong in the "
        "argument at all. " + _TAG_RULE + "\n\n"
        "When net income is above operating income, or the evidence names an "
        "unusual or non-operating item (a tax benefit, unrealized investment "
        "gains, a gain on a sale), do not present net income as evidence of "
        "operating profitability: argue from operating income, and name the "
        "unusual item if you mention net income at all. " +
        UNUSUAL_ITEMS_RULE + " " + OVERLAP_RULE + "\n\n" +
        FREE_CASH_FLOW_RULE + "\n\n" + LEGAL_AMOUNT_RULE + "\n\n" +
        PERIOD_LABEL_RULE + " " + PROFIT_TREND_RULE + "\n\n" +
        SOURCE_WORDING_RULE + "\n\n"
        "If this round's context includes a list of claims Reflection "
        "already flagged as unsupported in your own prior argument, treating "
        "that list correctly is the single highest priority in this round -- "
        "higher than making a compelling case. " + _LENGTH_RULE)


_BULL_SYSTEM_PROMPT = _side_prompt(
    "Bull", "FOR", "impressive",
    "weak, a modest bull case citing that weakness honestly",
    '"revenue grew 16% YoY [fundamental]"')
_BEAR_SYSTEM_PROMPT = _side_prompt(
    "Bear", "AGAINST", "alarming",
    "strong for the bull case, a modest bear case that acknowledges that "
    "honestly", '"free cash flow fell 40% [risk]"')
_SYSTEM_PROMPTS: dict[DebateSide, str] = {
    "bull": _BULL_SYSTEM_PROMPT,
    "bear": _BEAR_SYSTEM_PROMPT
}


class DebateSideAgent:

    def __init__(
            self, side: DebateSide, llm_router: LLMRouter | None = None,
            llm_tier: LLMTier = LLMTier.HIGHEST_ACCURACY) -> None:
        self.side: DebateSide = side
        self.llm_router = llm_router or LLMRouter()
        self.llm_tier = llm_tier

    async def make_argument(self, state: AnalysisState) -> DebateArgument:
        return await generate_debate_argument(
            side=self.side, state=state, llm_router=self.llm_router,
            llm_tier=self.llm_tier, system_prompt=_SYSTEM_PROMPTS[self.side])
