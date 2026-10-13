from datetime import UTC, datetime
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from domains.analysis.model import (AgentRun, Analysis, DebateSession,
                                    Recommendation)
from domains.analysis.repository import (UNFILLED_TIER_STATUSES,
                                         AnalysisRepository, acceptance_lapsed)
from domains.analysis.schema import (AgentResultRead, AgentRunRead,
                                     AnalysisDetailRead, AnalysisRead,
                                     DebateArgumentRead, DebateRead,
                                     RecommendationRead,
                                     RecommendationTierRead, ReflectionRead)
from shared.enums import TierStatus, WorkflowStatus
from shared.exceptions import NotFoundError
from shared.schemas import PaginatedResponse, PaginationParams


def _agent_run_read(run: AgentRun) -> AgentRunRead:
    return AgentRunRead(
        id=run.id, agent_name=run.agent.name, status=run.status,
        started_at=run.started_at, completed_at=run.completed_at,
        duration_ms=run.duration_ms, result=(
            AgentResultRead.model_validate(run.result)
            if run.result else None))


def _tier_reads(
        rec: Recommendation,
        user_id: UUID | None) -> list[RecommendationTierRead]:
    tiers = sorted(
        rec.tiers, key=lambda t: (t.side, t.target_price, str(t.id)))
    now = datetime.now(UTC)
    window_passed = (
        rec.window_expires_at is not None and rec.window_expires_at <= now)
    return [
        RecommendationTierRead(
            id=t.id, recommendation_id=t.recommendation_id, side=t.side,
            target_price=t.target_price, quantity=t.quantity, status=(
                TierStatus.EXPIRED.value if (
                    window_passed and t.status in UNFILLED_TIER_STATUSES)
                or acceptance_lapsed(
                    status=t.status, user_id=user_id,
                    issued_at=rec.created_at, now=now) else t.status),
            window_expires_at=rec.window_expires_at,
            triggered_at=t.triggered_at, triggered_price=t.triggered_price,
            created_at=t.created_at) for t in tiers
    ]


def _recommendation_read(
        rec: Recommendation, analysis: Analysis) -> RecommendationRead:
    return RecommendationRead(
        id=rec.id, analysis_id=rec.analysis_id, ticker=analysis.ticker,
        recommendation=rec.recommendation, confidence=rec.confidence,
        reasoning=rec.reasoning,
        confidence_adjustment_note=rec.confidence_adjustment_note,
        tier_generation_note=rec.tier_generation_note,
        debate_converged=rec.debate_converged, created_at=rec.created_at,
        window_expires_at=rec.window_expires_at, accepted_at=rec.accepted_at,
        tiers=_tier_reads(rec, analysis.user_id))


def _debate_read(session_row: DebateSession) -> DebateRead:
    return DebateRead(
        id=session_row.id, arguments=[
            DebateArgumentRead(
                round_number=a.round_number, side=a.side, content=a.content,
                citations=a.citations) for a in session_row.arguments
        ])


def analysis_detail_read(analysis: Analysis) -> AnalysisDetailRead:
    return AnalysisDetailRead(
        id=analysis.id, ticker=analysis.ticker, status=analysis.status,
        user_id=analysis.user_id, portfolio_id=analysis.portfolio_id,
        created_at=analysis.created_at, failure_reason=analysis.failure_reason,
        agent_runs=[_agent_run_read(r) for r in analysis.agent_runs], debate=(
            _debate_read(analysis.debate_sessions[0])
            if analysis.debate_sessions else None),
        reflections=reflection_reads(analysis), recommendation=(
            _recommendation_read(analysis.recommendations[-1], analysis)
            if analysis.recommendations else None))


def recommendation_read(analysis: Analysis) -> RecommendationRead:
    if not analysis.recommendations:
        raise NotFoundError(
            f"No recommendation yet for analysis {analysis.id} "
            f"(status={analysis.status})")
    return _recommendation_read(analysis.recommendations[-1], analysis)


def tier_reads(analysis: Analysis) -> list[RecommendationTierRead]:
    if not analysis.recommendations:
        return []
    return _tier_reads(analysis.recommendations[-1], analysis.user_id)


def debate_read(analysis: Analysis) -> DebateRead:
    if not analysis.debate_sessions:
        raise NotFoundError(f"No debate for analysis {analysis.id}")
    return _debate_read(analysis.debate_sessions[0])


def reflection_reads(analysis: Analysis) -> list[ReflectionRead]:
    return [ReflectionRead.model_validate(r) for r in analysis.reflections]


class AnalysisService:

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = AnalysisRepository(session)

    async def create_analysis(
            self, *, user_id: UUID | None, portfolio_id: UUID | None,
            ticker: str) -> AnalysisRead:
        analysis = await self.repo.create(
            user_id=user_id, portfolio_id=portfolio_id, ticker=ticker,
            status=WorkflowStatus.PENDING.value)
        return AnalysisRead.model_validate(analysis)

    async def get_analysis_detail(
            self, analysis_id: UUID) -> AnalysisDetailRead:
        analysis = await self.repo.get_by_id(analysis_id, with_details=True)
        return analysis_detail_read(analysis)

    async def list_analyses(
            self, user_id: UUID,
            pagination: PaginationParams) -> PaginatedResponse[AnalysisRead]:
        analyses, total = await self.repo.list_for_user(user_id, pagination)
        return PaginatedResponse(
            items=[AnalysisRead.model_validate(a) for a in analyses],
            total=total, page=pagination.page, page_size=pagination.page_size)
