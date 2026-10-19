from collections.abc import Sequence
from functools import partial
from typing import Any
from uuid import UUID
import structlog
from ai.knowledge.archival import delete_archived_report
from config import settings
from database import SessionLocal
from sqlalchemy import Select, delete, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession
from domains.analysis.model import (AgentResult, AgentRun, Analysis,
                                    DebateArgument, DebateSession,
                                    Recommendation, RecommendationTier,
                                    Reflection)
from domains.analytics.model import RecommendationMetric
from domains.backtest.model import Backtest
from domains.backtest.repository import BacktestRepository
from domains.decision.model import (DecisionOutcome, DecisionReview,
                                    InvestmentDecision)
from domains.knowledge.model import AgentReport
from domains.memory.model import AgentMemory, EpisodicMemory
from shared.deleted_users import (SweepResult, log_sweep, run_cleanup_steps,
                                  run_deleted_user_sweep)

logger = structlog.get_logger(__name__)


def _analyses(user_id: UUID) -> Select[tuple[UUID]]:
    return select(Analysis.id).where(Analysis.user_id == user_id)


def _recommendations(user_id: UUID) -> Select[tuple[UUID]]:
    return select(Recommendation.id).where(
        Recommendation.analysis_id.in_(_analyses(user_id)))


async def _execute_all(session: AsyncSession, *statements: Any) -> int:
    last_count = 0
    for statement in statements:
        result = await session.execute(statement)
        last_count = int(getattr(result, "rowcount", 0) or 0)
    await session.commit()
    return last_count


async def _delete_reports(session: AsyncSession, user_id: UUID) -> int:
    report_ids = (
        await session.execute(
            select(AgentReport.id).where(
                AgentReport.analysis_id.in_(_analyses(user_id))))
    ).scalars().all()
    for report_id in report_ids:
        await delete_archived_report(session, report_id)
    await session.commit()
    return len(report_ids)


async def _delete_decisions(session: AsyncSession, user_id: UUID) -> int:
    decisions = select(InvestmentDecision.id).where(
        or_(
            InvestmentDecision.user_id == user_id,
            InvestmentDecision.recommendation_id.in_(
                _recommendations(user_id))))
    return await _execute_all(
        session,
        delete(DecisionOutcome).where(
            DecisionOutcome.decision_id.in_(decisions)),
        delete(DecisionReview).where(
            DecisionReview.decision_id.in_(decisions)),
        delete(InvestmentDecision).where(InvestmentDecision.id.in_(decisions)))


async def _delete_analyses(session: AsyncSession, user_id: UUID) -> int:
    analyses = _analyses(user_id)
    recommendations = _recommendations(user_id)
    return await _execute_all(
        session,
        delete(RecommendationMetric).where(
            RecommendationMetric.recommendation_id.in_(recommendations)),
        delete(RecommendationTier).where(
            RecommendationTier.recommendation_id.in_(recommendations)),
        delete(Recommendation).where(Recommendation.id.in_(recommendations)),
        delete(Reflection).where(Reflection.analysis_id.in_(analyses)),
        delete(AgentResult).where(
            AgentResult.agent_run_id.in_(
                select(AgentRun.id).where(
                    AgentRun.analysis_id.in_(analyses)))),
        delete(AgentRun).where(AgentRun.analysis_id.in_(analyses)),
        delete(DebateArgument).where(
            DebateArgument.debate_session_id.in_(
                select(DebateSession.id).where(
                    DebateSession.analysis_id.in_(analyses)))),
        delete(DebateSession).where(DebateSession.analysis_id.in_(analyses)),
        delete(EpisodicMemory).where(
            or_(
                EpisodicMemory.user_id == user_id,
                EpisodicMemory.analysis_id.in_(analyses))),
        delete(Analysis).where(Analysis.user_id == user_id))


async def _delete_backtests(session: AsyncSession, user_id: UUID) -> int:
    repository = BacktestRepository(session)
    backtests = (
        await session.execute(
            select(Backtest).where(Backtest.user_id == user_id))
    ).scalars().all()
    for backtest in backtests:
        await repository.delete(backtest)
    await session.commit()
    return len(backtests)


async def _delete_agent_memories(session: AsyncSession, user_id: UUID) -> int:
    return await _execute_all(
        session, delete(AgentMemory).where(AgentMemory.user_id == user_id))


async def delete_user_data(
        user_id: UUID, *, strict: bool = False) -> dict[str, int]:
    return await run_cleanup_steps(
        SessionLocal, "ai_user", user_id, {
            "archived_reports": partial(_delete_reports, user_id=user_id),
            "decisions": partial(_delete_decisions, user_id=user_id),
            "analyses": partial(_delete_analyses, user_id=user_id),
            "backtests": partial(_delete_backtests, user_id=user_id),
            "agent_memories": partial(_delete_agent_memories, user_id=user_id)
        }, strict=strict)


async def users_with_data() -> set[UUID]:
    async with SessionLocal() as session:
        user_ids: Sequence[UUID | None] = (
            await session.execute(
                union(
                    select(Analysis.user_id),
                    select(InvestmentDecision.user_id),
                    select(Backtest.user_id), select(EpisodicMemory.user_id),
                    select(AgentMemory.user_id)))).scalars().all()
    return {user_id for user_id in user_ids if user_id is not None}


async def sweep_deleted_users() -> SweepResult:
    try:
        candidates = await users_with_data()
    except Exception:
        logger.exception("deleted_user_sweep_candidates_failed")
        return log_sweep(
            "user",
            SweepResult(
                candidates=0, deleted=0, cleaned=0, failed=0,
                lookup_failed=True))
    return await run_deleted_user_sweep(
        settings, candidates, partial(delete_user_data, strict=True))
