from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from uuid import UUID, uuid4
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from domains.analysis.model import Recommendation
from domains.decision.model import (DecisionOutcome, DecisionReview,
                                    InvestmentDecision)
from shared.database import count_query
from shared.exceptions import NotFoundError
from shared.schemas import PaginationParams


class PendingDecision(NamedTuple):
    id: UUID
    ticker: str
    created_at: datetime
    user_id: UUID
    action: str
    analysis_id: UUID


class InvestmentDecisionRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
            self, *, user_id: UUID, portfolio_id: UUID | None,
            recommendation_id: UUID, ticker: str, action_taken: str,
            notes: str | None) -> InvestmentDecision:
        decision = InvestmentDecision(
            user_id=user_id, portfolio_id=portfolio_id,
            recommendation_id=recommendation_id, ticker=ticker.upper(),
            action_taken=action_taken, notes=notes)
        self.session.add(decision)
        await self.session.flush()
        return decision

    async def get_for_user_and_recommendations(
            self, *, user_id: UUID,
            recommendation_ids: list[UUID]) -> InvestmentDecision | None:
        if not recommendation_ids:
            return None
        stmt = (
            select(InvestmentDecision).where(
                InvestmentDecision.user_id == user_id).where(
                    InvestmentDecision.recommendation_id.in_(
                        recommendation_ids)).order_by(
                            InvestmentDecision.created_at.desc()).limit(
                                1).with_for_update())
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, decision_id: UUID) -> InvestmentDecision:
        decision = await self.session.get(InvestmentDecision, decision_id)
        if decision is None:
            raise NotFoundError(f"Investment decision {decision_id} not found")
        return decision

    async def list_for_user(
            self, user_id: UUID, pagination: PaginationParams
    ) -> tuple[list[InvestmentDecision], int]:
        stmt = (
            select(InvestmentDecision).where(
                InvestmentDecision.user_id == user_id).order_by(
                    InvestmentDecision.created_at.desc(),
                    InvestmentDecision.id.desc()))
        total = await count_query(self.session, stmt)
        stmt = stmt.offset(pagination.offset).limit(pagination.limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def list_pending_review(
            self, *, min_age_days: int, max_age_days: int,
            limit: int = 200) -> list[PendingDecision]:
        now = datetime.now(UTC)
        already_evaluated = select(DecisionOutcome.decision_id).where(
            DecisionOutcome.evaluation_days == min_age_days)
        stmt = (
            select(
                InvestmentDecision.id, InvestmentDecision.ticker,
                InvestmentDecision.created_at, InvestmentDecision.user_id,
                Recommendation.recommendation,
                Recommendation.analysis_id).join(
                    Recommendation, Recommendation.id ==
                    InvestmentDecision.recommendation_id).where(
                InvestmentDecision.created_at.between(
                    now - timedelta(days=max_age_days),
                    now - timedelta(days=min_age_days))).where(
                    InvestmentDecision.id.not_in(already_evaluated)).order_by(
                        InvestmentDecision.created_at).limit(limit))
        result = await self.session.execute(stmt)
        return [PendingDecision(*row) for row in result.all()]


class DecisionOutcomeRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def delete_for_decision(self, decision_id: UUID) -> int:
        result = await self.session.execute(
            delete(DecisionOutcome).where(
                DecisionOutcome.decision_id == decision_id))
        return int(getattr(result, "rowcount", 0) or 0)

    async def create_if_absent(
            self, *, decision_id: UUID, evaluation_days: int,
            strategy_return: float | None, benchmark_return: float | None,
            alpha: float | None, outperformed: bool | None) -> bool:
        stmt = (
            pg_insert(DecisionOutcome).values(
                id=uuid4(), decision_id=decision_id,
                evaluation_days=evaluation_days,
                strategy_return=strategy_return,
                benchmark_return=benchmark_return, alpha=alpha,
                outperformed=outperformed,
                evaluated_at=datetime.now(UTC)).on_conflict_do_nothing(
                    index_elements=["decision_id", "evaluation_days"
                                    ]).returning(DecisionOutcome.id))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def get_latest_for_decision(
            self, decision_id: UUID) -> DecisionOutcome | None:
        stmt = (
            select(DecisionOutcome).where(
                DecisionOutcome.decision_id == decision_id).order_by(
                    DecisionOutcome.evaluated_at.desc()).limit(1))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


class DecisionReviewRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
            self, *, decision_id: UUID, review_text: str | None,
            lessons_learned: str | None) -> DecisionReview:
        review = DecisionReview(
            decision_id=decision_id, review_text=review_text,
            lessons_learned=lessons_learned)
        self.session.add(review)
        await self.session.flush()
        return review

    async def delete_for_decision(self, decision_id: UUID) -> int:
        result = await self.session.execute(
            delete(DecisionReview).where(
                DecisionReview.decision_id == decision_id))
        return int(getattr(result, "rowcount", 0) or 0)

    async def get_by_decision_id(
            self, decision_id: UUID) -> DecisionReview | None:
        stmt = (
            select(DecisionReview).where(
                DecisionReview.decision_id == decision_id).order_by(
                    DecisionReview.created_at.desc()).limit(1))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
