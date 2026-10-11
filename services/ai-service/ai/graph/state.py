from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Literal
from ai.tools.base import is_evidence_record
from pydantic import BaseModel, ConfigDict, Field, model_validator
from shared.enums import (AgentRole, AgentStatus, AnalysisFailureReason,
                          AnalysisType, RecommendationAction)


def merge_agent_outputs(
    existing: "dict[AgentRole, AgentOutput] | None",
    new: "dict[AgentRole, AgentOutput] | None"
) -> "dict[AgentRole, AgentOutput]":
    merged = dict(existing or {})
    merged.update(new or {})
    return merged


def replace_reflection_feedback(
        existing: dict[Any, Any] | None,
        new: dict[Any, Any] | None) -> dict[Any, Any]:
    return dict(new if new is not None else (existing or {}))


def append_list(existing: list[Any] | None,
                new: list[Any] | None) -> list[Any]:
    return [*(existing or []), *(new or [])]


def merge_debate_state(
        existing: "DebateState | None",
        new: "DebateState | None") -> "DebateState":
    existing = existing or DebateState()
    if new is None:
        return existing

    judge_contributed = bool(new.verdicts)
    reflection_contributed = new.correction_used_for_round is not None
    return DebateState(
        round_number=(
            new.round_number if judge_contributed else existing.round_number),
        mutual_rounds_completed=(
            new.mutual_rounds_completed
            if judge_contributed else existing.mutual_rounds_completed),
        arguments=[*existing.arguments, *new.arguments
                   ], verdicts=[*existing.verdicts, *new.verdicts],
        converged=new.converged if judge_contributed else existing.converged,
        correction_used_for_round=(
            new.correction_used_for_round if reflection_contributed else
            existing.correction_used_for_round), superseded_through_round=max(
                existing.superseded_through_round,
                new.superseded_through_round))


FailureKind = Literal["rate_limit", "server_overload", "non_english", "other"]


class AgentOutput(BaseModel):
    model_config = ConfigDict(frozen=False)

    role: AgentRole
    status: AgentStatus = AgentStatus.COMPLETED
    summary: str
    score: float = Field(
        ge=0.0, le=1.0, description="Evidence-quality score in [0, 1].")
    is_well_supported: bool = True
    failure_kind: FailureKind | None = None
    citations: list[str] = Field(default_factory=list)
    raw_output: dict[str, Any] = Field(default_factory=dict)
    iterations_used: int = 0
    hit_iteration_cap: bool = False
    retry_of_feedback: str | None = Field(
        default=None,
        description="If this output is a retry (Reflection sent this "
        "role back), the feedback it was given -- kept on the output "
        "itself so the final recommendation can show *why* a second "
        "pass happened, not just that one did.")
    started_at: datetime | None = Field(
        default=None,
        description="Real wall-clock time `agent.run(state)` was "
        "invoked, set by `ai/graph/nodes.py::_run_agent_safely` -- not "
        "set by the agent itself.")
    completed_at: datetime | None = Field(
        default=None,
        description="Real wall-clock time `agent.run(state)` returned "
        "(or raised), set by `ai/graph/nodes.py::_run_agent_safely`.")


def lacks_successful_tool_evidence(out: AgentOutput) -> bool:
    if out.status != AgentStatus.COMPLETED or out.role is AgentRole.SENTIMENT:
        return False
    if not isinstance(out.raw_output, dict):
        return False
    scratchpad = out.raw_output.get("scratchpad")
    if not isinstance(scratchpad, list):
        return False
    return not any(
        isinstance(e, dict) and is_evidence_record(e) for e in scratchpad)


def is_usable_evidence(out: AgentOutput) -> bool:
    return out.is_well_supported and not lacks_successful_tool_evidence(out)


class AgentSkip(BaseModel):
    role: AgentRole
    reason: str


class DebateArgument(BaseModel):
    round_number: int
    side: Literal["bull", "bear"]
    argument: str
    citations: list[str] = Field(default_factory=list)
    available: bool = True
    unavailable_kind: FailureKind | None = None


def _is_superseded(
        argument: DebateArgument, superseded_through_round: int) -> bool:
    return argument.round_number <= superseded_through_round


ISSUE_ITERATION_CAP = "hit its iteration cap"
ISSUE_NOT_WELL_SUPPORTED = "marked not well supported by its own checks"
ISSUE_AGENT_FAILED = "agent failed this run"
ISSUE_NOT_RETRIED = "failed, not retried"
ISSUE_ZERO_EVIDENCE = "zero successful tool results"
ISSUE_RETRY_SKIPPED_CAPACITY = "retry skipped this round (provider capacity)"
ISSUE_RETRY_DELAYED_CAPACITY = (
    "retry delayed before dispatch (provider capacity)")
ISSUE_DEBATE_SIDE_NOT_REGENERATED = "debate side unavailable, not regenerated"
ISSUE_NO_EVIDENCE_NOT_RETRIED = "found no evidence, not retried"
ISSUE_LLM_REVIEW = "llm_review"
ISSUE_RETRY_NO_PROGRESS = "retry_no_progress"
ISSUE_REFLECTION_CRASHED = "reflection_crashed"
ISSUE_UNCLASSIFIED = "unclassified"

IssueKind = Literal["unsupported", "wrong_period", "stale", "not_engaged",
                    "inconsistent"]
ISSUE_KINDS: tuple[IssueKind, ...] = (
    "unsupported", "wrong_period", "stale", "not_engaged", "inconsistent")


class ReflectionIssue(BaseModel):
    code: str
    text: str
    role: str | None = None
    kind: IssueKind | None = None
    figure: str | None = None
    claimed_period: str | None = None
    evidence_quote: str | None = None
    failure_kind: FailureKind | None = None


FAILURE_KIND_TEXT: dict[str, str] = {
    "server_overload": "LLM provider overloaded (503)",
    "rate_limit": "LLM rate limit / quota exhausted (429)",
    "non_english": "model output rejected as non-English",
    "other": "error unrelated to provider capacity"
}

_KIND_CAUSES: dict[str, str] = {
    "unsupported": (
        "figures or claims that no agent's actual evidence supports"),
    "wrong_period": (
        "figures given for the wrong period (the figures themselves were in "
        "the evidence)"),
    "stale": (
        "an older figure or event presented as the current state while the "
        "newest period in the evidence shows something different"),
    "not_engaged": (
        "a debate side not engaging the other side's strongest point "
        "(argument quality, not a figure problem)"),
    "inconsistent": (
        "an agent's summary contradicting itself or its own cited numbers")
}


def _classify_reflection_causes(
        issues: Sequence[ReflectionIssue],
        verdict: "ReflectionVerdict | None" = None) -> list[str]:
    codes = [i.code for i in issues]
    causes: list[str] = []

    kinds = sorted({
        i.failure_kind
        for i in issues
        if i.code == ISSUE_NOT_RETRIED and i.failure_kind is not None
    })
    if kinds:
        causes.append(
            "agents that failed from " +
            " / ".join(FAILURE_KIND_TEXT[k] for k in kinds) +
            " and were deliberately not retried (not a content problem)")
    code_causes: tuple[tuple[tuple[str, ...], str], ...] = (
        ((ISSUE_NO_EVIDENCE_NOT_RETRIED, ),
         "an agent whose live search honestly found no relevant evidence "
         "this run (not retried: a retry cannot create evidence)"),
        ((ISSUE_AGENT_FAILED, ), "an agent failing outright this run"),
        ((ISSUE_ITERATION_CAP, ),
         "an agent running out of turns before reaching a final answer "
         "(not "
         "necessarily a sign it fabricated anything)"),
        ((ISSUE_ZERO_EVIDENCE, ),
         "an agent finishing with zero successful tool results, i.e. no "
         "evidence of its own this run"),
        ((ISSUE_NOT_WELL_SUPPORTED, ),
         "an agent's own mechanical checks (evidence anchor, self-reported "
         "figures) leaving its output not well supported"),
        ((ISSUE_RETRY_SKIPPED_CAPACITY, ISSUE_RETRY_DELAYED_CAPACITY),
         "a retry being skipped or delayed this round because the LLM "
         "provider was out of capacity (429 quota / 503 overload), not "
         "because of a content problem"),
        ((ISSUE_DEBATE_SIDE_NOT_REGENERATED, ),
         "a debate side that was unavailable from provider capacity "
         "(429/503) and deliberately not regenerated"),
        ((ISSUE_RETRY_NO_PROGRESS, ),
         "a retry not repeated because the same feedback had already "
         "produced no change"), ((ISSUE_REFLECTION_CRASHED, ),
                                 "the Reflection check itself failing"))
    for wanted, cause in code_causes:
        if any(code in wanted for code in codes):
            causes.append(cause)
    llm_issues = [i for i in issues if i.code == ISSUE_LLM_REVIEW]
    for kind in ISSUE_KINDS:
        if any(i.kind == kind for i in llm_issues):
            causes.append(_KIND_CAUSES[kind])
    if any(i.kind is None for i in llm_issues):
        causes.append("other problems the LLM citation review raised")
    if (verdict is not None and verdict.agent_count
            and not verdict.usable_agent_count):
        causes.append("no research agent producing usable evidence this run")
    if verdict is not None and not verdict.llm_review_ran:
        reason = (
            f" ({verdict.llm_review_skip_reason})"
            if verdict.llm_review_skip_reason else "")
        causes.append(
            f"the LLM citation review not running this pass{reason}, so "
            "nothing was verified against the evidence")
    review_ran = verdict is None or verdict.llm_review_ran
    unclassified = any(code == ISSUE_UNCLASSIFIED for code in codes)
    if review_ran and (unclassified or not causes):
        causes.append(
            "figures Bull/Bear cited that no agent's actual evidence supports")
    return causes


class JudgeVerdict(BaseModel):
    round_number: int
    converged: bool
    leaning: RecommendationAction
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    judge_failed: bool = False
    failure_reason: AnalysisFailureReason | None = None
    retry_round: bool = False


class DebateState(BaseModel):
    round_number: int = 0
    mutual_rounds_completed: int = 0
    correction_used_for_round: int | None = None
    arguments: list[DebateArgument] = Field(default_factory=list)
    verdicts: list[JudgeVerdict] = Field(default_factory=list)
    converged: bool = False
    superseded_through_round: int = 0

    @property
    def latest_verdict(self) -> JudgeVerdict | None:
        return self.verdicts[-1] if self.verdicts else None

    def latest_argument(
            self, side: Literal["bull", "bear"]) -> DebateArgument | None:
        return next((a for a in reversed(self.arguments) if a.side == side),
                    None)

    def unavailable_latest_sides(self) -> list[Literal["bull", "bear"]]:
        missing: list[Literal["bull", "bear"]] = []
        sides: tuple[Literal["bull", "bear"], ...] = ("bull", "bear")
        for side in sides:
            latest = self.latest_argument(side)
            if latest is not None and not latest.available:
                missing.append(side)
        return missing

    def transcript_arguments(self) -> list[DebateArgument]:
        kept: list[DebateArgument] = []
        for index, argument in enumerate(self.arguments):
            replaced = not argument.available and any(
                later.available and later.side == argument.side
                and later.round_number == argument.round_number
                for later in self.arguments[index + 1:])
            if not replaced:
                kept.append(argument)
        return kept

    def is_argument_superseded(self, argument: DebateArgument) -> bool:
        return _is_superseded(argument, self.superseded_through_round)

    def current_arguments(self) -> list[DebateArgument]:
        return [
            a for a in self.arguments if not self.is_argument_superseded(a)
        ]

    def previous_round_argument(
            self, side: Literal["bull", "bear"]) -> DebateArgument | None:
        return next((
            a for a in reversed(self.current_arguments())
            if a.side == side and a.round_number <= self.round_number), None)


class ReflectionVerdict(BaseModel):
    round_number: int
    verified: bool
    issues: list[str] = Field(default_factory=list)
    retry_agents: dict[AgentRole, str] = Field(
        default_factory=dict,
        description="role -> concrete feedback for that role's retry "
        "pass. The feedback is added to that one retry's context; it "
        "never changes the role's system prompt.")
    llm_review_ran: bool = False
    llm_review_skip_reason: str | None = None
    usable_agent_count: int = 0
    agent_count: int = 0
    issue_details: list[ReflectionIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sync_issue_details(self) -> "ReflectionVerdict":
        if not self.issue_details and self.issues:
            self.issue_details = [
                ReflectionIssue(code=ISSUE_UNCLASSIFIED, text=text)
                for text in self.issues
            ]
        return self

    def with_extra_issues(
            self, extra: Sequence[ReflectionIssue]) -> "ReflectionVerdict":
        return self.model_copy(
            update={
                "issues": [*self.issues, *(i.text for i in extra)],
                "issue_details": [*self.issue_details, *extra]
            })

    def review_meta_line(self) -> str:
        line = (
            f"{REVIEW_META_PREFIX} "
            f"llm_review_ran={str(self.llm_review_ran).lower()}; "
            f"usable_agents={self.usable_agent_count}/{self.agent_count}")
        if self.llm_review_skip_reason:
            line += (
                f"; llm_review_skipped_because={self.llm_review_skip_reason}")
        return line


REVIEW_META_PREFIX = "REVIEW_META:"


class EpisodicMemoryHit(BaseModel):
    analysis_id: str
    ticker: str
    analyzed_at: datetime
    recommendation: RecommendationAction
    confidence: float
    outcome: str | None = None
    outcome_return: float | None = None
    judge_leaning: RecommendationAction | None = None
    judge_confidence: float | None = None

    def track_record_line(self, window_days: int) -> str:
        line = (
            f"- {self.analyzed_at.date().isoformat()}: "
            f"{self.recommendation.value.upper()} "
            f"(confidence {self.confidence:.2f})")
        if self.outcome is None:
            return (
                line + f" -> outcome not known yet (under {window_days} days)")
        if self.outcome_return is not None:
            line += (
                f" -> {window_days}-day return "
                f"{self.outcome_return * 100:+.1f}%")
        return line + f", call was {self.outcome}"


class PriorRecommendation(BaseModel):
    recommendation_id: str
    analysis_id: str
    action: RecommendationAction
    confidence: float
    issued_at: datetime
    accepted: bool
    well_supported_count: int | None = None
    agent_count: int | None = None
    judge_leaning: RecommendationAction | None = None
    judge_confidence: float | None = None

    def judge_context_line(self, ticker: str) -> str:
        accepted_note = (
            "already accepted" if self.accepted else "not yet accepted")
        return (
            f"A prior {self.action.value.upper()} recommendation for {ticker} "
            f"in "
            f"this portfolio is still open ({accepted_note}; issued "
            f"{self.issued_at.date().isoformat()}, confidence "
            f"{self.confidence:.2f}). Its reasoning is deliberately not "
            f"shown: "
            "judge this run's evidence on its own merits.")

    def provenance_line(self) -> str:
        return (
            f"The Judge was shown the prior open {self.action.value.upper()} "
            f"recommendation of {self.issued_at.date().isoformat()} "
            f"(confidence "
            f"{self.confidence:.2f}, analysis {self.analysis_id}) -- its "
            f"action, "
            "confidence and date only, not its reasoning.")


LOW_PARTICIPATION_RATIO_THRESHOLD = 0.5
PARTIAL_PARTICIPATION_RATIO_THRESHOLD = 0.8


class AnalysisState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    analysis_id: str
    user_id: str
    portfolio_id: str | None = None
    ticker: str
    analysis_type: AnalysisType
    requested_agents: list[AgentRole] | None = None
    holdings_count: int = 0
    pending_recommendation_context: str | None = None
    prior_recommendation: PriorRecommendation | None = None
    held_quantity: float | None = None
    episodic_context: list[EpisodicMemoryHit] = Field(default_factory=list)
    position_weight_pct: float | None = None

    selected_agents: list[AgentRole] = Field(default_factory=list)
    skipped_agents: list[AgentSkip] = Field(default_factory=list)

    procedural_hints: dict[AgentRole, str] = Field(default_factory=dict)

    fresh_filing_context: str | None = Field(default=None)

    kg_context: str | None = Field(default=None)

    portfolio_kg_context: str | None = Field(default=None)

    agent_outputs: Annotated[dict[AgentRole, AgentOutput],
                             merge_agent_outputs] = Field(
                                 default_factory=dict)
    reflection_feedback: Annotated[dict[
        AgentRole, str], replace_reflection_feedback] = Field(
            default_factory=dict,
            description="Written by reflection_node every "
            "round with ONLY the roles it retries this round (the reducer "
            "replaces, not merges); read by the retried agent (and by "
            "_run_agent_safely's failure path) as retry_of_feedback.")

    debate: Annotated[DebateState,
                      merge_debate_state] = Field(default_factory=DebateState)

    reflection_history: Annotated[list[ReflectionVerdict],
                                  append_list] = (Field(default_factory=list))
    reflection_retry_count: int = 0
    judge_retry_attempts: int = 0
    judge_retry_seconds: float = 0.0
    debate_side_repair_used: bool = False
    debate_late_side_repair_used: bool = False
    pending_retry_agents: list[AgentRole] = Field(
        default_factory=list,
        description="The roles reflection_node dispatched "
        "for a retry this round. Written by reflection_node (set) and "
        "judge_node (cleared after the retried round is judged); no "
        "reducer, so the two must never write in the same superstep. "
        "Read by route_after_reflection (the Sends; Sentiment waits for "
        "News when both are pending) and should_continue_to_debate "
        "(News/Sentiment retry ordering), "
        "_retry_wave_brought_nothing, debate_join_node and "
        "_is_single_side_correction_pass.")

    final_recommendation: dict[str, Any] | None = Field(
        default=None,
        description="Written by recommendation_node: the JSON dump of "
        "the recommendation that `ai/recommendation/engine.py` builds "
        "from the latest Judge verdict, or None when the run ends "
        "without a verdict (see no_verdict_reason).")
    no_verdict_reason: AnalysisFailureReason | None = None

    def usable_agent_count(self) -> int:
        return sum(
            1 for out in self.agent_outputs.values()
            if is_usable_evidence(out))

    def below_debate_evidence_minimum(self, minimum: int) -> bool:
        if minimum <= 0 or not self.agent_outputs:
            return False
        required = min(
            minimum,
            len(self.selected_agents) or len(self.agent_outputs))
        return self.usable_agent_count() < required

    def well_supported_ratio(self) -> float | None:
        if not self.agent_outputs:
            return None
        return self.usable_agent_count() / len(self.agent_outputs)

    def confidence_adjustment_note(self) -> str | None:
        notes: list[str] = []

        if self.skipped_agents:
            names = ", ".join(
                f"{s.role.value} ({s.reason})" for s in self.skipped_agents)
            notes.append(f"Not run: {names}.")

        retried_thin: list[str] = []
        failed_not_retried: list[str] = []
        thin_not_retried: list[str] = []
        for out in self.agent_outputs.values():
            if is_usable_evidence(out):
                continue
            if out.retry_of_feedback is not None:
                retried_thin.append(out.role.value)
            elif out.status == AgentStatus.FAILED:
                kind = out.failure_kind or "other"
                failed_not_retried.append(f"{out.role.value} ({kind})")
            else:
                thin_not_retried.append(out.role.value)
        if retried_thin:
            notes.append(
                f"Limited evidence despite retry attempts: "
                f"{', '.join(retried_thin)}.")
        if failed_not_retried:
            notes.append(
                f"Failed, not retried: {', '.join(failed_not_retried)}.")
        if thin_not_retried:
            notes.append(
                f"Limited evidence, not retried: "
                f"{', '.join(thin_not_retried)}.")
        if self.prior_recommendation is not None:
            notes.append(self.prior_recommendation.provenance_line())

        ratio = self.well_supported_ratio()
        if ratio is not None and ratio < LOW_PARTICIPATION_RATIO_THRESHOLD:
            notes.append(
                f"Confidence capped: only {self.usable_agent_count()} of "
                f"{len(self.agent_outputs)} selected agents produced usable "
                "evidence this run -- treat this recommendation as "
                "provisional, not a full-evidence verdict.")

        warned = sorted(
            out.role.value for out in self.agent_outputs.values()
            if isinstance(out.raw_output, dict)
            and out.raw_output.get("verification_warnings"))
        if warned:
            notes.append(
                f"Unresolved verification warning(s) from: "
                f"{', '.join(warned)} -- "
                "at least one of their own claims could not be confirmed "
                "against "
                "real evidence this run; treat any recommendation reasoning "
                "that "
                "leans on those specific agents' claims with extra caution.")

        if not self.debate.converged:
            notes.append(
                "Bull/Bear debate did not converge within the round cap; "
                "confidence reflects that.")

        if (self.reflection_history
                and not self.reflection_history[-1].verified):
            last = self.reflection_history[-1]
            last_issues = list(last.issue_details)
            issue_count = len(last_issues)
            causes = _classify_reflection_causes(last_issues, last)
            when = (
                "even after retries" if self.reflection_retry_count > 0
                or len(self.reflection_history) > 1 else
                "in its only review pass")
            if issue_count:
                notes.append(
                    f"Reflection could not fully verify the debate content "
                    f"{when} "
                    f"({issue_count} unresolved issue"
                    f"{'s' if issue_count != 1 else ''} in the final review "
                    f"pass, e.g. {'; '.join(causes)}); confidence reflects "
                    f"that.")
            else:
                notes.append(
                    f"Reflection could not fully verify the debate content "
                    f"{when}: "
                    f"{'; '.join(causes)}; confidence reflects that.")

        elif self.reflection_history and any(
                not v.verified for v in self.reflection_history[:-1]):
            earlier_issues = [
                issue for v in self.reflection_history[:-1] if not v.verified
                for issue in v.issue_details
            ]
            causes = _classify_reflection_causes(earlier_issues)
            prefix = (
                f"Note: an earlier review pass this run flagged "
                f"{len(earlier_issues)} issue"
                f"{'s' if len(earlier_issues) != 1 else ''} (e.g. "
                f"{'; '.join(causes)}); ")
            last = self.reflection_history[-1]
            if ratio == 1.0 and last.llm_review_ran:
                notes.append(
                    prefix + "the system retried and the final evidence has "
                    "since been verified. Confidence is not reduced for this.")
            else:
                notes.append(
                    prefix + "the final review pass raised no further retry "
                    "candidates, but not every agent's evidence was verified "
                    "(see the evidence notes above).")

        return " ".join(notes) if notes else None
