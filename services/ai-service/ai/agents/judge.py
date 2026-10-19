import math
import re
from typing import Any
import structlog
from ai.agents.debate_common import (FREE_CASH_FLOW_RULE, LEGAL_AMOUNT_RULE,
                                     OVERLAP_RULE, PERIOD_LABEL_RULE,
                                     PROFIT_TREND_RULE, SOURCE_WORDING_RULE,
                                     UNUSUAL_ITEMS_RULE, filing_period_titles,
                                     find_ungrounded_figures, format_evidence,
                                     format_grounding_reference,
                                     latest_quarter)
from ai.agents.evidence_grounding import non_english_reason
from ai.graph.state import (LOW_PARTICIPATION_RATIO_THRESHOLD, AgentOutput,
                            AnalysisState, DebateArgument, JudgeVerdict)
from ai.llm.base import (AGENT_LLM_CALL_RETRIES, JsonObjectParse, LLMMessage,
                         LLMResponse, LLMTier, log_unparseable,
                         parse_json_object)
from ai.llm.router import (LLMRouter, fallback_call_count,
                           recording_served_models)
from ai.recommendation.engine import position_limit_line, previous_verdict
from ai.tools.base import is_evidence_record
from config import settings
from shared.enums import (AgentRole, AnalysisFailureReason,
                          RecommendationAction)

logger = structlog.get_logger(__name__)

_BASE_SYSTEM_PROMPT = (
    "You are the Judge in an investment debate in AlphaMind's multi-agent "
    "research system. Weigh the Bull and Bear arguments against the "
    "underlying research evidence -- not against how persuasively either "
    "side is written. Decide whether another debate round would plausibly "
    "change your assessment (`converged: false`) or whether you already "
    "have a stable view (`converged: true`).\n\n"
    "A trailing P/E is computed from net income. An unusual or non-operating "
    "GAIN booked in net income (an investment revaluation, an unrealized "
    "gain, a tax benefit recognized in the period) makes net income larger "
    "and the trailing P/E LOWER than core earnings justify -- never higher. "
    "A tax reserve, an accrual or a liability (for example 'unrecognized tax "
    "benefits') is not a gain and does not lower the trailing P/E. When the "
    "user message includes a valuation note on net vs. operating margin, do "
    "not cite the trailing P/E as a sign the stock is cheap -- weigh the "
    "forward P/E instead and say why. "
    "Use this unusual-gain argument only when the user message includes that "
    "valuation note, and only in that direction; otherwise do not mention "
    "it.\n\n"
    "Cash and BUY budget figures in the evidence (available cash, cash "
    "committed to other tickers' open BUY tiers, a remaining buy_budget or "
    "budget_shortfall) only limit HOW MUCH can be bought; the size is set "
    "later in code. They are not a reason for buy, hold or sell and do not "
    "change your confidence: decide leaning and confidence on the investment "
    "case alone, and do not cite cash or budget limits in the rationale. The "
    "single-position limit stated in the user message is the only portfolio "
    "rule that may change the action.\n\n"
    "When you cite a figure that covers a period (a quarter, six months, a "
    "fiscal year), name the period. Do not call a result ongoing, "
    "continuing or persistent unless the latest period in the evidence shows "
    "it too: a six-month operating loss that includes a profitable latest "
    "quarter is not an ongoing loss. " + PERIOD_LABEL_RULE +
    " A qualitative growth claim (for example "
    "double-digit growth) must match the latest period in the evidence or "
    "name the older period it describes. " + PROFIT_TREND_RULE +
    " When the user message lists a latest-quarter "
    "change to state, the rationale must state it and name any unusual "
    "item the evidence gives as its driver; when it also lists year-to-date "
    "figures, the rationale must state the year-to-date change of the same "
    "metric too. " + UNUSUAL_ITEMS_RULE + " " + OVERLAP_RULE + " "
    "When the rationale states a year-to-date or year-over-year change and "
    "the user message lists unusual items in the year-to-date window or in "
    "the year-ago period, it must say that the change includes them and "
    "give their amounts; do not compute an adjusted figure unless both "
    "numbers are in the evidence.\n\n" + LEGAL_AMOUNT_RULE + "\n\n" +
    FREE_CASH_FLOW_RULE + "\n\n"
    "When the evidence gives company guidance for a coming period, compare "
    "it with the latest actual quarter in the evidence for the same metric "
    "(its level and its year-over-year growth) before calling the guidance "
    "strong or weak, and say what the comparison shows.\n\n"
    "Name the single factor that decides your verdict in the rationale. "
    "When the user message gives an earlier verdict on this ticker and your "
    "leaning differs from its action, fill change_since_previous with what "
    "changed and say it in the rationale, and set change_basis: "
    "new_evidence when the change rests on a fact that is new since the "
    "earlier verdict (name that fact), or reweighting when the evidence is "
    "unchanged and you weigh it differently (say which point you weigh "
    "differently and why). Do not present a re-weighting of unchanged "
    "evidence as new evidence. When the earlier verdict is your own earlier "
    "leaning and confidence, a confidence move of 0.05 or more also needs "
    "change_since_previous and change_basis. When it is only the final "
    "action and confidence after code adjustments, that confidence is not "
    "your own earlier confidence, so do not anchor to it and do not explain "
    "a difference from it.\n\n"
    "Before deciding, take the strongest points of the side your verdict "
    "goes against (at least the two strongest) and, in opposing_points, "
    "say for each why it does not change the verdict or how it lowers your "
    "confidence.\n\n" + SOURCE_WORDING_RULE + "\n\n"
    "The rationale is shown to the user as the investment reasoning. Give "
    "investment reasons only: never mention the debate, debate rounds, "
    "iterations or whether your view is stable -- that belongs only in the "
    "`converged` field.\n\n")

_PRIOR_RECOMMENDATION_CLAUSE = (
    "A prior open recommendation for this ticker is included below in the "
    "user message (its action, confidence and date only -- its reasoning "
    "is deliberately withheld). Judge genuinely whether your current "
    "verdict supersedes it (real new evidence or a materially changed "
    "situation) or whether it should stand as-is -- do not automatically "
    "treat 'newer' as 'better', and do not treat the prior's existence as "
    "evidence for it. Your rationale should say explicitly if and why "
    "you're superseding it.\n\n")

_LOW_PARTICIPATION_CLAUSE = (
    "Most of the research agents selected for this ticker failed or "
    "produced unusable evidence this round -- you are judging on a "
    "materially incomplete picture, not the usual full evidence set. "
    "Do not treat a prior open recommendation (if included below) as "
    "still supported by fresh evidence just because little contradicts "
    "it -- 'little evidence to disagree with' is not the same as "
    "'evidence confirms it.' Your rationale must acknowledge the "
    "evidence gap explicitly rather than restating an earlier "
    "verdict's reasoning as if it were newly confirmed, and your "
    "confidence should reflect how little was actually verified this "
    "round.\n\n")

_TRACK_RECORD_CLAUSE = (
    "The user message also lists AlphaMind's earlier calls on this ticker "
    "and, where the review window has passed, what the price actually did "
    "afterwards. Use it only to calibrate: if earlier calls were wrong, ask "
    "what today's evidence might be missing and be more careful with your "
    "confidence; if they were right, that is not evidence that the same "
    "call is right today. Never repeat an earlier call because it was made "
    "before -- base the verdict on today's evidence and debate.\n\n")

_RESPONSE_FORMAT_INSTRUCTION = (
    "Respond with exactly one JSON object and nothing else:\n"
    '{"converged": true|false, "leaning": "buy"|"hold"|"sell", '
    '"confidence": 0.0-1.0, "rationale": "2-8 sentences: first every '
    "statement the rules above and the user message require (each may be "
    "one sentence), then your main reasons; use the extra sentences only "
    'for required statements", '
    '"deciding_factor": "the single factor that decides the verdict, in a '
    'few words", '
    '"change_since_previous": "what changed since the earlier verdict -- '
    "required when your leaning differs from its action, or when the user "
    'message says your confidence move needs it, else an empty string", '
    '"change_basis": "new_evidence" | "reweighting" | "" (empty only when '
    "change_since_previous is empty), "
    '"opposing_points": [{"point": "one of the strongest points of the side '
    'your verdict goes against, in one sentence", "response": "why it does '
    'not change the verdict, or how it lowers your confidence"}] (at least '
    "the two strongest points)}")


def _build_system_prompt(
        has_pending_recommendation: bool, low_participation: bool = False,
        has_track_record: bool = False) -> str:
    prompt = _BASE_SYSTEM_PROMPT
    if has_pending_recommendation:
        prompt += _PRIOR_RECOMMENDATION_CLAUSE
    if low_participation:
        prompt += _LOW_PARTICIPATION_CLAUSE
    if has_track_record:
        prompt += _TRACK_RECORD_CLAUSE
    return prompt + _RESPONSE_FORMAT_INSTRUCTION


def trailing_pe_line(state: AnalysisState) -> str:
    fundamental = state.agent_outputs.get(AgentRole.FUNDAMENTAL)
    if fundamental is None or not isinstance(fundamental.raw_output, dict):
        return ""
    facts = fundamental.raw_output.get("key_facts")
    if not isinstance(facts, dict):
        return ""
    net, operating = facts.get("net_margin_pct"), facts.get(
        "operating_margin_pct")
    if not isinstance(net, int | float) or not isinstance(operating,
                                                          int | float):
        return ""
    if net <= 0 or net <= operating:
        return ""
    pes = _trailing_and_forward_pe(fundamental)
    if pes is not None:
        trailing, forward = pes
        if forward < trailing * _ONE_OFF_MATERIAL_PE_RATIO:
            logger.info(
                "judge.trailing_pe_note_skipped", trailing_pe=trailing,
                forward_pe=forward,
                reason="trailing P/E not materially below forward P/E")
            return ""
    return (
        f"\n\nValuation note (computed from this run's fundamentals): net "
        f"margin {net:.1f}% is above operating margin {operating:.1f}% -- net "
        "income includes non-operating or unusual gains, so the trailing P/E "
        "is LOWER than core earnings justify. Do not cite the trailing P/E as "
        "a sign the stock is cheap; weigh the forward P/E.")


_ONE_OFF_MATERIAL_PE_RATIO = 1.10
_TRAILING_PE_RE = re.compile(r"(?<!forward )\bP/E=(\d+(?:\.\d+)?)")
_FORWARD_PE_RE = re.compile(r"\bforward P/E=(\d+(?:\.\d+)?)")


def _trailing_and_forward_pe(
        fundamental: AgentOutput) -> tuple[float, float] | None:
    raw = (
        fundamental.raw_output
        if isinstance(fundamental.raw_output, dict) else {})
    for entry in reversed(raw.get("scratchpad") or []):
        if (not isinstance(entry, dict)
                or entry.get("action") != "get_fundamentals"
                or not is_evidence_record(entry)):
            continue
        observation = str(entry.get("observation") or "")
        trailing = _TRAILING_PE_RE.search(observation)
        forward = _FORWARD_PE_RE.search(observation)
        if trailing and forward:
            values = float(trailing.group(1)), float(forward.group(1))
            if values[0] > 0 and values[1] > 0:
                return values
    return None


def previous_verdict_line(
        state: AnalysisState) -> tuple[str, RecommendationAction | None]:
    previous = previous_verdict(state)
    if previous is None:
        return "", None
    issued_at, action, confidence, judge_own, _ = previous
    day = issued_at.date().isoformat()
    if judge_own:
        return (
            f"\n\nMost recent earlier verdict on {state.ticker}: your own "
            f"final-round leaning {action.value.upper()} with confidence "
            f"{confidence:.2f} (before code adjustments) on {day}. If your "
            "leaning differs from it, or your confidence differs from "
            f"{confidence:.2f} by {CONFIDENCE_CHANGE_TO_EXPLAIN:.2f} or more, "
            "the rationale must say what changed since then, and "
            "change_since_previous / change_basis must say whether it rests "
            "on new evidence or on a re-weighting of unchanged evidence; "
            "name the single deciding factor either way.", action)
    return (
        f"\n\nMost recent earlier verdict on {state.ticker}: "
        f"{action.value.upper()} (final action and confidence "
        f"{confidence:.2f} after code adjustments -- penalties, caps and the "
        "position limit; the Judge's own earlier leaning and confidence are "
        f"not stored) on {day}. If your leaning "
        "differs from that action, the rationale must say what changed since "
        "then, and change_since_previous / change_basis must say whether it "
        "rests on new evidence or on a re-weighting of unchanged evidence; "
        "name the single deciding factor either way. Do not explain a "
        "confidence difference from this final value.", action)


CONFIDENCE_CHANGE_TO_EXPLAIN = 0.05
_CHANGE_BASES = frozenset({"new_evidence", "reweighting"})

_QUARTER_METRICS: tuple[tuple[str, str],
                        ...] = (("revenue", "revenue"),
                                ("operating_income", "operating income"),
                                ("net_income", "net income"))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def latest_quarter_changes_to_state(state: AnalysisState) -> list[str]:
    payload = latest_quarter(state)
    if payload is None:
        return []
    threshold = settings.JUDGE_STATE_QUARTER_DECLINE_PCT
    changes: list[str] = []
    for key, label in _QUARTER_METRICS:
        pct = _number(payload.get(f"{key}_yoy_pct"))
        current = _number(payload.get(key))
        prior = _number(payload.get(f"{key}_prior"))
        if pct is not None:
            if pct > -threshold:
                continue
            if (current is not None and prior is not None and current < 0
                    and prior < 0):
                changes.append(
                    f"{label}: loss widened {abs(pct):.0f}% year over year")
            else:
                changes.append(f"{label} {pct:+.0f}% year over year")
        elif current is not None and prior is not None and current < 0 < prior:
            changes.append(f"{label} swung to a loss")
    return changes


def _has_year_to_date(state: AnalysisState) -> bool:
    payload = latest_quarter(state)
    ytd = payload.get("year_to_date") if payload is not None else None
    return isinstance(ytd, dict) and any(
        _number(ytd.get(key)) is not None for key, _ in _QUARTER_METRICS)


def profit_trend_line(state: AnalysisState) -> str:
    changes = latest_quarter_changes_to_state(state)
    has_ytd = _has_year_to_date(state)
    filing_titles = filing_period_titles(state)
    if not changes and not has_ytd and not filing_titles:
        return ""
    parts: list[str] = []
    if changes:
        parts.append(_quarter_decline_text(state, changes))
    if has_ytd:
        parts.append(
            "The latest-quarter line in the evidence ends with year-to-date "
            "figures (computed in code from SEC XBRL). When the rationale "
            "states the latest quarter's change for a metric, it must also "
            "state that metric's year-to-date change, and attribute either "
            "change to a driver (a segment's results, a stated cause, an "
            "unusual item) only as the filing passages or records in the "
            "evidence state it.")
    if filing_titles:
        parts.append(
            "The evidence lists, from the filing records: " +
            "; ".join(filing_titles) + ". When the rationale states a "
            "year-to-date or year-over-year change and unusual items are "
            "listed for that year-to-date window or that year-ago period, it "
            "must say the change includes them and give their amounts; do not "
            "compute an adjusted figure unless both numbers are in the "
            "evidence.")
    return "\n\n" + " ".join(parts)


def _quarter_decline_text(state: AnalysisState, changes: list[str]) -> str:
    period_end = latest_quarter(state) or {}
    ended = period_end.get("period_end")
    quarter = (
        f" (quarter ended {ended})"
        if isinstance(ended, str) and ended else "")
    newer = period_end.get("newer_filing")
    title = (
        f"Latest quarter in SEC XBRL{quarter} -- the newer {newer} is not in "
        "XBRL yet; take the newest quarter from that filing's passages"
        if isinstance(newer, str) and newer else f"Latest quarter{quarter}")
    return (
        f"{title}, computed in code from SEC XBRL: " + "; ".join(changes) +
        ". The rationale must state this change with the quarter and name "
        "the unusual items the evidence gives as its driver, if any; do not "
        "present an older-period figure as current.")


def _track_record_block(state: AnalysisState) -> str:
    if not state.episodic_context:
        return ""
    window = settings.AI_ACCURACY_EVALUATION_DAYS
    lines = "\n".join(
        hit.track_record_line(window) for hit in state.episodic_context)
    return (
        f"\n\nTrack record -- AlphaMind's earlier calls on {state.ticker}, "
        f"newest first (calibration only, not evidence about today):\n{lines}")


_RAW_TAIL_CHARS = 300
_ERROR_CONTEXT_BEFORE = 300
_ERROR_CONTEXT_AFTER = 200


def _parse_failure_fields(result: JsonObjectParse) -> dict[str, Any]:
    pos = result.error_pos
    fields: dict[str, Any] = {
        "parse_error": result.error,
        "error_pos": pos,
        "error_line": result.error_line,
        "error_col": result.error_col
    }
    if pos is not None:
        fields["error_context"] = result.text[
            max(0, pos - _ERROR_CONTEXT_BEFORE):pos + _ERROR_CONTEXT_AFTER]
    return fields


def _parse_response(
    raw: str, *, attempt: int = 1, response: LLMResponse | None = None
) -> tuple[dict[str, Any] | None, JsonObjectParse]:
    result = parse_json_object(raw)
    if result.data is None:
        log_unparseable(
            "judge.unparseable_response", raw, attempt=attempt,
            model=response.model if response is not None else None,
            finish_reason=(
                response.finish_reason if response is not None else None),
            **_parse_failure_fields(result))
        return None, result
    if result.leading_chars or result.trailing_text or result.lenient_strings:
        logger.warning(
            "judge.json_parsed_leniently", attempt=attempt,
            leading_chars_ignored=result.leading_chars,
            trailing_chars_ignored=len(result.trailing_text),
            trailing_text=result.trailing_text[:_RAW_TAIL_CHARS],
            control_characters_in_strings=result.lenient_strings,
            raw_chars=len(raw))
    return result.data, result


def _json_reask_request(raw: str, result: JsonObjectParse) -> str:
    where = (
        f" at line {result.error_line} column {result.error_col} "
        f"(character {result.error_pos} of {len(raw)})"
        if result.error_pos is not None else "")
    return (
        "Your previous reply could not be read as JSON: "
        f"{result.error or 'invalid JSON'}{where}. Return the same answer "
        "again as exactly one valid JSON object in the required format and "
        "nothing else: every key and string in double quotes, every double "
        'quote inside a string escaped as \\", no line breaks inside '
        "strings, and no text before or after the object.")


_JUDGE_MAX_TOKENS = 3072


class JudgeAgent:

    def __init__(
            self, llm_router: LLMRouter | None = None,
            llm_tier: LLMTier = LLMTier.HIGHEST_ACCURACY) -> None:
        self.llm_router = llm_router or LLMRouter()
        self.llm_tier = llm_tier

    async def _complete(self, messages: list[LLMMessage]) -> LLMResponse:
        return await self.llm_router.complete(
            self.llm_tier, messages, retries=AGENT_LLM_CALL_RETRIES,
            response_format="json_object", max_tokens=_JUDGE_MAX_TOKENS)

    async def _reask_for_json(
            self, messages: list[LLMMessage], response: LLMResponse,
            failure: JsonObjectParse
    ) -> tuple[dict[str, Any] | None, LLMResponse]:
        logger.warning(
            "judge.json_reask", parse_error=failure.error,
            error_pos=failure.error_pos, raw_chars=len(response.content),
            model=response.model)
        try:
            retry = await self._complete([
                *messages,
                LLMMessage(role="assistant", content=response.content),
                LLMMessage(
                    role="user",
                    content=_json_reask_request(response.content, failure))
            ])
        except Exception as exc:
            logger.warning("judge.json_reask_failed", error=str(exc)[:300])
            return None, response
        parsed, _ = _parse_response(retry.content, attempt=2, response=retry)
        if parsed is None:
            logger.warning(
                "judge.json_reask_failed", error="reply still unparseable")
            return None, response
        logger.info("judge.json_reask_recovered", model=retry.model)
        return parsed, retry

    async def _rewrite_ungrounded_figures(
            self, parsed: dict[str, Any], messages: list[LLMMessage],
            answer: LLMResponse,
            reference: str) -> tuple[dict[str, Any], LLMResponse]:
        ungrounded = sorted(
            find_ungrounded_figures(
                str(parsed.get("rationale", "")), reference))
        if not ungrounded:
            return parsed, answer
        logger.warning(
            "judge.ungrounded_figures", ungrounded_figures=ungrounded)
        request = (
            "These figures in your rationale have no matching number anywhere "
            f"in the evidence: {', '.join(ungrounded)}. Restate each one "
            "exactly as the evidence gives it (value, unit and period), or "
            "remove it. Do not add items together to make a new figure. "
            "Return the whole JSON object again in the same format.")
        try:
            response = await self._complete([
                *messages,
                LLMMessage(role="assistant", content=answer.content),
                LLMMessage(role="user", content=request)
            ])
        except Exception as exc:
            logger.warning(
                "judge.ungrounded_rewrite_failed", error=str(exc)[:300])
            return parsed, answer
        rewritten, _ = _parse_response(response.content, response=response)
        rationale = str(rewritten.get("rationale") or "") if rewritten else ""
        if (rewritten is None or not rationale.strip()
                or non_english_reason(rationale)):
            logger.warning(
                "judge.ungrounded_rewrite_failed",
                error="rewrite unparseable or without an English rationale")
            return parsed, answer
        still = sorted(find_ungrounded_figures(rationale, reference))
        if still:
            logger.warning(
                "judge.ungrounded_figures_after_rewrite",
                ungrounded_figures=still)
        return rewritten, response

    async def assess_round(self, state: AnalysisState) -> JudgeVerdict:
        with recording_served_models() as served:
            verdict = await self._assess_round(state)
        return verdict.model_copy(
            update={"fallback_calls": fallback_call_count(served)})

    async def _assess_round(self, state: AnalysisState) -> JudgeVerdict:
        round_number = state.debate.round_number + 1

        bull_arg = state.debate.latest_argument("bull")
        bear_arg = state.debate.latest_argument("bear")

        if len(state.debate.unavailable_latest_sides()) == 2:
            return JudgeVerdict(
                round_number=round_number, converged=False,
                leaning=RecommendationAction.HOLD, confidence=0.0, rationale=(
                    "Judge skipped: neither the Bull nor the Bear side "
                    "produced an argument this round -- no debate to assess."),
                judge_failed=True,
                failure_reason=AnalysisFailureReason.DEBATE_UNAVAILABLE)

        pending_context_line = (
            f"\n\nPrior open recommendation for this ticker (consider whether "
            f"your verdict genuinely supersedes it, or whether it should "
            f"stand): {state.pending_recommendation_context}"
            if state.pending_recommendation_context else "")
        holding_line = (
            f"\n\nCurrent holding of {state.ticker} in this portfolio: "
            f"{state.held_quantity:g} shares (informational only -- a SELL "
            "view is allowed whether or not the ticker is held)."
            if state.held_quantity is not None else "")
        holding_line += position_limit_line(state)
        holding_line += trailing_pe_line(state)
        holding_line += profit_trend_line(state)
        verdict_line, previous_action = previous_verdict_line(state)
        previous = previous_verdict(state)
        previous_confidence = (
            previous.confidence if previous is not None else None)
        previous_is_final = previous is None or not previous.judge_own
        holding_line += verdict_line
        track_record_line = _track_record_block(state)
        user_content = (
            f"Ticker: {state.ticker}\n"
            f"Round: {round_number}\n\n"
            f"Research agent evidence:\n{format_evidence(state)}\n\n"
            f"Bull argument: {_argument_text(bull_arg, 'Bull')}\n\n"
            f"Bear argument: {_argument_text(bear_arg, 'Bear')}"
            f"{pending_context_line}{holding_line}{track_record_line}")

        participation_ratio = state.well_supported_ratio()
        low_participation = (
            participation_ratio is not None
            and participation_ratio < LOW_PARTICIPATION_RATIO_THRESHOLD)
        system_prompt = _build_system_prompt(
            has_pending_recommendation=bool(
                state.pending_recommendation_context),
            low_participation=low_participation,
            has_track_record=bool(track_record_line))
        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=user_content)
        ]
        response = await self._complete(messages)
        parsed, failure = _parse_response(response.content, response=response)
        if parsed is None:
            parsed, response = await self._reask_for_json(
                messages, response, failure)
        if parsed is not None:
            parsed, response = await self._rewrite_ungrounded_figures(
                parsed, messages, response,
                format_grounding_reference(state) + pending_context_line +
                holding_line + track_record_line)

        if parsed is None:
            return JudgeVerdict(
                round_number=round_number, converged=False,
                leaning=RecommendationAction.HOLD, confidence=0.0, rationale=(
                    "Judge response could not be parsed; forcing another "
                    "round."), judge_failed=True,
                failure_reason=AnalysisFailureReason.JUDGE_UNAVAILABLE,
                retry_round=True)

        rationale = str(parsed.get("rationale", ""))
        language_issue = non_english_reason(rationale)
        if language_issue is not None:
            logger.warning(
                "judge.non_english_rationale", reason=language_issue)
            return JudgeVerdict(
                round_number=round_number, converged=False,
                leaning=RecommendationAction.HOLD, confidence=0.0,
                rationale="Judge output rejected: non-English text.",
                judge_failed=True,
                failure_reason=AnalysisFailureReason.JUDGE_UNAVAILABLE)

        _log_opposing_points(parsed)

        try:
            leaning = RecommendationAction(
                str(parsed.get("leaning", "hold")).lower())
        except ValueError:
            leaning = RecommendationAction.HOLD

        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = (
            max(0.0, min(1.0, confidence)) if math.isfinite(confidence) else
            0.0)
        _log_deciding_factor(
            parsed, leaning, previous_action, confidence=confidence,
            previous_confidence=previous_confidence,
            previous_is_final=previous_is_final)

        return JudgeVerdict(
            round_number=round_number,
            converged=_parse_converged(parsed.get("converged", False)),
            leaning=leaning, confidence=confidence, rationale=rationale,
            model=response.model)


_MIN_OPPOSING_POINTS = 2


def opposing_points(parsed: dict[str, Any]) -> list[dict[str, str]]:
    raw = parsed.get("opposing_points")
    if not isinstance(raw, list):
        return []
    points: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        point = " ".join(str(item.get("point") or "").split())
        response = " ".join(str(item.get("response") or "").split())
        if point and response:
            points.append({"point": point, "response": response})
    return points


def _log_opposing_points(parsed: dict[str, Any]) -> None:
    points = opposing_points(parsed)
    if len(points) < _MIN_OPPOSING_POINTS:
        raw = parsed.get("opposing_points")
        logger.warning(
            "judge.opposing_points_missing", count=len(points),
            raw_count=len(raw) if isinstance(raw, list) else None,
            minimum=_MIN_OPPOSING_POINTS)
    if points:
        logger.info(
            "judge.opposing_points", opposing_points=[{
                "point": p["point"],
                "response": p["response"]
            } for p in points[:4]])


def change_since_previous_required(
        leaning: RecommendationAction, confidence: float,
        previous_action: RecommendationAction | None,
        previous_confidence: float | None) -> tuple[bool, bool]:
    flipped = previous_action is not None and previous_action != leaning
    moved = previous_confidence is not None and (
        round(abs(confidence - previous_confidence), 4)
        >= CONFIDENCE_CHANGE_TO_EXPLAIN)
    return flipped, moved


def _log_deciding_factor(
        parsed: dict[str, Any], leaning: RecommendationAction,
        previous_action: RecommendationAction | None, *,
        confidence: float | None = None,
        previous_confidence: float | None = None,
        previous_is_final: bool = True) -> None:
    factor = str(parsed.get("deciding_factor") or "").strip()[:200]
    change = str(parsed.get("change_since_previous") or "").strip()[:300]
    basis = str(parsed.get("change_basis") or "").strip().lower()
    flipped, moved = (
        change_since_previous_required(
            leaning, confidence, previous_action, previous_confidence)
        if confidence is not None else
        (previous_action is not None and previous_action != leaning, False))
    previous_leaning = previous_action.value if previous_action else None
    logger.info(
        "judge.deciding_factor", deciding_factor=factor or None,
        leaning=leaning.value, previous_leaning=previous_leaning,
        confidence=confidence, previous_confidence=previous_confidence,
        flipped=flipped, confidence_moved=moved,
        previous_is_final=previous_is_final, change_since_previous=change
        or None, change_basis=basis or None)
    if not (flipped or (moved and not previous_is_final)):
        return
    if not change:
        logger.warning(
            "judge.change_since_previous_missing", leaning=leaning.value,
            previous_leaning=previous_leaning, confidence=confidence,
            previous_confidence=previous_confidence, flipped=flipped,
            confidence_moved=moved)
    if basis not in _CHANGE_BASES:
        logger.warning(
            "judge.change_basis_missing", change_basis=basis[:40] or None,
            flipped=flipped, confidence_moved=moved)


def _parse_converged(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _argument_text(argument: DebateArgument | None, side: str) -> str:
    if argument is None:
        return "(none)"
    if not argument.available:
        return (
            f"(UNAVAILABLE -- the {side} side failed to produce an argument "
            "this round. Do not infer what it would have argued; this verdict "
            "can only weigh the other side against the evidence.)")
    return argument.argument
