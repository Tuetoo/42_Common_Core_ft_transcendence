from datetime import datetime
from typing import NamedTuple
from ai.graph.state import (FAILURE_KIND_TEXT,
                            LOW_PARTICIPATION_RATIO_THRESHOLD,
                            PARTIAL_PARTICIPATION_RATIO_THRESHOLD,
                            AnalysisState, JudgeVerdict)
from config import settings
from pydantic import BaseModel
from shared.enums import (AgentStatus, AnalysisFailureReason,
                          RecommendationAction)

_UNCONVERGED_DEBATE_PENALTY = 0.15

_LOW_PARTICIPATION_CONFIDENCE_CAP = 0.3
_PARTIAL_PARTICIPATION_PENALTY = 0.15

_ONE_SIDE_UNAVAILABLE_CONFIDENCE_CAP = 0.3


class PreviousVerdict(NamedTuple):
    issued_at: datetime
    action: RecommendationAction
    confidence: float
    judge_own: bool
    published_confidence: float


def _candidate(
        issued_at: datetime, final_action: RecommendationAction,
        final_confidence: float, judge_leaning: RecommendationAction | None,
        judge_confidence: float | None) -> PreviousVerdict:
    if judge_leaning is not None and judge_confidence is not None:
        return PreviousVerdict(
            issued_at, judge_leaning, judge_confidence, True,
            final_confidence)
    return PreviousVerdict(
        issued_at, final_action, final_confidence, False, final_confidence)


def previous_verdict(state: AnalysisState) -> PreviousVerdict | None:
    candidates: list[PreviousVerdict] = []
    prior = state.prior_recommendation
    if prior is not None:
        candidates.append(
            _candidate(
                prior.issued_at, prior.action, prior.confidence,
                prior.judge_leaning, prior.judge_confidence))
    if state.episodic_context:
        hit = max(state.episodic_context, key=lambda h: h.analyzed_at)
        candidates.append(
            _candidate(
                hit.analyzed_at, hit.recommendation, hit.confidence,
                hit.judge_leaning, hit.judge_confidence))
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda c: c.issued_at)
    except TypeError:
        return candidates[0]


def at_or_above_position_limit(weight_pct: float | None) -> bool:
    return (
        weight_pct is not None
        and weight_pct >= settings.MAX_POSITION_WEIGHT_PCT)


def position_limit_line(state: AnalysisState) -> str:
    weight = state.position_weight_pct
    if weight is None:
        return ""
    limit = settings.MAX_POSITION_WEIGHT_PCT
    line = (
        f"\n\nCurrent weight of {state.ticker} in this portfolio: "
        f"{weight:.1f}% "
        f"of net value. Single-position limit: {limit:.0f}%.")
    if at_or_above_position_limit(weight):
        line += (
            f" {state.ticker} is at or above the limit: do not recommend BUY "
            "(no further buying). HOLD or SELL remain allowed on their merits."
        )
    return line


def position_limit_note(ticker: str, weight_pct: float) -> str:
    return (
        f"BUY changed to HOLD: {ticker} is already {weight_pct:.1f}% of the "
        f"portfolio's net value, at or above the "
        f"{settings.MAX_POSITION_WEIGHT_PCT:.0f}% single-position limit -- no "
        "further buying.")


class FinalRecommendation(BaseModel):
    ticker: str
    action: RecommendationAction
    confidence: float
    reasoning: str
    confidence_adjustment_note: str | None
    debate_converged: bool
    judge_leaning: RecommendationAction | None = None
    judge_confidence: float | None = None


def _llm_credit_exhausted(state: AnalysisState) -> bool:
    verdict = state.debate.latest_verdict
    return (
        verdict is not None and verdict.failure_reason
        == AnalysisFailureReason.LLM_CREDIT_EXHAUSTED) or any(
            out.failure_kind == "credit_exhausted"
            for out in state.agent_outputs.values()) or any(
                arg.unavailable_kind == "credit_exhausted"
                for arg in state.debate.arguments)


def no_verdict_reason(state: AnalysisState) -> AnalysisFailureReason | None:
    reason = _no_verdict_reason(state)
    if reason is not None and _llm_credit_exhausted(state):
        return AnalysisFailureReason.LLM_CREDIT_EXHAUSTED
    return reason


def _no_verdict_reason(state: AnalysisState) -> AnalysisFailureReason | None:
    verdict = state.debate.latest_verdict
    if verdict is None:
        if not state.agent_outputs or all(
                out.status == AgentStatus.FAILED
                for out in state.agent_outputs.values()):
            return AnalysisFailureReason.NO_EVIDENCE
        if state.below_debate_evidence_minimum(
                settings.MIN_USABLE_AGENTS_FOR_DEBATE):
            return AnalysisFailureReason.INSUFFICIENT_EVIDENCE
        return AnalysisFailureReason.JUDGE_UNAVAILABLE
    if verdict.judge_failed and state.debate.latest_valid_verdict is None:
        return (
            verdict.failure_reason or AnalysisFailureReason.JUDGE_UNAVAILABLE)
    return None


def _earlier_verdict_sentence(
        state: AnalysisState, verdict: JudgeVerdict) -> str:
    latest = state.debate.latest_verdict
    if latest is None or not latest.judge_failed or verdict.judge_failed:
        return ""
    reason = latest.failure_reason or AnalysisFailureReason.JUDGE_UNAVAILABLE
    return (
        f"The last Judge pass failed ({reason.value.replace('_', ' ')}), so "
        "this recommendation keeps the Judge's earlier valid verdict from "
        f"round {verdict.round_number}.")


def _lowered_sentence(before: float, after: float, reason: str) -> str:
    return f"Confidence lowered by {before - after:.2f} because {reason}."


def _participation_reason(state: AnalysisState) -> str:
    return (
        f"only {state.usable_agent_count()} of {len(state.agent_outputs)} "
        "selected agents produced usable evidence")


def _stored_confidence_sentence(
        state: AnalysisState, verdict: JudgeVerdict, confidence: float) -> str:
    parts: list[str] = []
    if confidence != round(verdict.confidence, 2):
        parts.append(
            f"Stored confidence is {confidence:.2f}, against the Judge's "
            f"{verdict.confidence:.2f}, because of the code adjustments "
            "listed in this note.")
    previous = previous_verdict(state)
    if (previous is not None and previous.judge_own
            and round(previous.confidence, 2)
            != round(previous.published_confidence, 2)):
        parts.append(
            f"The Judge compared its confidence with its own earlier "
            f"{previous.confidence:.2f} from "
            f"{previous.issued_at.date().isoformat()}, before code "
            "adjustments; the recommendation published then had "
            f"{previous.published_confidence:.2f}, and this one is stored at "
            f"{confidence:.2f}.")
    return " ".join(parts)


def _backup_model_sentence(
        state: AnalysisState, verdict: JudgeVerdict) -> str:
    calls = (
        sum(a.fallback_calls for a in state.debate.arguments)
        + sum(v.fallback_calls for v in state.debate.verdicts)
        + sum(r.fallback_calls for r in state.reflection_history))
    if calls == 0:
        return ""
    judge = (
        f" The final Judge verdict was written by {verdict.model}."
        if verdict.model else "")
    return (
        f"Backup model: {calls} Bull, Bear, Judge or reflection call(s) in "
        "this analysis were answered by the provider's fallback model, not "
        f"its primary model.{judge}")


def build_final_recommendation(
        state: AnalysisState, verdict: JudgeVerdict) -> FinalRecommendation:
    action = verdict.leaning
    reasoning = verdict.rationale

    confidence = verdict.confidence
    cap_notes: list[str] = []

    def lower(after: float, reason: str) -> None:
        nonlocal confidence
        if after < confidence:
            cap_notes.append(_lowered_sentence(confidence, after, reason))
        confidence = after

    if not state.debate.converged:
        lower(
            max(0.0, confidence - _UNCONVERGED_DEBATE_PENALTY),
            "the Bull/Bear debate did not converge")

    well_supported_ratio = state.well_supported_ratio()
    if well_supported_ratio is not None:
        if well_supported_ratio < LOW_PARTICIPATION_RATIO_THRESHOLD:
            lower(
                min(confidence, _LOW_PARTICIPATION_CONFIDENCE_CAP),
                _participation_reason(state))
        elif well_supported_ratio < PARTIAL_PARTICIPATION_RATIO_THRESHOLD:
            lower(
                max(0.0, confidence - _PARTIAL_PARTICIPATION_PENALTY),
                _participation_reason(state))

    if action == RecommendationAction.BUY and at_or_above_position_limit(
            state.position_weight_pct):
        assert state.position_weight_pct is not None
        action = RecommendationAction.HOLD
        cap_notes.append(
            position_limit_note(state.ticker, state.position_weight_pct))

    missing_sides = state.debate.unavailable_latest_sides()
    if len(missing_sides) == 1:
        missing = missing_sides[0]
        latest = state.debate.latest_argument(missing)
        kind = (
            latest.unavailable_kind if latest is not None else None) or "other"
        why = FAILURE_KIND_TEXT[kind]
        capped = min(confidence, _ONE_SIDE_UNAVAILABLE_CONFIDENCE_CAP)
        lowered_by = confidence - capped
        confidence = capped
        cap_notes.append(
            f"Confidence capped at "
            f"{_ONE_SIDE_UNAVAILABLE_CONFIDENCE_CAP:.2f}: the "
            f"{missing.capitalize()} side's argument was unavailable in the "
            f"final "
            f"debate round ({why}), so this verdict weighs only one side of "
            "the debate" + (
                f" (lowered by {lowered_by:.2f})." if lowered_by > 0 else "."))

    prior = state.prior_recommendation
    if (prior is not None and prior.action == action
            and prior.well_supported_count is not None):
        this_count = state.usable_agent_count()
        fewer_usable = this_count < prior.well_supported_count
        if fewer_usable and confidence > prior.confidence:
            lowered_by = confidence - prior.confidence
            confidence = prior.confidence
            cap_notes.append(
                f"Confidence capped at the prior recommendation's "
                f"{prior.confidence:.2f} (lowered by {lowered_by:.2f}): this "
                f"run reaffirms the prior "
                f"{prior.action.value.upper()} with fewer agents producing "
                f"usable evidence ({this_count}) than the prior analysis had "
                f"({prior.well_supported_count}).")

    base_note = state.confidence_adjustment_note()
    confidence = round(confidence, 2)
    note_parts = [
        p for p in (
            base_note, _earlier_verdict_sentence(state, verdict), *cap_notes,
            _stored_confidence_sentence(state, verdict, confidence),
            _backup_model_sentence(state, verdict)) if p
    ]
    return FinalRecommendation(
        ticker=state.ticker, action=action, confidence=confidence,
        reasoning=reasoning, confidence_adjustment_note=(
            " ".join(note_parts) if note_parts else None),
        debate_converged=state.debate.converged and not missing_sides,
        judge_leaning=verdict.leaning, judge_confidence=verdict.confidence)
