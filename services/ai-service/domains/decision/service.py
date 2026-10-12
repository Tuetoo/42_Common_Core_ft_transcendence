import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
import structlog
from ai.llm.base import LLMError, LLMMessage, LLMTier
from ai.llm.json_fence import strip_json_code_fence
from ai.llm.router import LLMRouter
from ai_events import (TIER_CANCEL_REASON_DECLINED,
                       TIER_CANCEL_REASON_SUPERSEDED, TierEventInfo,
                       decision_event_payload, tier_cancelled_events)
from deps import ensure_analysis_access, ensure_portfolio_owner
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from domains.analysis.model import Analysis, Recommendation
from domains.analysis.repository import (AnalysisRepository,
                                         RecommendationTierRepository)
from domains.decision.model import DecisionOutcome, InvestmentDecision
from domains.decision.repository import (DecisionOutcomeRepository,
                                         DecisionReviewRepository,
                                         InvestmentDecisionRepository)
from domains.decision.schema import (DecisionOutcomeRead, DecisionReviewRead,
                                     InvestmentDecisionCreate,
                                     InvestmentDecisionRead)
from shared.enums import RecommendationAction, TierSide, TierStatus
from shared.events import EventName
from shared.exceptions import (ConflictError, NotFoundError,
                               ServiceUnavailableError, ValidationAppError)
from shared.schemas import PaginatedResponse, PaginationParams
from shared.security import CurrentUser

logger = structlog.get_logger(__name__)

REVIEW_RETRY_AFTER_SECONDS = 60


class ReviewUnavailableError(ServiceUnavailableError):
    code = "review_unavailable"
    retry_after_seconds = REVIEW_RETRY_AFTER_SECONDS


_REVIEW_SYSTEM_PROMPT = (
    "You are a calm, honest investment coach reviewing one past decision "
    "with the user. You will be given: the AI's original recommendation "
    "and its reasoning, what the user actually did, and the realized "
    "outcome versus a benchmark. Write a short, plain-language review: "
    "what happened, whether the outcome supports or contradicts the "
    "original reasoning, and one concrete, specific lesson for next time "
    "-- not generic advice like 'diversify' unless the numbers actually "
    "support that being the specific lesson here. If the outcome was "
    "clearly a miss (underperformed meaningfully, or the original call was "
    "wrong), say so plainly -- do not soften it into 'sharper than "
    "expected' or similar hedging language that avoids stating the call "
    "didn't work out. Respond with ONLY a "
    'JSON object: {"review_text": "...", "lessons_learned": "..."}. '
    "No markdown, no preamble.")


class DecisionService:

    def __init__(self, session: AsyncSession):
        self.session = session
        self.decisions = InvestmentDecisionRepository(session)
        self.outcomes = DecisionOutcomeRepository(session)
        self.reviews = DecisionReviewRepository(session)
        self.pending_events: list[tuple[EventName, dict[str, Any]]] = []

    async def record_decision(
            self, *, user: CurrentUser, analysis_id: UUID,
            data: InvestmentDecisionCreate) -> InvestmentDecisionRead:
        analysis = await AnalysisRepository(self.session).get_by_id(
            analysis_id, with_details=True)
        await ensure_analysis_access(user, analysis)
        if analysis.portfolio_id is not None:
            await ensure_portfolio_owner(user, analysis.portfolio_id)
        if (data.portfolio_id is not None
                and data.portfolio_id != analysis.portfolio_id):
            await ensure_portfolio_owner(user, data.portfolio_id)
        user_id = user.user_id
        if not analysis.recommendations:
            raise ValidationAppError(
                f"Analysis {analysis_id} has no recommendation yet -- "
                "cannot record a decision against it")
        recommendation = analysis.recommendations[-1]

        existing = await self.decisions.get_for_user_and_recommendations(
            user_id=user_id,
            recommendation_ids=[r.id for r in analysis.recommendations])
        previous_action = (
            existing.action_taken if existing is not None else None)
        if data.action_taken == "followed":
            if previous_action != "followed":
                superseded = await self._accept_recommendation_tiers(
                    recommendation, analysis)
                self.pending_events.extend(
                    tier_cancelled_events(
                        superseded, reason=TIER_CANCEL_REASON_SUPERSEDED))
        elif data.action_taken == "declined" or (data.action_taken == "ignored"
                                                 and existing is not None):
            declined = await self._decline_recommendation_tiers(
                recommendation, analysis)
            self.pending_events.extend(
                tier_cancelled_events(
                    declined, reason=TIER_CANCEL_REASON_DECLINED))

        if existing is not None:
            existing.recommendation_id = recommendation.id
            existing.portfolio_id = data.portfolio_id
            existing.action_taken = data.action_taken
            existing.notes = data.notes
            existing.created_at = datetime.now(UTC)
            removed = await self.outcomes.delete_for_decision(existing.id)
            reviews_removed = await self.reviews.delete_for_decision(
                existing.id)
            await self.session.flush()
            logger.info(
                "decision.updated", decision_id=str(existing.id),
                previous_action=previous_action, action=data.action_taken,
                outcomes_removed=removed, reviews_removed=reviews_removed)
            self._queue_decision_recorded(
                existing.id, analysis, user_id, data.action_taken)
            return InvestmentDecisionRead.model_validate(existing)

        try:
            decision = await self.decisions.create(
                user_id=user_id, portfolio_id=data.portfolio_id,
                recommendation_id=recommendation.id, ticker=analysis.ticker,
                action_taken=data.action_taken, notes=data.notes)
        except IntegrityError as exc:
            raise ConflictError(
                "A decision for this analysis was recorded concurrently -- "
                "retry to update it.") from exc
        self._queue_decision_recorded(
            decision.id, analysis, user_id, data.action_taken)
        return InvestmentDecisionRead.model_validate(decision)

    def _queue_decision_recorded(
            self, decision_id: UUID, analysis: Analysis, user_id: UUID,
            action_taken: str) -> None:
        self.pending_events.append((
            EventName.DECISION_RECORDED,
            decision_event_payload(
                decision_id=decision_id, analysis_id=analysis.id,
                user_id=user_id, ticker=analysis.ticker,
                decision=action_taken)))

    async def _accept_recommendation_tiers(
            self, recommendation: Recommendation,
            analysis: Analysis) -> list[TierEventInfo]:
        if recommendation.window_expires_at is None:
            return []
        if recommendation.window_expires_at < datetime.now(UTC):
            raise ValidationAppError(
                "This recommendation's acceptance window has already "
                "expired -- nothing left to accept.")
        pending = (
            await self.session.execute(
                sql_text(
                    "SELECT side, target_price FROM ai.recommendation_tiers "
                    "WHERE recommendation_id = :rec_id AND status = :pending"),
                {
                    "rec_id": recommendation.id,
                    "pending": TierStatus.PENDING_ACCEPTANCE.value
                })).all()
        if not pending:
            raise ValidationAppError(
                "This recommendation has no tier still awaiting acceptance -- "
                "its tiers were already accepted, declined, superseded by a "
                "newer recommendation, or expired. Nothing left to accept.")
        await self.session.execute(
            sql_text(
                "UPDATE ai.recommendations SET accepted_at = :now "
                "WHERE id = :id"), {
                    "now": datetime.now(UTC),
                    "id": recommendation.id
                })
        await self.session.execute(
            sql_text(
                "UPDATE ai.recommendation_tiers SET status = :new_status "
                "WHERE recommendation_id = :rec_id AND status = :old_status"),
            {
                "new_status": TierStatus.ACCEPTED_AWAITING_TRIGGER.value,
                "rec_id": recommendation.id,
                "old_status": TierStatus.PENDING_ACCEPTANCE.value
            })

        superseded: list[TierEventInfo] = []
        if analysis.portfolio_id is not None:
            accepted_side = str(pending[0][0])
            closed = await RecommendationTierRepository(
                self.session).invalidate_unfilled_tiers_for_new_recommendation(
                    portfolio_id=analysis.portfolio_id, ticker=analysis.ticker,
                    new_action=(
                        RecommendationAction.SELL if accepted_side
                        == TierSide.SELL.value else RecommendationAction.BUY),
                    new_prices=[Decimal(row[1]) for row in pending],
                    exclude_recommendation_id=recommendation.id,
                    source="an accepted")
            for item in closed:
                logger.info(
                    "decision.accept_superseded_tiers",
                    accepted_recommendation_id=str(recommendation.id),
                    superseded_recommendation_id=str(item.recommendation_id),
                    side=item.side, count=item.count, reason=item.reason)
                superseded.extend(item.tiers)

        await self.session.flush()
        return superseded

    async def _decline_recommendation_tiers(
            self, recommendation: Recommendation,
            analysis: Analysis) -> list[TierEventInfo]:
        rows = (
            await self.session.execute(
                sql_text(
                    "UPDATE ai.recommendation_tiers SET status = :new_status "
                    "WHERE recommendation_id = :rec_id "
                    "AND status IN (:pending, :accepted) "
                    "RETURNING id, side, quantity, target_price"), {
                        "new_status": TierStatus.DECLINED.value,
                        "rec_id": recommendation.id,
                        "pending": TierStatus.PENDING_ACCEPTANCE.value,
                        "accepted": TierStatus.ACCEPTED_AWAITING_TRIGGER.value
                    })).all()
        await self.session.flush()
        return [
            TierEventInfo(
                tier_id=row[0], analysis_id=analysis.id,
                user_id=analysis.user_id, portfolio_id=analysis.portfolio_id,
                ticker=analysis.ticker, side=str(row[1]),
                quantity=Decimal(row[2]), price=Decimal(row[3]))
            for row in rows
        ]

    async def list_my_decisions(
        self, user_id: UUID, pagination: PaginationParams
    ) -> PaginatedResponse[InvestmentDecisionRead]:
        rows, total = await self.decisions.list_for_user(user_id, pagination)
        return PaginatedResponse(
            items=[InvestmentDecisionRead.model_validate(r) for r in rows],
            total=total, page=pagination.page, page_size=pagination.page_size)

    async def _get_own_decision(
            self, decision_id: UUID, user_id: UUID) -> InvestmentDecision:
        decision = await self.decisions.get_by_id(decision_id)
        if decision.user_id != user_id:
            raise NotFoundError(f"Decision {decision_id} not found")
        return decision

    async def get_outcome(
            self, decision_id: UUID, *, user_id: UUID) -> DecisionOutcomeRead:
        await self._get_own_decision(decision_id, user_id)
        outcome = await self.outcomes.get_latest_for_decision(decision_id)
        if outcome is None:
            raise NotFoundError(
                f"Decision {decision_id} exists but hasn't been evaluated "
                "yet -- ai/workflows/review_workflow.py evaluates decisions "
                "once enough time has elapsed")
        return DecisionOutcomeRead.model_validate(outcome)

    async def generate_review(
            self, decision_id: UUID, *, user_id: UUID,
            llm_router: LLMRouter | None = None) -> DecisionReviewRead:
        decision = await self._get_own_decision(decision_id, user_id)
        outcome = await self.outcomes.get_latest_for_decision(decision_id)
        if outcome is None:
            raise ValidationAppError(
                f"Decision {decision_id} has no evaluated outcome yet -- "
                "cannot generate a review until "
                "ai/workflows/review_workflow.py "
                "has scored it")
        recommendation = await self.session.get(
            Recommendation, decision.recommendation_id)
        if recommendation is None:
            raise NotFoundError(
                f"Recommendation {decision.recommendation_id} referenced by "
                f"decision {decision_id} no longer exists")

        router = llm_router or LLMRouter()
        prompt = self._build_review_prompt(decision, recommendation, outcome)
        try:
            response = await router.complete(
                LLMTier.CHEAP, [
                    LLMMessage(role="system", content=_REVIEW_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=prompt)
                ])
            parsed = json.loads(strip_json_code_fence(response.content))
            review_text = parsed.get("review_text")
            lessons_learned = parsed.get("lessons_learned")
            if not isinstance(review_text, str) or not review_text.strip():
                raise ValueError("the answer has no review_text")
            if not isinstance(lessons_learned, str):
                lessons_learned = None
        except (LLMError, json.JSONDecodeError, AttributeError,
                ValueError) as exc:
            logger.warning(
                "decision_review.generation_failed",
                decision_id=str(decision_id), error_type=type(exc).__name__,
                error=str(exc)[:200])
            raise ReviewUnavailableError(
                "Review generation is temporarily unavailable; try again "
                "shortly.") from exc

        await self.reviews.delete_for_decision(decision_id)
        review = await self.reviews.create(
            decision_id=decision_id, review_text=review_text,
            lessons_learned=lessons_learned)
        return DecisionReviewRead.model_validate(review)

    async def get_review(
            self, decision_id: UUID, *, user_id: UUID) -> DecisionReviewRead:
        await self._get_own_decision(decision_id, user_id)
        review = await self.reviews.get_by_decision_id(decision_id)
        if review is None:
            raise NotFoundError(
                f"No review generated yet for decision {decision_id} -- "
                "POST /api/v1/decisions/{decision_id}/review to generate one")
        return DecisionReviewRead.model_validate(review)

    @staticmethod
    def _build_review_prompt(
            decision: InvestmentDecision, recommendation: Recommendation,
            outcome: DecisionOutcome) -> str:
        outperformed_text = (
            "outperformed" if outcome.outperformed else (
                "underperformed"
                if outcome.outperformed is False else "vs. (unknown)"))
        return (
            f"Ticker: {decision.ticker}\n"
            f"AI recommendation: {recommendation.recommendation} "
            f"(confidence {recommendation.confidence})\n"
            f"AI reasoning: {recommendation.reasoning}\n"
            f"User's action: {decision.action_taken}" +
            (f" -- user's note: {decision.notes}" if decision.notes else "") +
            f"\nRealized outcome ({outcome.evaluation_days} days later): "
            f"strategy return {outcome.strategy_return}, "
            f"benchmark return {outcome.benchmark_return}, "
            f"{outperformed_text} the benchmark.")
