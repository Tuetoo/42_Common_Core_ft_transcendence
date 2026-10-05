from uuid import UUID
import structlog
from ai_events import publish_events
from database import get_db
from deps import (ensure_analysis_access, ensure_portfolio_owner,
                  get_analysis_writer, get_current_user)
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from domains.analysis.model import Analysis
from domains.analysis.queue import (AnalysisJob, QueueFullError,
                                    analysis_queue, check_admission)
from domains.analysis.repository import AnalysisRepository
from domains.analysis.schema import (AnalysisCreate, AnalysisDetailRead,
                                     AnalysisRead, DebateRead,
                                     RecommendationRead,
                                     RecommendationTierRead, ReflectionRead)
from domains.analysis.service import (AnalysisService, analysis_detail_read,
                                      debate_read, recommendation_read,
                                      reflection_reads, tier_reads)
from domains.decision.schema import (InvestmentDecisionCreate,
                                     InvestmentDecisionRead)
from shared.schemas import (MAX_PAGE, APIResponse, PaginatedResponse,
                            PaginationParams)
from shared.security import CurrentUser

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["analyses"])


def _run_in_background(
        analysis_id: UUID, payload: AnalysisCreate,
        user_id: UUID | None = None) -> None:
    analysis_queue.enqueue(
        AnalysisJob(
            analysis_id=analysis_id, analysis_type=payload.analysis_type,
            requested_agents=payload.requested_agents, user_id=user_id))


async def create_and_enqueue(
        db: AsyncSession, *, user_id: UUID | None, payload: AnalysisCreate,
        enforce_user_cap: bool = False) -> AnalysisRead:
    try:
        await check_admission(
            db, user_id=user_id, enforce_user_cap=enforce_user_cap)
    except QueueFullError as exc:
        raise HTTPException(
            status_code=503, detail=str(exc),
            headers={"Retry-After": "60"}) from exc
    service = AnalysisService(db)
    analysis = await service.create_analysis(
        user_id=user_id, portfolio_id=payload.portfolio_id,
        ticker=payload.ticker)
    await db.commit()
    _run_in_background(analysis.id, payload, user_id)
    return analysis


async def _load_accessible_analysis(
        db: AsyncSession, analysis_id: UUID, user: CurrentUser) -> Analysis:
    analysis = await AnalysisRepository(db).get_by_id(
        analysis_id, with_details=True)
    await ensure_analysis_access(user, analysis)
    return analysis


@router.post(
    "", response_model=APIResponse[AnalysisRead], status_code=201,
    description=(
        "Start an analysis. Login required. The owner is always the caller (a "
        "body `user_id` is ignored), and `portfolio_id` must be a portfolio "
        "the "
        "caller may use."))
async def create_analysis(
    payload: AnalysisCreate, user: CurrentUser = Depends(get_analysis_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[AnalysisRead]:
    if payload.portfolio_id is not None:
        await ensure_portfolio_owner(user, payload.portfolio_id)
    analysis = await create_and_enqueue(
        db, user_id=user.user_id, payload=payload, enforce_user_cap=True)
    return APIResponse(data=analysis)


@router.get("", response_model=PaginatedResponse[AnalysisRead])
async def list_my_analyses(
        page: int = Query(default=1, ge=1, le=MAX_PAGE),
        page_size: int = Query(default=20, ge=1, le=100),
        user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db, scope="function")
) -> PaginatedResponse[AnalysisRead]:
    service = AnalysisService(db)
    return await service.list_analyses(
        user.user_id, PaginationParams(page=page, page_size=page_size))


@router.get("/{analysis_id}", response_model=APIResponse[AnalysisDetailRead])
async def get_analysis(
    analysis_id: UUID, user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[AnalysisDetailRead]:
    analysis = await _load_accessible_analysis(db, analysis_id, user)
    return APIResponse(data=analysis_detail_read(analysis))


@router.get(
    "/{analysis_id}/recommendation",
    response_model=APIResponse[RecommendationRead])
async def get_recommendation(
    analysis_id: UUID, user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[RecommendationRead]:
    analysis = await _load_accessible_analysis(db, analysis_id, user)
    return APIResponse(data=recommendation_read(analysis))


@router.get(
    "/{analysis_id}/tiers",
    response_model=APIResponse[list[RecommendationTierRead]], description=(
        'The tiers that "followed" accepts (and later executes), with their '
        "current status. Same access rule as the other analysis routes."))
async def get_tiers(
    analysis_id: UUID, user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[list[RecommendationTierRead]]:
    analysis = await _load_accessible_analysis(db, analysis_id, user)
    return APIResponse(data=tier_reads(analysis))


@router.get("/{analysis_id}/debate", response_model=APIResponse[DebateRead])
async def get_debate(
    analysis_id: UUID, user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[DebateRead]:
    analysis = await _load_accessible_analysis(db, analysis_id, user)
    return APIResponse(data=debate_read(analysis))


@router.get(
    "/{analysis_id}/reflection",
    response_model=APIResponse[list[ReflectionRead]])
async def get_reflection(
    analysis_id: UUID, user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[list[ReflectionRead]]:
    analysis = await _load_accessible_analysis(db, analysis_id, user)
    return APIResponse(data=reflection_reads(analysis))


@router.post(
    "/{analysis_id}/decisions", status_code=201,
    response_model=APIResponse[InvestmentDecisionRead])
async def record_decision(
    analysis_id: UUID, payload: InvestmentDecisionCreate,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_analysis_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[InvestmentDecisionRead]:
    from domains.decision.service import DecisionService

    service = DecisionService(db)
    result = await service.record_decision(
        user=user, analysis_id=analysis_id, data=payload)
    background_tasks.add_task(publish_events, list(service.pending_events))
    return APIResponse(data=result)
