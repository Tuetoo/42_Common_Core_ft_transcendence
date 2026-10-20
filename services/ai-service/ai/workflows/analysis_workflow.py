from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
import structlog
from ai.experiment_isolation import isolation_skips
from ai.graph.builder import build_analysis_graph
from ai.graph.state import (AgentOutput, AnalysisState, PriorRecommendation,
                            ReflectionVerdict, is_usable_evidence)
from ai.knowledge.archival import archive_agent_report, is_archivable_role
from ai.knowledge.recycled_content import recycled_share
from ai.memory.agent_memory import (maybe_remember_trajectory, owner_uuid,
                                    stored_judge_verdict)
from ai.recommendation.position_sizing import (BudgetBlockedNote,
                                               current_position_weight_pct,
                                               generate_position_sizing,
                                               tier_side_for_action)
from ai_events import (TIER_CANCEL_REASON_SUPERSEDED, analysis_event_payload,
                       publish_event, publish_events,
                       recommendation_ready_payload, tier_cancelled_events)
from clients.core_client import get_portfolio_holdings
from database import SessionLocal
from observability import record_analysis_completed, record_analysis_failed
from sqlalchemy.ext.asyncio import AsyncSession
from domains.analysis.model import Recommendation
from domains.analysis.repository import (AgentCatalogRepository,
                                         AnalysisPersistenceRepository,
                                         AnalysisRepository,
                                         RecommendationTierRepository,
                                         TierInvalidation)
from domains.memory.repository import EpisodicMemoryRepository
from shared.enums import (AgentRole, AgentStatus, AnalysisFailureReason,
                          AnalysisType, RecommendationAction, WorkflowStatus)
from shared.events import EventName
from shared.exceptions import UpstreamServiceError

logger = structlog.get_logger(__name__)


def _archival_recycled_share(output: AgentOutput) -> float:
    raw = output.raw_output if isinstance(output.raw_output, dict) else {}
    if raw.get("reused_prior_note"):
        return 1.0
    return recycled_share(output.summary, raw.get("scratchpad"))


def flagged_role_sets(
    reflection_history: list[ReflectionVerdict]
) -> tuple[set[AgentRole], set[AgentRole]]:
    all_rounds = {
        role
        for verdict in reflection_history
        for role in verdict.retry_agents
    }
    last_round = (
        set(reflection_history[-1].retry_agents)
        if reflection_history else set())
    return all_rounds, last_round


_GRAPH_RECURSION_LIMIT = 100


async def _resolve_holdings_context(portfolio_id: str | None,
                                    ticker: str) -> tuple[int, float | None]:
    if portfolio_id is None:
        return 0, None
    try:
        holdings = await get_portfolio_holdings(portfolio_id)
    except UpstreamServiceError:
        logger.warning(
            "holdings_lookup_failed_degrading_to_zero",
            portfolio_id=portfolio_id)
        return 0, None
    held = sum(
        float(h.get("quantity") or 0.0) for h in holdings
        if str(h.get("ticker", "")).upper() == ticker.upper())
    return len(holdings), held


async def _resolve_position_weight(
        portfolio_id: str | None, ticker: str) -> float | None:
    if portfolio_id is None:
        return None
    try:
        return await current_position_weight_pct(portfolio_id, ticker)
    except Exception:
        logger.warning("position_weight_lookup_failed", ticker=ticker)
        return None


async def _resolve_prior_recommendation(
        portfolio_id: str | None, ticker: str) -> PriorRecommendation | None:
    if portfolio_id is None:
        return None
    if isolation_skips("prior_recommendation_read", ticker=ticker):
        return None
    try:
        async with SessionLocal() as session:
            repo = AnalysisRepository(session)
            prior = await repo.get_open_recommendation_for(
                portfolio_id=UUID(portfolio_id), ticker=ticker)
            if prior is None:
                return None
            runs = await repo.list_agent_run_outputs(prior.analysis_id)
    except Exception:
        logger.warning(
            "pending_recommendation_lookup_failed", ticker=ticker,
            portfolio_id=portfolio_id)
        return None

    try:
        action = RecommendationAction(prior.recommendation)
    except ValueError:
        return None
    judge_leaning, judge_confidence = stored_judge_verdict(
        prior.judge_leaning, prior.judge_confidence)
    return PriorRecommendation(
        analysis_id=str(prior.analysis_id), action=action,
        confidence=float(prior.confidence), issued_at=prior.created_at,
        accepted=prior.accepted_at is not None,
        well_supported_count=(
            _count_usable_persisted_runs(runs) if runs else None),
        judge_leaning=judge_leaning, judge_confidence=judge_confidence)


def _count_usable_persisted_runs(
        runs: list[tuple[str, str, dict[str, Any] | None]]) -> int:
    count = 0
    for name, status, output in runs:
        if (not isinstance(output, dict)
                or output.get("is_well_supported") is not True):
            continue
        try:
            rebuilt = AgentOutput(
                role=AgentRole(name), status=AgentStatus(status), summary="",
                score=0.0, is_well_supported=True, raw_output=output)
        except ValueError:
            continue
        if is_usable_evidence(rebuilt):
            count += 1
    return count


async def run_analysis_workflow(
        analysis_id: str, *, analysis_type: AnalysisType = AnalysisType.FULL,
        requested_agents: list[AgentRole] | None = None) -> None:
    with structlog.contextvars.bound_contextvars(analysis_id=analysis_id):
        await _run_analysis_workflow(
            analysis_id, analysis_type=analysis_type,
            requested_agents=requested_agents)


async def _run_analysis_workflow(
        analysis_id: str, *, analysis_type: AnalysisType,
        requested_agents: list[AgentRole] | None) -> None:
    analysis_uuid = UUID(analysis_id)

    async with SessionLocal() as session:
        analysis = await AnalysisRepository(session).get_by_id(analysis_uuid)
        user_id = str(analysis.user_id) if analysis.user_id else ""
        portfolio_id = (
            str(analysis.portfolio_id) if analysis.portfolio_id else None)
        ticker = analysis.ticker
        prior_status = analysis.status
        started = await AnalysisRepository(session).set_status_and_reason(
            analysis_uuid, WorkflowStatus.RUNNING.value, None,
            only_if_active=True)
        await session.commit()
    if started is None:
        logger.warning(
            "analysis_workflow_skipped_not_active", analysis_id=analysis_id,
            status=prior_status)
        return

    await publish_event(
        EventName.ANALYSIS_STARTED,
        analysis_event_payload(
            analysis_id=analysis_id, user_id=user_id, ticker=ticker,
            portfolio_id=portfolio_id))

    with structlog.contextvars.bound_contextvars(ticker=ticker):
        try:
            await _run_and_persist(
                analysis_id=analysis_id, user_id=user_id,
                portfolio_id=portfolio_id, ticker=ticker,
                analysis_type=analysis_type, requested_agents=requested_agents)
        except Exception:
            logger.exception(
                "analysis_workflow_failed", analysis_id=analysis_id)
            await mark_analysis_failed(
                analysis_uuid, FAILURE_REASON_WORKFLOW_ERROR)


FAILURE_REASON_WORKFLOW_ERROR = AnalysisFailureReason.WORKFLOW_ERROR.value


async def mark_analysis_failed(analysis_id: UUID | str, reason: str) -> bool:
    analysis_uuid = (
        analysis_id if isinstance(analysis_id, UUID) else UUID(analysis_id))
    async with SessionLocal() as session:
        repo = AnalysisRepository(session)
        ticker = await repo.set_status_and_reason(
            analysis_uuid, WorkflowStatus.FAILED.value, reason,
            only_if_active=True)
        owner_id, portfolio_id = (
            await repo.get_owner_fields(analysis_uuid)
            if ticker is not None else (None, None))
        await session.commit()
    if ticker is None:
        return False
    record_analysis_failed(reason)
    logger.warning(
        "analysis_marked_failed", analysis_id=str(analysis_uuid),
        ticker=ticker, failure_reason=reason)
    await publish_event(
        EventName.ANALYSIS_FAILED,
        analysis_event_payload(
            analysis_id=analysis_uuid, user_id=owner_id, ticker=ticker,
            portfolio_id=portfolio_id, failure_reason=reason,
            include_failure_reason=True))
    return True


async def _run_and_persist(
        *, analysis_id: str, user_id: str, portfolio_id: str | None,
        ticker: str, analysis_type: AnalysisType,
        requested_agents: list[AgentRole] | None) -> None:
    analysis_uuid = UUID(analysis_id)
    holdings_count, held_quantity = await _resolve_holdings_context(
        portfolio_id, ticker)
    prior = await _resolve_prior_recommendation(portfolio_id, ticker)
    position_weight_pct = await _resolve_position_weight(portfolio_id, ticker)

    state = AnalysisState(
        analysis_id=analysis_id, user_id=user_id, portfolio_id=portfolio_id,
        ticker=ticker, analysis_type=analysis_type,
        requested_agents=requested_agents, holdings_count=holdings_count,
        held_quantity=held_quantity, position_weight_pct=position_weight_pct,
        prior_recommendation=prior, pending_recommendation_context=(
            prior.judge_context_line(ticker) if prior is not None else None))

    graph = build_analysis_graph()
    result = await graph.ainvoke(
        state, config={"recursion_limit": _GRAPH_RECURSION_LIMIT})

    final_rec: dict[str, Any] | None = result["final_recommendation"]
    no_verdict: str = (
        result.get("no_verdict_reason")
        or AnalysisFailureReason.JUDGE_UNAVAILABLE.value)
    superseded: list[TierInvalidation] = []
    recommendation = None

    async with SessionLocal() as session:
        catalog = AgentCatalogRepository(session)
        persist = AnalysisPersistenceRepository(session)
        agent_id_by_role: dict[AgentRole, UUID] = {}
        archivable_outputs: dict[AgentRole, AgentOutput] = {}

        for role, output in result["agent_outputs"].items():
            agent = await catalog.get_or_create(role.value)
            agent_id_by_role[role] = agent.id
            await session.commit()
            now = datetime.now(UTC)
            agent_started_at = output.started_at or now
            agent_completed_at = output.completed_at or now
            duration_ms = (
                int((agent_completed_at - agent_started_at).total_seconds() *
                    1000) if output.started_at is not None
                and output.completed_at is not None else None)
            await persist.save_agent_run(
                analysis_id=analysis_uuid, agent_id=agent.id,
                status=output.status.value, started_at=agent_started_at,
                completed_at=agent_completed_at, duration_ms=duration_ms,
                score=output.score, summary=output.summary, output={
                    **output.raw_output, "is_well_supported": output.
                    is_well_supported
                })
            if (is_archivable_role(role)
                    and output.status == AgentStatus.COMPLETED):
                archivable_outputs[role] = output

        transcript = result["debate"].transcript_arguments()
        dropped = len(result["debate"].arguments) - len(transcript)
        if dropped:
            logger.info(
                "analysis_workflow.replaced_side_placeholders_dropped",
                count=dropped)
        if transcript:
            await persist.save_debate_session(
                analysis_id=analysis_uuid, arguments=[{
                    "round_number": arg.round_number,
                    "side": arg.side,
                    "content": arg.argument,
                    "citations": arg.citations
                } for arg in transcript])

        for verdict in result["reflection_history"]:
            await persist.save_reflection(
                analysis_id=analysis_uuid, round_number=verdict.round_number,
                verified=verdict.verified,
                issues=[*verdict.issues,
                        verdict.review_meta_line()], retry_agents={
                            k.value: v
                            for k, v in verdict.retry_agents.items()
                        })

        flagged_roles, last_round_flagged_roles = flagged_role_sets(
            result["reflection_history"])

        async def _write_agent_learning() -> None:
            if isolation_skips(
                    "procedural_memory_and_archival_write",
                    analysis_id=analysis_id):
                return
            for role, output in result["agent_outputs"].items():
                if role in flagged_roles:
                    logger.info(
                        "analysis_workflow.memory_write_skipped_flagged",
                        analysis_id=analysis_id, role=role.value)
                    continue
                raw = (
                    output.raw_output
                    if isinstance(output.raw_output, dict) else {})
                raw_warnings = raw.get("verification_warnings")
                await maybe_remember_trajectory(
                    agent_id=agent_id_by_role[role],
                    user_id=owner_uuid(user_id), ticker=ticker,
                    score=output.score, final_answer=output.summary,
                    scratchpad=raw.get("scratchpad", []),
                    is_well_supported=output.is_well_supported,
                    hit_iteration_cap=output.hit_iteration_cap,
                    verification_warnings=([str(w) for w in raw_warnings
                                            ] if isinstance(
                                                raw_warnings, list) else None),
                    no_evidence=bool(raw.get("no_evidence")))

            for role, output in archivable_outputs.items():
                if role in last_round_flagged_roles:
                    logger.info(
                        "analysis_workflow.archival_skipped_flagged",
                        analysis_id=analysis_id, role=role.value)
                    continue
                try:
                    warnings = (
                        output.raw_output.get("verification_warnings")
                        if isinstance(output.raw_output, dict) else None)
                    await archive_agent_report(
                        analysis_id=analysis_uuid, role=role, ticker=ticker,
                        content=output.summary,
                        hit_iteration_cap=output.hit_iteration_cap,
                        verification_warnings=warnings,
                        is_well_supported=output.is_well_supported,
                        recycled_share=_archival_recycled_share(output))
                except Exception:
                    logger.exception(
                        "analysis_workflow.archival_failed",
                        analysis_id=analysis_id, role=role.value)

        learning_pending = False
        if final_rec is None:
            await session.commit()
            logger.info(
                "analysis_workflow.learning_skipped_no_verdict",
                analysis_id=analysis_id, reason=no_verdict)
        else:
            await session.commit()
            recommendation = await _save_recommendation_and_tiers(
                session, persist, analysis_id=analysis_id,
                analysis_uuid=analysis_uuid, ticker=ticker,
                portfolio_id=portfolio_id, final_rec=final_rec,
                closed_out=superseded)
            if not isolation_skips("episodic_memory_write",
                                   analysis_id=analysis_id):
                await EpisodicMemoryRepository(session).create(
                    user_id=UUID(user_id) if user_id else None,
                    analysis_id=analysis_uuid, ticker=ticker,
                    recommendation=final_rec["action"],
                    confidence=final_rec["confidence"],
                    summary=final_rec["reasoning"])

            completed = await AnalysisRepository(
                session).set_status_and_reason(
                    analysis_uuid, WorkflowStatus.COMPLETED.value, None,
                    only_if_active=True)
            if completed is None:
                await session.rollback()
                logger.warning(
                    "analysis_workflow.completion_discarded_not_active",
                    analysis_id=analysis_id, ticker=ticker)
                return
            await session.commit()
            record_analysis_completed()
            learning_pending = True

    if final_rec is None:
        await mark_analysis_failed(analysis_uuid, no_verdict)
        return

    try:
        await publish_event(
            EventName.ANALYSIS_COMPLETED,
            analysis_event_payload(
                analysis_id=analysis_id, user_id=user_id, ticker=ticker,
                portfolio_id=portfolio_id))
        if recommendation is not None and final_rec is not None:
            await publish_event(
                EventName.RECOMMENDATION_READY,
                recommendation_ready_payload(
                    recommendation_id=recommendation.id,
                    analysis_id=analysis_id, user_id=user_id, ticker=ticker,
                    action=final_rec["action"],
                    confidence=final_rec["confidence"]))
        for item in superseded:
            await publish_events(
                tier_cancelled_events(
                    item.tiers, reason=TIER_CANCEL_REASON_SUPERSEDED))
    finally:
        if learning_pending:
            try:
                await _write_agent_learning()
            except Exception:
                logger.exception(
                    "analysis_workflow.learning_write_failed",
                    analysis_id=analysis_id)


async def _save_recommendation_and_tiers(
        session: AsyncSession, persist: AnalysisPersistenceRepository, *,
        analysis_id: str, analysis_uuid: UUID, ticker: str,
        portfolio_id: str | None, final_rec: dict[str, Any],
        closed_out: list[TierInvalidation] | None = None) -> Recommendation:
    action = RecommendationAction(final_rec["action"])
    sizing = None
    keep_note: BudgetBlockedNote | None = None
    notes: list[str] = []
    tier_repo = RecommendationTierRepository(session)
    portfolio_uuid = UUID(portfolio_id) if portfolio_id else None

    if action != RecommendationAction.HOLD and portfolio_uuid is not None:
        try:
            if isolation_skips("committed_buy_cash_read",
                               analysis_id=analysis_id):
                holders = []
            else:
                holders = await tier_repo.committed_buy_holders(
                    portfolio_id=portfolio_uuid, exclude_ticker=ticker)
                await session.commit()
            sizing, sizing_note = await generate_position_sizing(
                ticker=ticker, action=action,
                confidence=final_rec["confidence"],
                reasoning=final_rec["reasoning"], portfolio_id=portfolio_id
                or "", committed_buy_cash=sum(
                    (h.amount for h in holders), Decimal(0)))
            if sizing_note:
                notes.append(sizing_note)
            if isinstance(sizing_note, BudgetBlockedNote) and holders:
                notes.append(
                    "The committed cash is held by open BUY tiers on: " +
                    ", ".join(dict.fromkeys(
                        h.public_label() for h in holders)) + ".")
            if (action == RecommendationAction.BUY and sizing is None
                    and isinstance(sizing_note, BudgetBlockedNote)):
                keep_note = sizing_note
        except Exception:
            logger.exception(
                "analysis_workflow.position_sizing_failed",
                analysis_id=analysis_id)
            notes.append(
                "No entry/exit tiers were generated for this recommendation "
                "-- "
                "position sizing failed unexpectedly; see logs for this "
                "analysis.")

    if portfolio_uuid is not None and isolation_skips(
            "earlier_tiers_supersede", analysis_id=analysis_id):
        pass
    elif portfolio_uuid is not None and keep_note is not None:
        kept = await tier_repo.unfilled_buy_tier_count(
            portfolio_id=portfolio_uuid, ticker=ticker)
        if kept:
            logger.info(
                "analysis_workflow.earlier_tiers_kept_budget_blocked",
                analysis_id=analysis_id, ticker=ticker, count=kept)
            notes.append(
                f"Kept {kept} earlier unfilled BUY tier(s) for {ticker} (not "
                "superseded): this BUY produced no tiers "
                f"{keep_note.reason_text}.")
    elif portfolio_uuid is not None:
        closed = (
            await tier_repo.invalidate_unfilled_tiers_for_new_recommendation(
                portfolio_id=portfolio_uuid, ticker=ticker, new_action=action,
                new_prices=([t.target_price
                             for t in sizing.tiers] if sizing else [])))
        for item in closed:
            logger.info(
                "analysis_workflow.earlier_tiers_superseded",
                analysis_id=analysis_id, ticker=ticker,
                superseded_recommendation_id=str(item.recommendation_id),
                side=item.side, count=item.count, reason=item.reason)
            notes.append(item.note())
        if closed_out is not None:
            closed_out.extend(closed)

    recommendation = await persist.save_recommendation(
        analysis_id=analysis_uuid, recommendation=final_rec["action"],
        confidence=final_rec["confidence"], reasoning=final_rec["reasoning"],
        confidence_adjustment_note=final_rec.get("confidence_adjustment_note"),
        debate_converged=final_rec["debate_converged"],
        tier_generation_note=" ".join(notes) if notes else None,
        judge_leaning=final_rec.get("judge_leaning"),
        judge_confidence=final_rec.get("judge_confidence"))
    if sizing is not None:
        await persist.save_recommendation_tiers(
            recommendation_id=recommendation.id,
            side=tier_side_for_action(action).value,
            window_days=sizing.window_days,
            tiers=[(t.target_price, t.quantity) for t in sizing.tiers])
    return recommendation
