import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, Protocol
import structlog
from ai.agents.debate_side import DebateSide, DebateSideAgent
from ai.agents.evidence_grounding import non_english_reason
from ai.agents.judge import JudgeAgent
from ai.agents.loop import BaseAgent
from ai.agents.portfolio import PortfolioAgent
from ai.agents.reflection import ReflectionAgent
from ai.agents.supervisor import select_agents
from ai.graph.edges import MAX_REFLECTION_RETRIES, node_name
from ai.graph.state import (ISSUE_REFLECTION_CRASHED,
                            ISSUE_RETRY_DELAYED_CAPACITY,
                            ISSUE_RETRY_NO_PROGRESS,
                            ISSUE_RETRY_SKIPPED_CAPACITY, AgentOutput,
                            AnalysisState, DebateArgument, DebateState,
                            FailureKind, JudgeVerdict, ReflectionIssue,
                            ReflectionVerdict, merge_debate_state)
from ai.llm.base import (LLMNonEnglishOutputError, LLMProviderTimeoutError,
                         LLMRateLimitError, LLMServerOverloadedError, LLMTier)
from ai.llm.capacity import (capacity_aware_delay, chain_capacity,
                             free_chain_model)
from ai.llm.router import tier_model_chain
from ai.recommendation.engine import (build_final_recommendation,
                                      no_verdict_reason)
from config import settings
from shared.enums import (AgentRole, AgentStatus, AnalysisFailureReason,
                          RecommendationAction)

logger = structlog.get_logger(__name__)

_FAN_OUT_STAGGER_ORDER: tuple[AgentRole, ...] = (
    AgentRole.MARKET, AgentRole.NEWS, AgentRole.SENTIMENT, AgentRole.FILING,
    AgentRole.TECHNICAL, AgentRole.FUNDAMENTAL, AgentRole.RISK,
    AgentRole.MACRO, AgentRole.PORTFOLIO)
_FAN_OUT_STAGGER_SECONDS = 0.65


async def _run_agent_safely(
        role: AgentRole, agent: BaseAgent,
        state: AnalysisState) -> AgentOutput:
    if role in _FAN_OUT_STAGGER_ORDER:
        stagger_seconds = (
            _FAN_OUT_STAGGER_ORDER.index(role) * _FAN_OUT_STAGGER_SECONDS)
        if stagger_seconds > 0:
            await asyncio.sleep(stagger_seconds)
    started_at = datetime.now(UTC)
    try:
        output = await agent.run(state)
    except Exception as exc:
        failure_kind = _failure_kind_of(exc)
        logger.warning(
            "agent_node.failed", role=role.value, error=str(exc),
            failure_kind=failure_kind)
        output = AgentOutput(
            role=role, status=AgentStatus.FAILED,
            summary=f"{role.value} agent failed: {exc}", score=0.0,
            is_well_supported=False, failure_kind=failure_kind,
            retry_of_feedback=state.reflection_feedback.get(role))
    else:
        language_issue = non_english_reason(output.summary)
        if language_issue is not None:
            logger.warning(
                "agent_node.non_english_output_rejected", role=role.value,
                reason=language_issue)
            output = AgentOutput(
                role=role, status=AgentStatus.FAILED, summary=(
                    f"{role.value} agent output rejected: non-English text"),
                score=0.0, is_well_supported=False, failure_kind="non_english",
                iterations_used=output.iterations_used,
                hit_iteration_cap=output.hit_iteration_cap,
                retry_of_feedback=output.retry_of_feedback,
                raw_output={"non_english_reason": language_issue})
    output.started_at = started_at
    output.completed_at = datetime.now(UTC)
    return output


def _failure_kind_of(exc: BaseException) -> FailureKind:
    if isinstance(exc, (LLMServerOverloadedError, LLMProviderTimeoutError)):
        return "server_overload"
    if isinstance(exc, LLMRateLimitError):
        return "rate_limit"
    if isinstance(exc, LLMNonEnglishOutputError):
        return "non_english"
    return "other"


def _make_node(role: AgentRole,
               agent_cls: type[BaseAgent]) -> Callable[[AnalysisState], Any]:

    async def _node(state: AnalysisState) -> dict[str, Any]:
        agent = agent_cls(role=role)
        output = await _run_agent_safely(role, agent, state)
        return {"agent_outputs": {role: output}}

    _node.__name__ = node_name(role)
    return _node


def portfolio_node_factory() -> Callable[[AnalysisState], Any]:

    async def portfolio_node(state: AnalysisState) -> dict[str, Any]:
        agent = PortfolioAgent(
            role=AgentRole.PORTFOLIO, portfolio_id=state.portfolio_id)
        output = await _run_agent_safely(AgentRole.PORTFOLIO, agent, state)
        return {"agent_outputs": {AgentRole.PORTFOLIO: output}}

    return portfolio_node


async def episodic_memory_node(state: AnalysisState) -> dict[str, Any]:
    from ai.memory.episodic import retrieve_episodic_context

    if settings.EPISODIC_CONTEXT_LIMIT <= 0:
        return {"episodic_context": []}
    try:
        context = await retrieve_episodic_context(
            state.ticker, user_id=state.user_id or None)
    except Exception as exc:
        logger.warning(
            "episodic_memory_node.retrieval_failed", ticker=state.ticker,
            error=str(exc))
        return {"episodic_context": []}
    logger.info(
        "episodic_memory_node.retrieved", ticker=state.ticker,
        count=len(context),
        scored=sum(1 for hit in context if hit.outcome is not None))
    return {"episodic_context": context}


async def _procedural_hints(
        selected: list[AgentRole], ticker: str,
        user_id: str) -> dict[AgentRole, str]:
    from ai.memory.procedural import owner_uuid, retrieve_procedural_hints

    owner_id = owner_uuid(user_id)
    hints: dict[AgentRole, str] = {}
    for role in selected:
        try:
            text = await retrieve_procedural_hints(
                role, ticker, user_id=owner_id)
        except Exception as exc:
            logger.warning(
                "supervisor_node.procedural_hints_failed", role=role.value,
                ticker=ticker, error=str(exc))
            continue
        if text is not None:
            hints[role] = text
    return hints


async def _fresh_filing_context(state: AnalysisState) -> str | None:
    from ai.knowledge.fresh_filings import check_for_fresh_filing_content

    try:
        return await check_for_fresh_filing_content(state.ticker)
    except Exception as exc:
        logger.warning(
            "supervisor_node.fresh_filing_check_failed", ticker=state.ticker,
            error=str(exc))
        return None


async def _ticker_kg_context(state: AnalysisState) -> str | None:
    from ai.knowledge.kg_context import get_kg_context_for_ticker

    try:
        kg_context = await get_kg_context_for_ticker(
            state.ticker, viewer_user_id=state.user_id or None)
    except Exception as exc:
        logger.warning(
            "supervisor_node.kg_context_failed", ticker=state.ticker,
            error=str(exc))
        return None
    logger.info(
        "supervisor_node.kg_context_injected", ticker=state.ticker,
        found=kg_context is not None,
        char_count=len(kg_context) if kg_context else 0)
    return kg_context


async def _portfolio_kg_context(
        portfolio_id: str, viewer_user_id: str | None) -> str | None:
    from ai.knowledge.kg_context import get_kg_context_for_portfolio
    from ai.providers.registry import get_market_data_provider
    from clients.core_client import get_portfolio_holdings

    holdings = await get_portfolio_holdings(portfolio_id)
    provider = get_market_data_provider()
    valued: list[tuple[float, str]] = []
    for h in holdings:
        ticker = h.get("ticker")
        quantity = h.get("quantity")
        if not ticker or not quantity:
            continue
        try:
            quote = await provider.get_current_price(ticker)
            valued.append((float(quantity) * float(quote.price), ticker))
        except Exception:
            continue
    top_tickers = [
        t for _, t in sorted(valued, key=lambda v: v[0], reverse=True)[:20]
    ]
    portfolio_kg_context = None
    if top_tickers:
        portfolio_kg_context = await get_kg_context_for_portfolio(
            top_tickers, viewer_user_id=viewer_user_id)
    logger.info(
        "supervisor_node.portfolio_kg_context_injected",
        portfolio_id=portfolio_id, tickers_ranked=top_tickers,
        found=portfolio_kg_context is not None,
        char_count=len(portfolio_kg_context) if portfolio_kg_context else 0)
    return portfolio_kg_context


_FILING_CONTEXT_ROLES = frozenset(
    {AgentRole.FILING, AgentRole.FUNDAMENTAL, AgentRole.RISK})


async def supervisor_node(state: AnalysisState) -> dict[str, Any]:
    selected, skipped = select_agents(
        requested_agents=state.requested_agents,
        analysis_type=state.analysis_type, holdings_count=state.holdings_count)
    procedural_hints = await _procedural_hints(
        selected, state.ticker, state.user_id)

    needs_filing_context = bool(_FILING_CONTEXT_ROLES & set(selected))
    fresh_filing_context = (
        await _fresh_filing_context(state) if needs_filing_context else None)
    kg_context = (
        await _ticker_kg_context(state) if needs_filing_context else None)

    portfolio_kg_context = None
    if AgentRole.PORTFOLIO in selected and state.portfolio_id:
        try:
            portfolio_kg_context = await _portfolio_kg_context(
                state.portfolio_id, state.user_id or None)
        except Exception as exc:
            logger.warning(
                "supervisor_node.portfolio_kg_context_failed",
                portfolio_id=state.portfolio_id, error=str(exc))

    return {
        "selected_agents": selected,
        "skipped_agents": skipped,
        "procedural_hints": procedural_hints,
        "fresh_filing_context": fresh_filing_context,
        "kg_context": kg_context,
        "portfolio_kg_context": portfolio_kg_context
    }


async def agent_join_node(state: AnalysisState) -> dict[str, Any]:
    logger.debug(
        "debate_debug.agent_join_node.enter",
        agent_outputs_keys=[r.value for r in state.agent_outputs],
        reflection_retry_count=state.reflection_retry_count,
        debate_round_number=state.debate.round_number)
    return {}


_REPAIRABLE_UNAVAILABLE_KINDS = frozenset({"server_overload", "rate_limit"})


def _repairable_missing_sides(debate: DebateState) -> list[DebateSide]:
    sides: list[DebateSide] = []
    for side in debate.unavailable_latest_sides():
        failed = debate.latest_argument(side)
        if (failed is not None
                and failed.unavailable_kind in _REPAIRABLE_UNAVAILABLE_KINDS):
            sides.append(side)
    return sides


async def debate_join_node(state: AnalysisState) -> dict[str, Any]:
    if state.debate_side_repair_used or state.debate.verdicts:
        return {}
    if _is_single_side_correction_pass(state):
        return {}
    sides = _repairable_missing_sides(state.debate)
    if not sides:
        return {}
    delay, free_model = await capacity_aware_delay(
        LLMTier.HIGHEST_ACCURACY, settings.DEBATE_SIDE_REPAIR_DELAY_SECONDS)
    for side in sides:
        failed = state.debate.latest_argument(side)
        logger.warning(
            "debate_join_node.side_repair_retry", side=side, failure_kind=(
                failed.unavailable_kind if failed is not None else None),
            delay_seconds=delay, free_model=free_model,
            sides_repaired=len(sides))
    if delay > 0:
        await asyncio.sleep(delay)

    async def _repair(side: DebateSide) -> DebateArgument | None:
        if side == "bear" and len(sides) == 2:
            await asyncio.sleep(_DEBATE_SIDE_STAGGER_SECONDS)
        argument = await _side_argument(
            side, state, failed_event="debate_join_node.side_repair_failed")
        if not argument.available:
            return None
        logger.info("debate_join_node.side_repaired", side=side)
        return argument

    results = await asyncio.gather(*(_repair(side) for side in sides))
    repaired = [argument for argument in results if argument is not None]
    if not repaired:
        return {"debate_side_repair_used": True}
    return {
        "debate": DebateState(arguments=repaired),
        "debate_side_repair_used": True
    }


_DEBATE_SIDE_STAGGER_SECONDS = 0.5


async def _side_argument(
        side: DebateSide, state: AnalysisState, *,
        failed_event: str) -> DebateArgument:
    try:
        return await DebateSideAgent(side).make_argument(state)
    except Exception as exc:
        kind = _failure_kind_of(exc)
        logger.warning(
            failed_event, side=side, error=str(exc), failure_kind=kind)
        return DebateArgument(
            round_number=state.debate.round_number + 1, side=side,
            argument=f"[{side} argument unavailable ({kind}): {exc}]",
            available=False, unavailable_kind=kind)


class _StateNode(Protocol):

    def __call__(self,
                 state: AnalysisState) -> Coroutine[Any, Any, dict[str, Any]]:
        ...


def _debate_side_node(side: DebateSide) -> _StateNode:
    name = f"{side}_node"

    async def _node(state: AnalysisState) -> dict[str, Any]:
        logger.debug(
            f"debate_debug.{name}.enter",
            round_number_in=state.debate.round_number,
            reflection_retry_count=state.reflection_retry_count,
            existing_argument_count=len(state.debate.arguments))
        if side == "bear":
            await asyncio.sleep(_DEBATE_SIDE_STAGGER_SECONDS)
        argument = await _side_argument(
            side, state, failed_event=f"{name}.failed")
        previous = state.debate.latest_argument(side)
        if (not argument.available
                and argument.unavailable_kind in _REPAIRABLE_UNAVAILABLE_KINDS
                and _is_single_side_correction_pass(state)
                and previous is not None and previous.available):
            logger.warning(
                f"{name}.correction_failed_kept_previous",
                failure_kind=argument.unavailable_kind,
                kept_round_number=previous.round_number)
            return {}
        logger.debug(
            f"debate_debug.{name}.exit",
            argument_round_number=argument.round_number)
        return {"debate": DebateState(arguments=[argument])}

    _node.__name__ = name
    return _node


bull_node = _debate_side_node("bull")
bear_node = _debate_side_node("bear")


def _is_single_side_correction_pass(state: AnalysisState) -> bool:
    retry_targets = {
        role
        for role in state.pending_retry_agents
        if role in (AgentRole.BULL, AgentRole.BEAR)
    }
    return len(retry_targets) == 1


async def _late_side_repair(
        state: AnalysisState) -> list[DebateArgument] | None:
    sides = _repairable_missing_sides(state.debate)
    if not sides:
        return None
    free_model = await free_chain_model(LLMTier.HIGHEST_ACCURACY)
    if free_model is None:
        return None
    logger.warning(
        "judge_node.late_side_repair", sides=sides, free_model=free_model)

    async def _repair(side: DebateSide) -> DebateArgument:
        if side == "bear" and len(sides) == 2:
            await asyncio.sleep(_DEBATE_SIDE_STAGGER_SECONDS)
        return await _side_argument(
            side, state, failed_event="judge_node.late_side_repair_failed")

    results = await asyncio.gather(*(_repair(side) for side in sides))
    repaired = [argument for argument in results if argument.available]
    for argument in repaired:
        logger.info("judge_node.late_side_repaired", side=argument.side)
    return repaired


async def _judge_retry_wait(
        attempts: int, seconds_used: float) -> float | None:
    remaining = settings.JUDGE_RETRY_BUDGET_SECONDS - seconds_used
    chain = tier_model_chain(LLMTier.HIGHEST_ACCURACY)
    if (not chain or attempts >= settings.JUDGE_RETRY_MAX_ATTEMPTS
            or remaining <= 0):
        return None
    capacity = await chain_capacity(chain)
    if capacity is None:
        return 0.0
    wait, scale, _ = capacity
    if scale == "day":
        return None
    return min(wait, remaining)


async def judge_node(state: AnalysisState) -> dict[str, Any]:
    logger.debug(
        "debate_debug.judge_node.enter",
        round_number_in=state.debate.round_number,
        mutual_rounds_completed_in=state.debate.mutual_rounds_completed,
        argument_count=len(state.debate.arguments),
        argument_rounds=[a.round_number for a in state.debate.arguments],
        pending_retry_agents_in=[r.value for r in state.pending_retry_agents])
    is_correction_pass = _is_single_side_correction_pass(state)
    retry_attempts = state.judge_retry_attempts
    retry_seconds = state.judge_retry_seconds
    late_repair_used = state.debate_late_side_repair_used
    repaired: list[DebateArgument] = []
    judge_state = state

    async def _repair_before_judge() -> None:
        nonlocal judge_state, late_repair_used
        if late_repair_used or is_correction_pass:
            return
        try:
            result = await _late_side_repair(judge_state)
        except Exception as exc:
            logger.warning("judge_node.late_side_repair_error", error=str(exc))
            return
        if result is None:
            return
        late_repair_used = True
        if result:
            repaired.extend(result)
            judge_state = state.model_copy(
                update={
                    "debate": merge_debate_state(
                        state.debate, DebateState(arguments=repaired))
                })

    try:
        await _repair_before_judge()
        while True:
            try:
                verdict = await JudgeAgent().assess_round(judge_state)
                break
            except LLMRateLimitError as exc:
                wait = await _judge_retry_wait(retry_attempts, retry_seconds)
                if wait is None:
                    raise
                retry_attempts += 1
                retry_seconds += wait
                logger.warning(
                    "judge_node.delayed_retry", error=str(exc),
                    failure_kind=_failure_kind_of(exc), attempt=retry_attempts,
                    delay_seconds=round(wait, 1))
                if wait > 0:
                    await asyncio.sleep(wait)
                await _repair_before_judge()
    except Exception as exc:
        logger.warning(
            "judge_node.failed", error=str(exc),
            failure_kind=_failure_kind_of(exc))
        round_number = state.debate.round_number + 1
        verdict = JudgeVerdict(
            round_number=round_number, converged=False,
            leaning=RecommendationAction.HOLD, confidence=0.0,
            rationale=f"Judge unavailable: {exc}", judge_failed=True,
            failure_reason=AnalysisFailureReason.JUDGE_UNAVAILABLE)
    mutual_rounds_completed = state.debate.mutual_rounds_completed + (
        0 if is_correction_pass else 1)
    logger.debug(
        "debate_debug.judge_node.exit",
        verdict_round_number=verdict.round_number,
        mutual_rounds_completed_out=mutual_rounds_completed,
        is_correction_pass=is_correction_pass, converged=verdict.converged)
    return {
        "debate": DebateState(
            round_number=verdict.round_number,
            mutual_rounds_completed=mutual_rounds_completed,
            arguments=repaired, verdicts=[verdict],
            converged=verdict.converged),
        "pending_retry_agents": [],
        "judge_retry_attempts": retry_attempts,
        "judge_retry_seconds": retry_seconds,
        "debate_late_side_repair_used": late_repair_used
    }


_RATE_LIMIT_RETRY_BACKOFF_SECONDS = 20.0


async def _rate_limit_gate_for_retry(
    retry_now: dict[AgentRole, str]
) -> tuple[dict[AgentRole, str], list[ReflectionIssue]]:
    if not retry_now or settings.LLM_DEFAULT_PROVIDER != "google":
        return retry_now, []

    bull_bear_roles = {AgentRole.BULL, AgentRole.BEAR} & retry_now.keys()
    data_roles = retry_now.keys() - bull_bear_roles

    gated = dict(retry_now)
    notes: list[ReflectionIssue] = []

    async def _gate_tier(roles: set[AgentRole], tier: LLMTier) -> None:
        chain = tier_model_chain(tier)
        if not roles or not chain:
            return
        capacity = await chain_capacity(chain)
        if capacity is None:
            return
        wait, scale, model = capacity
        role_names = ", ".join(sorted(r.value for r in roles))
        models = ", ".join(chain)
        if scale == "day":
            for role in roles:
                gated.pop(role, None)
            logger.warning(
                "reflection_node.rate_limit_skip", models=chain,
                roles=sorted(r.value for r in roles))
            notes.append(
                ReflectionIssue(
                    code=ISSUE_RETRY_SKIPPED_CAPACITY, text=(
                        f"Retry for {role_names}: "
                        f"{ISSUE_RETRY_SKIPPED_CAPACITY} -- every model "
                        f"this retry could use ({models}) is out of its "
                        "daily quota (429) across every pooled Google "
                        "project right now; retrying would fail "
                        "identically, so this evidence stays as-is.")))
            return
        bounded_wait = min(wait, _RATE_LIMIT_RETRY_BACKOFF_SECONDS)
        reason = f"no model this retry could use ({models}) was free: " + (
            f"{model} was confirmed overloaded by repeated 503s earlier "
            "this run (Google-side 'high demand', short-lived)"
            if scale == "server_error" else
            f"{model} sent no response within the request timeout "
            "earlier this run" if scale == "timeout" else
            f"{model} was near its per-minute rate limit (429) across "
            "every pooled Google project")
        logger.warning(
            "reflection_node.rate_limit_backoff", model=model, models=chain,
            roles=sorted(r.value
                         for r in roles), wait_seconds=round(bounded_wait, 1),
            real_remaining_wait_seconds=round(wait, 1), scale=scale)
        await asyncio.sleep(bounded_wait)
        notes.append(
            ReflectionIssue(
                code=ISSUE_RETRY_DELAYED_CAPACITY, text=(
                    f"Retry for {role_names}: "
                    f"{ISSUE_RETRY_DELAYED_CAPACITY} "
                    f"({bounded_wait:.0f}s) -- {reason}.")))

    await _gate_tier(data_roles, LLMTier.CHEAP)
    await _gate_tier(bull_bear_roles, LLMTier.HIGHEST_ACCURACY)
    return gated, notes


def _drop_no_progress_retries(
    state: AnalysisState, retry_now: dict[AgentRole, str]
) -> tuple[dict[AgentRole, str], list[ReflectionIssue]]:
    kept: dict[AgentRole, str] = {}
    notes: list[ReflectionIssue] = []
    for role, reason in retry_now.items():
        output = state.agent_outputs.get(role)
        if output is not None and output.retry_of_feedback == reason:
            logger.info(
                "reflection_node.retry_no_progress_skip", role=role.value)
            notes.append(
                ReflectionIssue(
                    code=ISSUE_RETRY_NO_PROGRESS, role=role.value, text=(
                        f"Retry for {role.value} not repeated: its previous "
                        f"retry "
                        "was given this same feedback and the same issue "
                        "remains.")))
            continue
        kept[role] = reason
    return kept, notes


async def reflection_node(state: AnalysisState) -> dict[str, Any]:
    logger.debug(
        "debate_debug.reflection_node.enter",
        reflection_retry_count_in=state.reflection_retry_count,
        reflection_history_len=len(state.reflection_history),
        debate_round_number=state.debate.round_number)
    try:
        verdict = await ReflectionAgent().check(state)
    except Exception as exc:
        logger.warning("reflection_node.failed", error=str(exc))
        crash_text = f"Reflection check failed: {exc}"
        verdict = ReflectionVerdict(
            round_number=len(state.reflection_history) + 1, verified=False,
            issues=[crash_text], issue_details=[
                ReflectionIssue(
                    code=ISSUE_REFLECTION_CRASHED, text=crash_text)
            ], retry_agents={}, llm_review_ran=False, llm_review_skip_reason=(
                f"reflection check crashed: {type(exc).__name__}"),
            agent_count=len(state.agent_outputs),
            usable_agent_count=state.usable_agent_count())

    under_cap = state.reflection_retry_count < MAX_REFLECTION_RETRIES
    retry_now = (
        verdict.retry_agents if (verdict.retry_agents and under_cap) else {})

    if retry_now:
        retry_now, no_progress_notes = _drop_no_progress_retries(
            state, retry_now)
        if no_progress_notes:
            verdict = verdict.with_extra_issues(no_progress_notes)
    if retry_now:
        retry_now, rate_limit_notes = await _rate_limit_gate_for_retry(
            retry_now)
        if rate_limit_notes:
            verdict = verdict.with_extra_issues(rate_limit_notes)

    current_mutual_round = state.debate.mutual_rounds_completed
    bull_bear_targets = {
        role
        for role in retry_now if role in (AgentRole.BULL, AgentRole.BEAR)
    }
    single_side_correction = len(bull_bear_targets) == 1
    correction_cap_hit = (
        single_side_correction
        and state.debate.correction_used_for_round == current_mutual_round)
    if correction_cap_hit:
        logger.debug(
            "debate_debug.reflection_node.correction_cap_hit",
            mutual_round=current_mutual_round,
            dropped_role=next(iter(bull_bear_targets)).value)
        retry_now = {
            role: reason
            for role, reason in retry_now.items()
            if role not in (AgentRole.BULL, AgentRole.BEAR)
        }
        single_side_correction = False

    result: dict[str, Any] = {
        "reflection_history": [verdict],
        "reflection_feedback": retry_now,
        "pending_retry_agents": list(retry_now.keys()),
        "reflection_retry_count": state.reflection_retry_count +
        (1 if retry_now else 0)
    }
    if single_side_correction:
        result["debate"] = DebateState(
            correction_used_for_round=current_mutual_round)
    elif retry_now and not bull_bear_targets:
        result["debate"] = DebateState(
            superseded_through_round=state.debate.round_number)

    logger.debug(
        "debate_debug.reflection_node.exit", under_cap=under_cap,
        pending_retry_agents=[r.value for r in retry_now],
        reflection_retry_count_out=result["reflection_retry_count"],
        single_side_correction=single_side_correction,
        current_mutual_round=current_mutual_round)
    return result


async def recommendation_node(state: AnalysisState) -> dict[str, Any]:
    reason = no_verdict_reason(state)
    if reason is not None:
        logger.warning(
            "recommendation_node.no_verdict", ticker=state.ticker,
            reason=reason)
        return {"final_recommendation": None, "no_verdict_reason": reason}
    verdict = state.debate.latest_verdict
    assert verdict is not None
    recommendation = build_final_recommendation(state, verdict)
    return {"final_recommendation": recommendation.model_dump(mode="json")}
