from ai.graph.state import (FAILURE_KIND_TEXT,
                            LOW_PARTICIPATION_RATIO_THRESHOLD,
                            PARTIAL_PARTICIPATION_RATIO_THRESHOLD,
                            AnalysisState, JudgeVerdict)
from config import settings
from pydantic import BaseModel
from shared.enums import (AgentStatus, AnalysisFailureReason,
                          RecommendationAction)

_UNCONVERGED_DEBATE_PENALTY = 0.15

_UNVERIFIED_REFLECTION_PENALTY = 0.15

_LOW_PARTICIPATION_CONFIDENCE_CAP = 0.3
_PARTIAL_PARTICIPATION_PENALTY = 0.15

_ONE_SIDE_UNAVAILABLE_CONFIDENCE_CAP = 0.3


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


def no_verdict_reason(state: AnalysisState) -> AnalysisFailureReason | None:
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
    if verdict.judge_failed:
        return (
            verdict.failure_reason or AnalysisFailureReason.JUDGE_UNAVAILABLE)
    return None


def build_final_recommendation(
        state: AnalysisState, verdict: JudgeVerdict) -> FinalRecommendation:
    action = verdict.leaning
    reasoning = verdict.rationale

    confidence = verdict.confidence
    if not state.debate.converged:
        confidence = max(0.0, confidence - _UNCONVERGED_DEBATE_PENALTY)
    if state.reflection_history and not state.reflection_history[-1].verified:
        confidence = max(0.0, confidence - _UNVERIFIED_REFLECTION_PENALTY)

    well_supported_ratio = state.well_supported_ratio()
    if well_supported_ratio is not None:
        if well_supported_ratio < LOW_PARTICIPATION_RATIO_THRESHOLD:
            confidence = min(confidence, _LOW_PARTICIPATION_CONFIDENCE_CAP)
        elif well_supported_ratio < PARTIAL_PARTICIPATION_RATIO_THRESHOLD:
            confidence = max(0.0, confidence - _PARTIAL_PARTICIPATION_PENALTY)

    cap_notes: list[str] = []

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
        confidence = min(confidence, _ONE_SIDE_UNAVAILABLE_CONFIDENCE_CAP)
        cap_notes.append(
            f"Confidence capped at "
            f"{_ONE_SIDE_UNAVAILABLE_CONFIDENCE_CAP:.2f}: the "
            f"{missing.capitalize()} side's argument was unavailable in the "
            f"final "
            f"debate round ({why}), so this verdict weighs only one side of "
            "the debate.")

    prior = state.prior_recommendation
    if (prior is not None and prior.action == action
            and prior.well_supported_count is not None):
        this_count = state.usable_agent_count()
        fewer_usable = this_count < prior.well_supported_count
        if fewer_usable and confidence > prior.confidence:
            confidence = prior.confidence
            cap_notes.append(
                f"Confidence capped at the prior recommendation's "
                f"{prior.confidence:.2f}: this run reaffirms the prior "
                f"{prior.action.value.upper()} with fewer agents producing "
                f"usable evidence ({this_count}) than the prior analysis had "
                f"({prior.well_supported_count}).")

    base_note = state.confidence_adjustment_note()
    note_parts = [p for p in (base_note, *cap_notes) if p]
    confidence = round(confidence, 2)
    return FinalRecommendation(
        ticker=state.ticker, action=action, confidence=confidence,
        reasoning=reasoning, confidence_adjustment_note=(
            " ".join(note_parts) if note_parts else None),
        debate_converged=state.debate.converged and not missing_sides,
        judge_leaning=verdict.leaning, judge_confidence=verdict.confidence)
