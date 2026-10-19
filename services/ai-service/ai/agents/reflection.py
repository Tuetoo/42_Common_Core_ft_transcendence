import asyncio
from collections.abc import Sequence
from typing import Any
import structlog
from ai.agents.debate_common import (_FIGURE_RE, CODE_BUILT_LINES_ARE_EVIDENCE,
                                     FREE_CASH_FLOW_RULE, OVERLAP_RULE,
                                     _quote_is_grounded,
                                     find_ungrounded_figures, format_evidence,
                                     format_grounding_reference)
from ai.graph.state import (AVAILABILITY_FAILURE_KINDS, FAILURE_KIND_TEXT,
                            ISSUE_AGENT_FAILED,
                            ISSUE_DEBATE_SIDE_NOT_REGENERATED,
                            ISSUE_ITERATION_CAP, ISSUE_KINDS, ISSUE_LLM_REVIEW,
                            ISSUE_NO_EVIDENCE_NOT_RETRIED, ISSUE_NOT_RETRIED,
                            ISSUE_NOT_WELL_SUPPORTED, ISSUE_ZERO_EVIDENCE,
                            AgentOutput, AnalysisState, DebateArgument,
                            DebateSide, FailureKind, IssueKind,
                            ReflectionIssue, ReflectionVerdict,
                            is_usable_evidence,
                            lacks_successful_tool_evidence)
from ai.llm.base import (AGENT_LLM_CALL_RETRIES, LLMError, LLMMessage,
                         LLMRateLimitError, LLMRequestTimeoutError,
                         LLMServerOverloadedError, LLMTier, log_unparseable,
                         parse_json_dict)
from ai.llm.router import (LLMRouter, capacity_aware_delay,
                           fallback_call_count, recording_served_models)
from config import settings
from shared.enums import AgentRole, AgentStatus

logger = structlog.get_logger(__name__)

COMPUTED_VALUES_ARE_EXACT = (
    "Values that the SEC XBRL lines label as computed (a label such as "
    "'free cash flow (computed: operating cash flow minus capital "
    "expenditures)'), values marked * (derived as the difference of two "
    "year-to-date facts), and the percentage changes and margins those "
    "lines give were computed in code from the unrounded filing values: "
    "they are exact. Redoing that arithmetic from the rounded figures "
    "shown can differ by a rounding step; that difference is not an "
    "issue, so never flag such a value, or a text that repeats it, as "
    "inconsistent or unsupported because of it.")

_SYSTEM_PROMPT = (
    "You are the Reflection step in AlphaMind's multi-agent research "
    "system, reviewing a completed debate round. You check FOUR things "
    "in this one pass, not just citation integrity:\n"
    "1. Citation integrity: do the Bull/Bear arguments "
    "and agent summaries make claims not actually grounded in the "
    "evidence provided -- fabricated figures, overstated certainty, or "
    "events that were not reported by any agent.\n"
    "2. Argument quality: did the side "
    "that seems to be winning actually engage with the OTHER side's "
    "strongest point, or did it just restate its own case while ignoring "
    "the best counterargument? Does the confidence either side projects "
    "(how certain their language sounds) actually match how strong their "
    "cited evidence really is -- e.g. a side sounding very certain while "
    "leaning on a single weak or heavily-caveated data point is a real "
    "issue worth flagging, not a style preference.\n"
    "3. Internal consistency: does an agent's summary "
    "contradict itself, or state a conclusion its own cited numbers don't "
    "actually support (independent of whether that number matches some "
    "OTHER agent's evidence, which is a separate mechanical check that "
    "already runs elsewhere)?\n"
    "4. Period and label consistency: compare the period of "
    "every figure in the Bull/Bear arguments and agent summaries with the "
    "period the agent evidence gives it (quarter such as Q2 2026, fiscal "
    "year such as FY2025, TTM, six/nine months, a date). Flag a figure "
    "stated for the wrong period, an annual/TTM figure placed beside "
    "quarterly figures as if it were quarterly, and an older figure or "
    "growth rate presented as the current state while the evidence (for "
    "example the code-computed latest-quarter line) shows a newer period "
    "with something different, and a growth rate on a base the evidence "
    "calls unusual (a strike, an unusual item) without saying so.\n\n" +
    CODE_BUILT_LINES_ARE_EVIDENCE + " " + COMPUTED_VALUES_ARE_EXACT + "\n\n"
    "The Bull/Bear arguments and the agent summaries must follow these two "
    "rules; a figure or claim that breaks one is an unsupported issue: " +
    OVERLAP_RULE + " " + FREE_CASH_FLOW_RULE + "\n\n"
    'A bare "looks fine" without showing the '
    "work lets real, self-consistent-looking-but-wrong content "
    "(a faithfully-reported but internally-impossible number, an "
    "argument that never engages the strongest counterpoint) pass "
    "through unexamined as long as no OTHER agent's evidence happens to "
    'contradict it. You must also list, in "checked_claims", each '
    "specific claim (a number, or a load-bearing qualitative point) you "
    "actually compared against its source -- 2 to 5 entries is normal "
    "for a typical round, listing every single claim is not required, "
    "but listing ZERO is only acceptable if the evidence/debate provided "
    "is itself genuinely too thin to contain any checkable claim (e.g. "
    'every agent failed). This exists so a concluding "no problem" '
    "reflects real, specific claims you actually looked at, not a "
    "default pass.\n\n"
    "Respond with exactly one JSON object and nothing else:\n"
    '{"checked_claims": ["short description of each specific claim you '
    "actually verified against its source, even when it checked out "
    'fine"], "issues": [{"role": "the role the problem belongs to", '
    '"kind": "unsupported" | "wrong_period" | "stale" | "not_engaged" | '
    '"inconsistent", "figure": "the figure exactly as written, or null", '
    '"claimed_period": "the period the text gives that figure, or null", '
    '"evidence_quote": "exact substring copied from the text below", '
    '"description": "short description of the problem"}]} -- "issues" may '
    'be empty (a genuinely clean round), but "checked_claims" should not '
    "be empty unless there was truly nothing checkable.\n"
    "kind: unsupported = a claim (figure, deal, event, rating) the agent "
    "evidence does not support (check 1); wrong_period = a figure that is "
    "in the evidence but given for a different period (check 4); stale = "
    "an older figure or event presented as current while the evidence "
    "shows a newer period (check 4); not_engaged = a side ignoring the "
    "other side's strongest point or sounding more certain than its "
    "evidence (check 2); inconsistent = an agent's summary contradicting "
    "itself or its own cited numbers (check 3).\n"
    "role -- the issue is sent back to this role, so pick the one that "
    "actually matches the problem:\n"
    "- If a DATA agent's own evidence or summary looks thin, wrong or "
    'inconsistent, name that agent\'s role (e.g. "news", "fundamental").\n'
    "- If the evidence itself looks fine but Bull or Bear invented a "
    "number, deal, or event, gave a figure the wrong period, or argued "
    'poorly (check 2), name "bull" or "bear" -- retrying a data agent '
    "won't fix a problem in how a side argued from good evidence.\n"
    "- Use null only for a general observation that belongs to no role.\n"
    "Only name roles that actually appear in the evidence or debate "
    "provided; do not invent role names.\n"
    "evidence_quote: copy it character-for-character. For unsupported, "
    "wrong_period, stale and inconsistent issues, quote the Agent evidence "
    "(for unsupported, the evidence sentence closest to the claim); for "
    "not_engaged, quote the other side's point from the debate. An issue "
    "whose quote is not found in that text is discarded.\n\n"
    "The full debate transcript below includes "
    "EVERY round so far, not just the newest one -- each side's rounds are "
    "marked, with the most recent round for each side labeled "
    '"(LATEST)". A claim that appeared in an EARLIER round for a side '
    "but is NOT repeated in that side's LATEST round has already been "
    "corrected -- do not flag it as a current issue just because it's "
    "still visible earlier in the transcript. Only flag a fabrication if "
    "it's present in that side's LATEST round, or if it's a genuinely NEW "
    "problem introduced in the latest round. Re-flagging an already-"
    "dropped old claim wastes a retry on a problem that no longer exists.\n\n"
    "Noting that a "
    'claim "is not in the latest round" and THEN still including it in '
    '"issues" anyway is the exact mistake this rule '
    "exists to prevent -- correctly observing staleness only to act on it "
    "as if it were current defeats the entire point. If a claim isn't "
    'present in that side\'s latest round, it does not belong in "issues" '
    "AT ALL, full stop -- not with a caveat, not framed "
    'as historical context, not included "for completeness." Leave it '
    "out entirely, as if it had never been a problem, because for the "
    "current state of the debate, it isn't one anymore.")

_REVIEW_MAX_TOKENS = 2048

_DEBATE_SIDE_ROLES: tuple[tuple[AgentRole, DebateSide], ...] = (
    (AgentRole.BULL, "bull"), (AgentRole.BEAR, "bear"))

_NOT_RETRIABLE_FAILURE_KINDS = AVAILABILITY_FAILURE_KINDS | {
    "non_english", "credit_exhausted"
}


def _not_retriable_failure_kind(out: AgentOutput) -> FailureKind | None:
    if out.status != AgentStatus.FAILED:
        return None
    if out.failure_kind in _NOT_RETRIABLE_FAILURE_KINDS:
        return out.failure_kind
    return None


def _review_shape_problem(checked_claims: Any, issues: Any) -> str | None:
    if not isinstance(checked_claims, list) or not all(
            isinstance(v, str) for v in checked_claims):
        kind = type(checked_claims).__name__
        return f"checked_claims is not a list of strings ({kind})"
    if not isinstance(issues, list):
        return f"issues is not a list ({type(issues).__name__})"
    return None


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text if text and text.lower() not in ("null", "none") else None


_ISSUE_QUOTE_CHARS = 200


def _issue_text(
        role: str | None, kind: IssueKind | None, description: str, *,
        figure: str | None = None, claimed_period: str | None = None,
        evidence_quote: str | None = None) -> str:
    label = kind.replace("_", " ") if kind else "issue"
    who = role.capitalize() if role in ("bull", "bear") else role
    text = (
        f"{who} ({label}): {description}"
        if who else f"({label}) {description}")
    details: list[str] = []
    if figure:
        details.append(f"figure {figure}")
    if claimed_period:
        details.append(f"given for {claimed_period}")
    if evidence_quote:
        quote = " ".join(evidence_quote.split())
        if len(quote) > _ISSUE_QUOTE_CHARS:
            quote = quote[:_ISSUE_QUOTE_CHARS].rstrip() + "..."
        source = (
            "the other side's point" if kind == "not_engaged" else "evidence")
        details.append(f'{source}: "{quote}"')
    return f"{text} [{'; '.join(details)}]" if details else text


def _earlier_other_side_text(
        arguments: Sequence[DebateArgument], side: str | None) -> str:
    own_rounds = [
        a.round_number for a in arguments if a.side == side and a.available
    ]
    if side not in ("bull", "bear") or not own_rounds:
        return ""
    latest = max(own_rounds)
    return "\n".join(
        a.argument for a in arguments
        if a.side != side and a.available and a.round_number < latest)


def review_skip_reason_for(exc: LLMError) -> str:
    name = type(exc).__name__
    if isinstance(exc, LLMRequestTimeoutError):
        return f"{name}: the LLM provider timed out"
    if isinstance(exc, LLMServerOverloadedError):
        return f"{name}: {FAILURE_KIND_TEXT['server_overload']}"
    if isinstance(exc, LLMRateLimitError):
        return f"{name}: {FAILURE_KIND_TEXT['rate_limit']}"
    return f"{name}: {str(exc)[:200]}"


class ReflectionAgent:

    def __init__(
            self, llm_router: LLMRouter | None = None,
            llm_tier: LLMTier = LLMTier.HIGHEST_ACCURACY) -> None:
        self.llm_router = llm_router or LLMRouter()
        self.llm_tier = llm_tier

    async def check(self, state: AnalysisState) -> ReflectionVerdict:
        with recording_served_models() as served:
            verdict = await self._check(state)
        return verdict.model_copy(
            update={"fallback_calls": fallback_call_count(served)})

    async def _check(self, state: AnalysisState) -> ReflectionVerdict:
        round_number = len(state.reflection_history) + 1

        not_retried_issues: list[ReflectionIssue] = []
        not_retriable_roles: set[AgentRole] = set()
        mechanical_candidates: dict[AgentRole, ReflectionIssue] = {}

        def _mechanical(
                role: AgentRole, code: str, text: str) -> ReflectionIssue:
            return ReflectionIssue(code=code, role=role.value, text=text)

        for out in state.agent_outputs.values():
            kind = _not_retriable_failure_kind(out)
            if kind is not None:
                not_retriable_roles.add(out.role)
                not_retried_issues.append(
                    ReflectionIssue(
                        code=ISSUE_NOT_RETRIED, role=out.role.value,
                        failure_kind=kind, text=(
                            f"{out.role.value} agent {ISSUE_NOT_RETRIED} "
                            f"({kind}): "
                            f"{FAILURE_KIND_TEXT[kind]} -- retrying now would "
                            f"hit "
                            "the same provider condition, not fix a content "
                            "problem")))
                continue
            if isinstance(out.raw_output,
                          dict) and out.raw_output.get("no_evidence"):
                not_retriable_roles.add(out.role)
                not_retried_issues.append(
                    _mechanical(
                        out.role, ISSUE_NO_EVIDENCE_NOT_RETRIED,
                        f"{out.role.value} agent "
                        f"{ISSUE_NO_EVIDENCE_NOT_RETRIED}: "
                        "its live search returned no relevant item this run; "
                        "a "
                        "retry would search the same source again"))
                continue
            if out.status == AgentStatus.FAILED:
                mechanical_candidates[out.role] = _mechanical(
                    out.role, ISSUE_AGENT_FAILED,
                    f"{out.role.value} {ISSUE_AGENT_FAILED} "
                    f"({out.failure_kind or 'other'}): {out.summary[:300]}")
            elif out.hit_iteration_cap:
                mechanical_candidates[out.role] = _mechanical(
                    out.role, ISSUE_ITERATION_CAP,
                    f"{ISSUE_ITERATION_CAP} without volunteering Finish; "
                    "evidence may be thin")
            elif lacks_successful_tool_evidence(out):
                mechanical_candidates[out.role] = _mechanical(
                    out.role, ISSUE_ZERO_EVIDENCE,
                    f"finished with {ISSUE_ZERO_EVIDENCE} this run -- its "
                    "answer has no evidence of its own; call the tools and "
                    "answer only from what they return")
            elif not out.is_well_supported:
                mechanical_candidates[out.role] = _mechanical(
                    out.role, ISSUE_NOT_WELL_SUPPORTED,
                    f"output {ISSUE_NOT_WELL_SUPPORTED} (an unresolved "
                    "finish-validation, evidence-anchor, no-evidence or "
                    "verification issue); evidence may be thin")

        usable_count = sum(
            1 for out in state.agent_outputs.values()
            if is_usable_evidence(out))
        llm_issues, llm_candidates, review_skip_reason = (
            await self._review_citations(state, usable_count=usable_count))
        llm_review_ran = review_skip_reason is None

        retry_candidates = {
            **{
                role: issue.text
                for role, issue in mechanical_candidates.items()
            },
            **llm_candidates
        }

        if not_retriable_roles:
            retry_candidates = {
                role: reason
                for role, reason in retry_candidates.items()
                if role not in not_retriable_roles
            }

        side_issues: list[ReflectionIssue] = []
        for role, side in _DEBATE_SIDE_ROLES:
            latest = state.debate.latest_argument(side)
            if (latest is not None and not latest.available
                    and latest.unavailable_kind in AVAILABILITY_FAILURE_KINDS):
                kind = latest.unavailable_kind
                side_issues.append(
                    ReflectionIssue(
                        code=ISSUE_DEBATE_SIDE_NOT_REGENERATED, role=side,
                        failure_kind=kind, text=(
                            f"{side} {ISSUE_DEBATE_SIDE_NOT_REGENERATED} "
                            f"({kind}): "
                            f"{FAILURE_KIND_TEXT[kind]} -- the argument is "
                            f"left "
                            "missing rather than re-requested from the same "
                            "overloaded model")))
                retry_candidates.pop(role, None)

        issue_details = (
            not_retried_issues + side_issues +
            list(mechanical_candidates.values()) + llm_issues)

        data_agent_candidates = {
            role: reason
            for role, reason in retry_candidates.items()
            if role not in (AgentRole.BULL, AgentRole.BEAR)
        }
        if data_agent_candidates:
            retry_candidates = data_agent_candidates

        verified = (
            not retry_candidates and usable_count >= 1 and llm_review_ran)
        return ReflectionVerdict(
            round_number=round_number, verified=verified,
            issues=[issue.text
                    for issue in issue_details], issue_details=issue_details,
            retry_agents=retry_candidates, llm_review_ran=llm_review_ran,
            llm_review_skip_reason=review_skip_reason,
            usable_agent_count=usable_count,
            agent_count=len(state.agent_outputs))

    async def _review_call(self, messages: list[Any]) -> Any:
        return await self.llm_router.complete(
            self.llm_tier, messages, retries=AGENT_LLM_CALL_RETRIES,
            response_format="json_object", max_tokens=_REVIEW_MAX_TOKENS)

    async def _review_citations(
        self, state: AnalysisState, *, usable_count: int
    ) -> tuple[list[ReflectionIssue], dict[AgentRole, str], str | None]:
        if not state.agent_outputs:
            return [], {}, "no agent outputs to review"
        judge_verdict = state.debate.latest_verdict
        if judge_verdict is None and state.below_debate_evidence_minimum(
                settings.MIN_USABLE_AGENTS_FOR_DEBATE):
            logger.info(
                "reflection.citation_review_skipped_insufficient_evidence",
                usable_agents=usable_count)
            return ([], {}, (
                "no debate to review: too few agents produced usable "
                f"evidence ({usable_count})"))
        if (judge_verdict is not None and judge_verdict.judge_failed
                and not judge_verdict.retry_round):
            logger.info(
                "reflection.citation_review_skipped_no_verdict",
                failure_reason=judge_verdict.failure_reason)
            return ([], {}, (
                "no verdict to review: the Judge produced none "
                f"({judge_verdict.failure_reason or 'judge_failed'})"))
        if usable_count == 0:
            return [], {}, "no agent produced usable evidence to review"

        messages = self._build_messages(state)
        try:
            try:
                response = await self._review_call(messages)
            except LLMRateLimitError as first_exc:
                if settings.REFLECTION_REVIEW_DELAYED_RETRY_SECONDS <= 0:
                    raise
                delay, free_model = await capacity_aware_delay(
                    self.llm_tier,
                    settings.REFLECTION_REVIEW_DELAYED_RETRY_SECONDS)
                logger.warning(
                    "reflection.citation_review_delayed_retry",
                    delay_seconds=delay, free_model=free_model,
                    error=str(first_exc)[:200])
                if delay > 0:
                    await asyncio.sleep(delay)
                response = await self._review_call(messages)
        except LLMError as exc:
            logger.info(
                "reflection.citation_review_unavailable", error=str(exc))
            return [], {}, review_skip_reason_for(exc)

        parsed = parse_json_dict(
            response.content, log_event="reflection.unparseable_response")
        if parsed is not None and not {"checked_claims", "issues"} & set(
                parsed):
            log_unparseable(
                "reflection.unparseable_response", response.content,
                reason="no checked_claims or issues key")
            parsed = None
        if parsed is None:
            return [], {}, "the review response could not be parsed"

        raw_checked = parsed.get("checked_claims") or []
        raw_issues = parsed.get("issues") or []
        shape_problem = _review_shape_problem(raw_checked, raw_issues)
        if shape_problem is not None:
            logger.warning(
                "reflection.review_bad_shape", problem=shape_problem)
            return ([], {}, (
                f"the review response had an unexpected shape: "
                f"{shape_problem}"))
        checked_claims: list[str] = list(raw_checked)
        if not checked_claims:
            logger.info(
                "reflection.citation_review_no_checked_claims",
                had_agent_outputs=bool(state.agent_outputs))
        else:
            logger.info(
                "reflection.citation_review_checked_claims",
                count=len(checked_claims))

        valid_roles: set[AgentRole] = set(state.agent_outputs.keys())
        debate_sides = {a.side for a in state.debate.arguments}
        if "bull" in debate_sides:
            valid_roles.add(AgentRole.BULL)
        if "bear" in debate_sides:
            valid_roles.add(AgentRole.BEAR)

        evidence_text = format_evidence(state)
        debate_arguments = self._reviewed_arguments(state)
        reference_text = format_grounding_reference(state)
        issues: list[ReflectionIssue] = []
        reasons: dict[AgentRole, list[str]] = {}
        for item in raw_issues:
            issue = self._structured_issue(
                item, valid_roles, evidence_text, debate_arguments,
                reference_text)
            if issue is None:
                continue
            issues.append(issue)
            if issue.role is not None:
                reasons.setdefault(AgentRole(issue.role),
                                   []).append(issue.text)
        retry_candidates = {
            role: "; ".join(texts)
            for role, texts in reasons.items()
        }
        return issues, retry_candidates, None

    @staticmethod
    def _structured_issue(
            item: Any, valid_roles: set[AgentRole], evidence_text: str,
            debate_arguments: Sequence[DebateArgument],
            reference_text: str) -> ReflectionIssue | None:
        if not isinstance(item, dict):
            logger.warning("reflection.issue_bad_shape", item=str(item)[:300])
            return None
        description = _optional_text(item.get("description"))
        if description is None:
            logger.warning("reflection.issue_bad_shape", item=str(item)[:300])
            return None
        raw_kind = (_optional_text(item.get("kind")) or "").lower()
        kind: IssueKind | None = next(
            (k for k in ISSUE_KINDS if k == raw_kind), None)
        if kind is None:
            logger.info(
                "reflection.issue_unknown_kind", kind=raw_kind[:40],
                description=description[:300])
        role: str | None = None
        raw_role = (_optional_text(item.get("role")) or "").lower()
        if raw_role:
            try:
                parsed_role = AgentRole(raw_role)
            except ValueError:
                parsed_role = None
            if parsed_role is not None and parsed_role in valid_roles:
                role = parsed_role.value
            else:
                logger.info(
                    "reflection.issue_role_not_routable", role=raw_role[:40],
                    description=description[:300])
        quote = _optional_text(item.get("evidence_quote")) or ""
        quote_source = (
            _earlier_other_side_text(debate_arguments, role)
            if kind == "not_engaged" else evidence_text)
        if not quote or not _quote_is_grounded(quote, quote_source):
            logger.warning(
                "reflection.issue_quote_not_grounded", kind=kind, role=role,
                description=description[:300], evidence_quote=quote[:300])
            return None
        figure = _optional_text(item.get("figure"))
        if (kind == "unsupported" and role in ("bull", "bear") and figure
                and _FIGURE_RE.search(figure)
                and not find_ungrounded_figures(figure, reference_text)):
            logger.info(
                "reflection.mechanical_false_positive_discarded", role=role,
                figure=figure[:100], description=description[:300])
            return None
        claimed_period = _optional_text(item.get("claimed_period"))
        return ReflectionIssue(
            code=ISSUE_LLM_REVIEW, role=role, kind=kind, figure=figure,
            claimed_period=claimed_period, evidence_quote=quote,
            text=_issue_text(
                role, kind, description, figure=figure,
                claimed_period=claimed_period, evidence_quote=quote))

    @staticmethod
    def _reviewed_arguments(state: AnalysisState) -> list[DebateArgument]:
        return [
            a for a in state.debate.transcript_arguments()
            if not state.debate.is_argument_superseded(a)
        ]

    @staticmethod
    def _debate_transcript(state: AnalysisState) -> str:
        arguments = ReflectionAgent._reviewed_arguments(state)
        latest_round_by_side: dict[str, int] = {}
        for a in arguments:
            latest_round_by_side[a.side] = max(
                a.round_number, latest_round_by_side.get(a.side, 0))
        return "\n".join(
            f"Round {a.round_number} {a.side}" + (
                " (LATEST)" if a.round_number ==
                latest_round_by_side.get(a.side) else "") + f": {a.argument}"
            for a in arguments)

    def _build_messages(self, state: AnalysisState) -> list[LLMMessage]:
        debate_text = self._debate_transcript(state)
        user_content = (
            f"Ticker: {state.ticker}\n\n"
            f"Agent evidence:\n{format_evidence(state)}\n\n"
            f"Debate so far:\n" + (debate_text or "(no debate yet)"))
        return [
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_content)
        ]
