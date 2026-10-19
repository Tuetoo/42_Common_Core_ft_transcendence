from uuid import UUID
from ai_events import publish_events
from config import settings
from database import get_db
from deps import get_analysis_writer, get_current_user
from fastapi import APIRouter, BackgroundTasks, Depends, Query
from rate_limit import UserRateLimitedError, check_user_rate_limit
from sqlalchemy.ext.asyncio import AsyncSession
from domains.decision.schema import (DecisionOutcomeRead, DecisionReviewRead,
                                     InvestmentDecisionRead)
from domains.decision.service import DecisionService
from shared.schemas import (MAX_PAGE, APIResponse, PaginatedResponse,
                            PaginationParams)
from shared.security import CurrentUser

router = APIRouter(tags=["decisions"])


@router.get("", response_model=PaginatedResponse[InvestmentDecisionRead])
async def list_my_decisions(
        page: int = Query(default=1, ge=1, le=MAX_PAGE),
        page_size: int = Query(default=20, ge=1, le=100),
        user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db, scope="function")
) -> PaginatedResponse[InvestmentDecisionRead]:
    return await DecisionService(db).list_my_decisions(
        user.user_id, PaginationParams(page=page, page_size=page_size))


@router.get(
    "/{decision_id}/outcome", response_model=APIResponse[DecisionOutcomeRead])
async def get_decision_outcome(
    decision_id: UUID, user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[DecisionOutcomeRead]:
    result = await DecisionService(db).get_outcome(
        decision_id, user_id=user.user_id)
    return APIResponse(data=result)


@router.post(
    "/{decision_id}/review", status_code=201,
    response_model=APIResponse[DecisionReviewRead], description=(
        "Generate a new review of the decision. Every call generates a new "
        "review and replaces the stored one. When the model call fails the "
        "answer is 503 with Retry-After and nothing is stored. Limited per "
        "user (DECISION_REVIEW_RATE_LIMIT_PER_MINUTE / _PER_DAY); over the "
        "limit the answer is 429 with Retry-After."))
async def generate_decision_review(
    decision_id: UUID, background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_analysis_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[DecisionReviewRead]:
    try:
        await check_user_rate_limit(
            "decision_review", str(user.user_id),
            per_minute=settings.DECISION_REVIEW_RATE_LIMIT_PER_MINUTE,
            per_day=settings.DECISION_REVIEW_RATE_LIMIT_PER_DAY,
            noun="review")
    except UserRateLimitedError as exc:
        raise exc.http_error() from exc
    service = DecisionService(db)
    result = await service.generate_review(decision_id, user_id=user.user_id)
    background_tasks.add_task(publish_events, list(service.pending_events))
    return APIResponse(data=result)


@router.get(
    "/{decision_id}/review", response_model=APIResponse[DecisionReviewRead])
async def get_decision_review(
    decision_id: UUID, user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[DecisionReviewRead]:
    result = await DecisionService(db).get_review(
        decision_id, user_id=user.user_id)
    return APIResponse(data=result)
