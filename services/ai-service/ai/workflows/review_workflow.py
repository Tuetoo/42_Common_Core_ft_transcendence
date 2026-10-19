import math
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple
from uuid import UUID, uuid4
import structlog
from ai.providers.registry import get_market_data_provider
from ai.tools.indicators import total_return
from ai_events import decision_outcome_event_payload, publish_event
from config import settings
from database import SessionLocal
from sqlalchemy.dialects.postgresql import insert as pg_insert
from domains.analysis.repository import AnalysisRepository
from domains.analytics.model import RecommendationMetric
from domains.analytics.repository import AnalyticsRepository
from domains.decision.repository import (DecisionOutcomeRepository,
                                         InvestmentDecisionRepository)
from domains.memory.repository import EpisodicMemoryRepository
from shared.enums import RecommendationAction
from shared.events import EventName

logger = structlog.get_logger(__name__)

_DECISION_EVALUATION_WINDOWS = [7, 30, 90]
_DEFAULT_BENCHMARK = "SPY"
_HOLD_FLAT_BAND = 0.02


class StepCounts(NamedTuple):
    done: int
    attempted: int
    failed: int


async def _realized_return(
        ticker: str, since: date,
        window_days: int | None = None) -> float | None:
    end = datetime.now(UTC).date()
    if window_days is not None:
        end = min(end, since + timedelta(days=window_days))
    provider = get_market_data_provider()
    bars = await provider.get_price_history_range(ticker, start=since, end=end)
    if len(bars) < 2:
        return None
    closes = [
        float(c) for c in (bar.close for bar in bars) if math.isfinite(c)
    ]
    if len(closes) < 2:
        return None
    return total_return(closes)


def _directional_return(action: str, ticker_return: float) -> float:
    if action == RecommendationAction.SELL.value:
        return -ticker_return
    return ticker_return


def _score_recommendation(action: str, realized_return: float) -> float:
    if action == RecommendationAction.BUY.value:
        return 1.0 if realized_return > 0 else 0.0
    if action == RecommendationAction.SELL.value:
        return 1.0 if realized_return < 0 else 0.0
    return 1.0 if abs(realized_return) <= _HOLD_FLAT_BAND else 0.0


async def _score_one_recommendation(
        recommendation_id: UUID, analysis_id: UUID,
        action: str) -> bool:
    async with SessionLocal() as session:
        analysis = await AnalysisRepository(session).get_by_id(analysis_id)
        ticker = analysis.ticker
        analyzed_on = analysis.created_at.date()

    realized_return = await _realized_return(
        ticker, analyzed_on, settings.AI_ACCURACY_EVALUATION_DAYS)
    if realized_return is None:
        logger.warning(
            "review.no_price_data", ticker=ticker,
            recommendation_id=str(recommendation_id))
        return False

    score = _score_recommendation(action, realized_return)
    async with SessionLocal() as session:
        await session.execute(
            pg_insert(RecommendationMetric).values(
                id=uuid4(), recommendation_id=recommendation_id,
                accuracy_score=score).on_conflict_do_nothing(
                    index_elements=["recommendation_id"]))
        memory = await EpisodicMemoryRepository(session).get_by_analysis_id(
            analysis_id)
        if memory is not None:
            await EpisodicMemoryRepository(session).update_outcome(
                memory, "correct" if score >= 1.0 else "incorrect",
                outcome_return=realized_return)
        await session.commit()
    return True


async def review_ai_accuracy() -> StepCounts:
    async with SessionLocal() as session:
        pending = await AnalyticsRepository(
            session).list_recommendations_pending_metric(
                min_age_days=settings.AI_ACCURACY_EVALUATION_DAYS,
                max_age_days=(
                    settings.AI_ACCURACY_EVALUATION_DAYS
                    + settings.REVIEW_GIVE_UP_AFTER_DAYS))
        pending_items = [(rec.id, rec.analysis_id, rec.recommendation)
                         for rec in pending]

    scored = 0
    failed = 0
    for recommendation_id, analysis_id, action in pending_items:
        try:
            ok = await _score_one_recommendation(
                recommendation_id, analysis_id, action)
        except Exception:
            logger.exception(
                "review.recommendation_failed",
                recommendation_id=str(recommendation_id))
            ok = False
        if ok:
            scored += 1
        else:
            failed += 1

    logger.info("review.ai_accuracy_complete", scored=scored, failed=failed)
    return StepCounts(scored, len(pending_items), failed)


async def _evaluate_one_decision(
        decision_id: UUID, ticker: str, decided_on: date, action: str,
        window_days: int, *, user_id: UUID, analysis_id: UUID) -> bool | None:
    ticker_return = await _realized_return(ticker, decided_on, window_days)
    benchmark_return = await _realized_return(
        _DEFAULT_BENCHMARK, decided_on, window_days)
    if ticker_return is None or benchmark_return is None:
        logger.warning(
            "review.decision_no_price_data", decision_id=str(decision_id),
            ticker=ticker, ticker_missing=ticker_return is None,
            benchmark_missing=benchmark_return is None)
        return None
    strategy_return = _directional_return(action, ticker_return)
    alpha = strategy_return - benchmark_return
    outperformed = strategy_return > benchmark_return
    async with SessionLocal() as session:
        written = await DecisionOutcomeRepository(session).create_if_absent(
            decision_id=decision_id, evaluation_days=window_days,
            strategy_return=strategy_return, benchmark_return=benchmark_return,
            alpha=alpha, outperformed=outperformed)
        await session.commit()
    if not written:
        return False
    await publish_event(
        EventName.DECISION_OUTCOME_EVALUATED,
        decision_outcome_event_payload(
            decision_id=decision_id, analysis_id=analysis_id, user_id=user_id,
            ticker=ticker, evaluation_days=window_days, alpha=alpha,
            outperformed=outperformed))
    return True


async def review_decision_outcomes() -> StepCounts:
    total_evaluated = 0
    attempted = 0
    failed = 0
    for window_days in _DECISION_EVALUATION_WINDOWS:
        async with SessionLocal() as session:
            pending = await InvestmentDecisionRepository(
                session).list_pending_review(
                    min_age_days=window_days, max_age_days=(
                        window_days + settings.REVIEW_GIVE_UP_AFTER_DAYS))

        attempted += len(pending)
        for d in pending:
            try:
                written = await _evaluate_one_decision(
                    d.id, d.ticker, d.created_at.date(), d.action, window_days,
                    user_id=d.user_id, analysis_id=d.analysis_id)
            except Exception:
                logger.exception(
                    "review.decision_failed", decision_id=str(d.id),
                    window_days=window_days)
                written = None
            if written is None:
                failed += 1
            elif written:
                total_evaluated += 1

    logger.info(
        "review.decision_outcomes_complete", evaluated=total_evaluated,
        failed=failed)
    return StepCounts(total_evaluated, attempted, failed)


async def run_agent_metrics_rollup() -> int:
    async with SessionLocal() as session:
        agent_ids = await AnalyticsRepository(session).list_agent_ids()

    updated = 0
    for agent_id in agent_ids:
        async with SessionLocal() as session:
            repo = AnalyticsRepository(session)
            avg_score, success_rate = await repo.compute_agent_rollup(agent_id)
            if avg_score is None and success_rate is None:
                continue
            await repo.upsert_agent_metric(
                agent_id=agent_id, avg_score=avg_score,
                success_rate=success_rate)
            await session.commit()
        updated += 1

    logger.info("review.agent_metrics_rollup_complete", updated=updated)
    return updated


async def run_review_workflow() -> dict[str, Any]:
    accuracy = await review_ai_accuracy()
    decisions = await review_decision_outcomes()
    agents_updated = await run_agent_metrics_rollup()
    return {
        "recommendations_scored": accuracy.done,
        "recommendations_attempted": accuracy.attempted,
        "recommendations_failed": accuracy.failed,
        "decisions_evaluated": decisions.done,
        "decisions_attempted": decisions.attempted,
        "decisions_failed": decisions.failed,
        "agent_metrics_updated": agents_updated
    }
