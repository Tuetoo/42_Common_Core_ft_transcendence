from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from ai_events import TierEventInfo
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from domains.analysis.model import (Agent, AgentResult, AgentRun, Analysis,
                                    DebateArgument, DebateSession,
                                    Recommendation, RecommendationTier,
                                    Reflection)
from shared.database import count_query
from shared.enums import (RecommendationAction, TierSide, TierStatus,
                          WorkflowStatus)
from shared.exceptions import NotFoundError
from shared.schemas import PaginationParams

_ACTIVE_STATUSES = (WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value)

_UNFILLED_TIER_STATUSES = (
    TierStatus.PENDING_ACCEPTANCE.value,
    TierStatus.ACCEPTED_AWAITING_TRIGGER.value)


def bands_cross(buy_prices: list[Decimal], sell_prices: list[Decimal]) -> bool:
    if not buy_prices or not sell_prices:
        return False
    return max(buy_prices) >= min(sell_prices)


@dataclass(frozen=True)
class TierInvalidation:
    recommendation_id: UUID
    issued_at: datetime
    side: str
    count: int
    reason: str
    tiers: tuple[TierEventInfo, ...] = ()

    def note(self) -> str:
        return (
            f"Closed {self.count} earlier unfilled {self.side.upper()} "
            f"tier(s) of "
            f"recommendation {self.recommendation_id} "
            f"({self.issued_at.date().isoformat()}) as superseded: "
            f"{self.reason}.")


class AnalysisRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_open_recommendation_for(
            self, *, portfolio_id: UUID, ticker: str) -> Recommendation | None:
        stmt = (
            select(Recommendation).join(
                Analysis, Recommendation.analysis_id == Analysis.id).where(
                    Analysis.portfolio_id == portfolio_id,
                    Analysis.ticker == ticker.upper(),
                    Recommendation.window_expires_at.is_not(None),
                    Recommendation.window_expires_at > datetime.now(UTC),
                    select(RecommendationTier.id).where(
                        RecommendationTier.recommendation_id ==
                        Recommendation.id,
                        RecommendationTier.status.in_(
                            _UNFILLED_TIER_STATUSES)).exists()).order_by(
                                Recommendation.created_at.desc()).limit(1))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_agent_run_outputs(
            self,
            analysis_id: UUID) -> list[tuple[str, str, dict[str, Any] | None]]:
        stmt = (
            select(Agent.name, AgentRun.status, AgentResult.output).join(
                Agent, AgentRun.agent_id == Agent.id).outerjoin(
                    AgentResult,
                    AgentResult.agent_run_id == AgentRun.id).where(
                        AgentRun.analysis_id == analysis_id))
        rows = (await self.session.execute(stmt)).all()
        return [(name, status, output) for name, status, output in rows]

    async def create(
            self, *, user_id: UUID | None, portfolio_id: UUID | None,
            ticker: str, status: str = "pending") -> Analysis:
        analysis = Analysis(
            user_id=user_id, portfolio_id=portfolio_id, ticker=ticker.upper(),
            status=status)
        self.session.add(analysis)
        await self.session.flush()
        return analysis

    async def get_by_id(
            self, analysis_id: UUID, *,
            with_details: bool = False) -> Analysis:
        stmt = select(Analysis).where(Analysis.id == analysis_id)
        if with_details:
            stmt = stmt.options(
                selectinload(Analysis.agent_runs).selectinload(AgentRun.agent),
                selectinload(Analysis.agent_runs).selectinload(
                    AgentRun.result),
                selectinload(Analysis.debate_sessions).selectinload(
                    DebateSession.arguments),
                selectinload(Analysis.reflections),
                selectinload(Analysis.recommendations).selectinload(
                    Recommendation.tiers))
        result = await self.session.execute(stmt)
        analysis = result.scalar_one_or_none()
        if analysis is None:
            raise NotFoundError(f"Analysis {analysis_id} not found")
        return analysis

    async def set_status_and_reason(
            self, analysis_id: UUID, status: str, failure_reason: str | None,
            *, only_if_active: bool = False) -> str | None:
        stmt = (
            update(Analysis).where(Analysis.id == analysis_id).values(
                status=status, failure_reason=failure_reason).returning(
                    Analysis.ticker).execution_options(
                        synchronize_session="fetch"))
        if only_if_active:
            stmt = stmt.where(Analysis.status.in_(_ACTIVE_STATUSES))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def fail_all_active(
        self, failure_reason: str
    ) -> list[tuple[UUID, str, UUID | None, UUID | None]]:
        stmt = (
            update(Analysis).where(
                Analysis.status.in_(_ACTIVE_STATUSES)).values(
                    status=WorkflowStatus.FAILED.value,
                    failure_reason=failure_reason).returning(
                        Analysis.id, Analysis.ticker, Analysis.user_id,
                        Analysis.portfolio_id).execution_options(
                            synchronize_session=False))
        result = await self.session.execute(stmt)
        return [(row[0], row[1], row[2], row[3]) for row in result.all()]

    async def get_owner_fields(
            self, analysis_id: UUID) -> tuple[UUID | None, UUID | None]:
        row = (
            await self.session.execute(
                select(Analysis.user_id, Analysis.portfolio_id).where(
                    Analysis.id == analysis_id))).first()
        if row is None:
            return None, None
        return row[0], row[1]

    async def list_for_user(
            self, user_id: UUID,
            pagination: PaginationParams) -> tuple[list[Analysis], int]:
        stmt = (
            select(Analysis).where(Analysis.user_id == user_id).order_by(
                Analysis.created_at.desc()))
        total = await count_query(self.session, stmt)
        stmt = stmt.offset(pagination.offset).limit(pagination.limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def get_latest_recommendation_for_ticker(
            self, ticker: str,
            portfolio_id: UUID) -> tuple[Recommendation, Analysis] | None:
        stmt = (
            select(Recommendation, Analysis).join(
                Analysis, Recommendation.analysis_id == Analysis.id).where(
                    Analysis.ticker == ticker.upper()).where(
                        Analysis.portfolio_id == portfolio_id).order_by(
                            Recommendation.created_at.desc()).limit(1))
        result = await self.session.execute(stmt)
        row = result.first()
        return (row[0], row[1]) if row else None


class AgentCatalogRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create(self, name: str) -> Agent:
        stmt = (
            pg_insert(Agent).values(name=name).on_conflict_do_nothing(
                index_elements=["name"]))
        await self.session.execute(stmt)
        await self.session.flush()
        result = await self.session.execute(
            select(Agent).where(Agent.name == name))
        return result.scalar_one()

    async def get_by_name(self, name: str) -> Agent | None:
        result = await self.session.execute(
            select(Agent).where(Agent.name == name))
        return result.scalar_one_or_none()


class AnalysisPersistenceRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def save_agent_run(
            self, *, analysis_id: UUID, agent_id: UUID, status: str,
            started_at: datetime | None, completed_at: datetime | None,
            duration_ms: int | None, score: float | None, summary: str | None,
            output: dict[str, Any] | None) -> AgentRun:
        agent_run = AgentRun(
            analysis_id=analysis_id, agent_id=agent_id, status=status,
            started_at=started_at, completed_at=completed_at,
            duration_ms=duration_ms)
        self.session.add(agent_run)
        await self.session.flush()
        self.session.add(
            AgentResult(
                agent_run_id=agent_run.id, score=score, summary=summary,
                output=output))
        return agent_run

    async def save_debate_session(
            self, *, analysis_id: UUID,
            arguments: list[dict[str, Any]]) -> DebateSession:
        session_row = DebateSession(analysis_id=analysis_id)
        self.session.add(session_row)
        await self.session.flush()
        for arg in arguments:
            self.session.add(
                DebateArgument(
                    debate_session_id=session_row.id, side=arg["side"],
                    content=arg["content"], round_number=arg["round_number"],
                    citations=arg.get("citations") or []))
        return session_row

    async def save_reflection(
            self, *, analysis_id: UUID, round_number: int, verified: bool,
            issues: list[str] | None,
            retry_agents: dict[str, str] | None) -> Reflection:
        reflection = Reflection(
            analysis_id=analysis_id, round_number=round_number,
            verified=verified, issues=issues, retry_agents=retry_agents)
        self.session.add(reflection)
        return reflection

    async def save_recommendation(
            self, *, analysis_id: UUID, recommendation: str, confidence: float,
            reasoning: str, confidence_adjustment_note: str | None,
            debate_converged: bool, tier_generation_note: str | None = None,
            judge_leaning: str | None = None,
            judge_confidence: float | None = None) -> Recommendation:
        rec = Recommendation(
            analysis_id=analysis_id, recommendation=recommendation,
            confidence=confidence, judge_leaning=judge_leaning,
            judge_confidence=judge_confidence, reasoning=reasoning,
            confidence_adjustment_note=confidence_adjustment_note,
            debate_converged=debate_converged,
            tier_generation_note=tier_generation_note)
        self.session.add(rec)
        await self.session.flush()
        return rec

    async def save_recommendation_tiers(
            self, *, recommendation_id: UUID, side: str, window_days: int,
            tiers: list[tuple[Decimal, Decimal]]) -> None:
        window_expires_at = datetime.now(UTC) + timedelta(days=window_days)
        recommendation = await self.session.get(
            Recommendation, recommendation_id)
        if recommendation is not None:
            recommendation.window_expires_at = window_expires_at

        for target_price, quantity in tiers:
            self.session.add(
                RecommendationTier(
                    recommendation_id=recommendation_id, side=side,
                    target_price=target_price, quantity=quantity))
        await self.session.flush()


class RecommendationTierRepository:

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_open_tier_ids(self) -> list[UUID]:
        result = await self.session.execute(
            select(RecommendationTier.id).where(
                RecommendationTier.status.in_((
                    TierStatus.PENDING_ACCEPTANCE.value,
                    TierStatus.ACCEPTED_AWAITING_TRIGGER.value))))
        return list(result.scalars().all())

    async def get_tier_with_details(
            self, tier_id: UUID) -> RecommendationTier | None:
        stmt = (
            select(RecommendationTier).where(
                RecommendationTier.id == tier_id).options(
                    selectinload(
                        RecommendationTier.recommendation).selectinload(
                            Recommendation.analysis)).execution_options(
                                populate_existing=True))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def unfilled_buy_tier_count(
            self, *, portfolio_id: UUID, ticker: str) -> int:
        stmt = (
            select(func.count(RecommendationTier.id)).join(
                Recommendation, RecommendationTier.recommendation_id ==
                Recommendation.id).join(
                    Analysis, Recommendation.analysis_id == Analysis.id).where(
                        Analysis.portfolio_id == portfolio_id,
                        Analysis.ticker == ticker.upper(),
                        RecommendationTier.side == TierSide.BUY.value,
                        RecommendationTier.status.in_(_UNFILLED_TIER_STATUSES),
                        Recommendation.window_expires_at > datetime.now(UTC)))
        return int((await self.session.execute(stmt)).scalar_one())

    async def committed_buy_cash(
            self, *, portfolio_id: UUID, exclude_ticker: str) -> Decimal:
        stmt = (
            select(
                RecommendationTier.target_price,
                RecommendationTier.quantity).join(
                    Recommendation,
                    RecommendationTier.recommendation_id == Recommendation.id).
            join(Analysis, Recommendation.analysis_id == Analysis.id).where(
                Analysis.portfolio_id == portfolio_id, Analysis.ticker
                != exclude_ticker.upper(),
                RecommendationTier.side == TierSide.BUY.value,
                RecommendationTier.status.in_(_UNFILLED_TIER_STATUSES),
                Recommendation.window_expires_at > datetime.now(UTC)))
        rows = (await self.session.execute(stmt)).all()
        return sum(
            (Decimal(price) * Decimal(quantity) for price, quantity in rows),
            Decimal(0))

    async def invalidate_unfilled_tiers_for_new_recommendation(
            self, *, portfolio_id: UUID, ticker: str,
            new_action: RecommendationAction, new_prices: list[Decimal],
            exclude_recommendation_id: UUID | None = None,
            source: str = "a newer") -> list[TierInvalidation]:
        stmt = (
            select(
                RecommendationTier, Recommendation.created_at, Analysis.id,
                Analysis.user_id).join(
                    Recommendation,
                    RecommendationTier.recommendation_id == Recommendation.id).
            join(Analysis, Recommendation.analysis_id == Analysis.id).where(
                Analysis.portfolio_id == portfolio_id,
                Analysis.ticker == ticker.upper(),
                RecommendationTier.status.in_(_UNFILLED_TIER_STATUSES),
                Recommendation.window_expires_at > datetime.now(UTC)))
        if exclude_recommendation_id is not None:
            stmt = stmt.where(
                RecommendationTier.recommendation_id !=
                exclude_recommendation_id)
        rows = (await self.session.execute(stmt)).all()
        sets: dict[tuple[UUID, str], list[RecommendationTier]] = {}
        issued: dict[UUID, datetime] = {}
        owner_of: dict[UUID, tuple[UUID, UUID | None]] = {}
        for tier, created_at, analysis_id, analysis_user_id in rows:
            sets.setdefault((tier.recommendation_id, tier.side),
                            []).append(tier)
            issued[tier.recommendation_id] = created_at
            owner_of[tier.id] = (analysis_id, analysis_user_id)

        closed: list[TierInvalidation] = []
        for (rec_id, side), tiers in sets.items():
            prices = [Decimal(t.target_price) for t in tiers]
            reason: str | None = None
            if new_action == RecommendationAction.HOLD:
                reason = (
                    f"{source} HOLD recommendation closes every unfilled tier")
            elif new_action == RecommendationAction.BUY:
                if side == TierSide.BUY.value:
                    reason = f"replaced by {source} BUY recommendation"
                elif bands_cross(new_prices, prices):
                    reason = (
                        f"{source} BUY recommendation's prices cross this "
                        f"SELL "
                        "set (a BUY price at or above a SELL price)")
            elif side == TierSide.SELL.value:
                reason = f"replaced by {source} SELL recommendation"
            elif not new_prices:
                reason = (
                    f"{source} SELL recommendation without SELL tiers "
                    f"(bearish "
                    "view) closes every unfilled BUY tier")
            elif bands_cross(prices, new_prices):
                reason = (
                    f"{source} SELL recommendation's prices cross this BUY "
                    f"set "
                    "(a BUY price at or above a SELL price)")
            if reason is None:
                continue
            ids = [t.id for t in tiers]
            result = await self.session.execute(
                update(RecommendationTier).where(
                    RecommendationTier.id.in_(ids),
                    RecommendationTier.status.in_(
                        _UNFILLED_TIER_STATUSES)).values(
                            status=TierStatus.SUPERSEDED.value).returning(
                                RecommendationTier.id).execution_options(
                                    synchronize_session="fetch"))
            closed_ids = {row[0] for row in result.all()}
            count = len(closed_ids)
            if count:
                closed.append(
                    TierInvalidation(
                        recommendation_id=rec_id, issued_at=issued[rec_id],
                        side=side, count=count, reason=reason, tiers=tuple(
                            TierEventInfo(
                                tier_id=t.id, analysis_id=owner_of[
                                    t.id][0], user_id=owner_of[t.id][1],
                                portfolio_id=portfolio_id, ticker=ticker.upper(
                                ), side=t.side, quantity=Decimal(
                                    t.quantity), price=Decimal(t.target_price))
                            for t in tiers if t.id in closed_ids)))
        return closed

    async def cancel_unfilled_tiers_for_portfolio(
            self, portfolio_id: UUID, *, note: str) -> list[TierEventInfo]:
        rows = (
            await self.session.execute(
                select(
                    RecommendationTier.id, RecommendationTier.side,
                    RecommendationTier.quantity,
                    RecommendationTier.target_price,
                    RecommendationTier.recommendation_id, Analysis.id,
                    Analysis.user_id, Analysis.ticker).join(
                        Recommendation, RecommendationTier.recommendation_id ==
                        Recommendation.id).join(
                            Analysis,
                            Recommendation.analysis_id == Analysis.id).where(
                                Analysis.portfolio_id == portfolio_id,
                                RecommendationTier.status.in_(
                                    _UNFILLED_TIER_STATUSES)))).all()
        if not rows:
            return []
        result = await self.session.execute(
            update(RecommendationTier).where(
                RecommendationTier.id.in_([row[0] for row in rows]),
                RecommendationTier.status.in_(_UNFILLED_TIER_STATUSES)).values(
                    status=TierStatus.EXPIRED.value).returning(
                        RecommendationTier.id).execution_options(
                            synchronize_session=False))
        closed_ids = {row[0] for row in result.all()}
        closed = [row for row in rows if row[0] in closed_ids]
        for rec_id in {row[4] for row in closed}:
            await self.session.execute(
                update(Recommendation).where(
                    Recommendation.id == rec_id).values(
                        tier_generation_note=func.concat_ws(
                            "\n", Recommendation.tier_generation_note,
                            note)).execution_options(
                                synchronize_session=False))
        return [
            TierEventInfo(
                tier_id=row[0], analysis_id=row[5], user_id=row[6],
                portfolio_id=portfolio_id, ticker=row[7], side=row[1],
                quantity=Decimal(row[2]), price=Decimal(row[3]))
            for row in closed
        ]

    async def list_triggered_tiers(
            self, *, user_id: UUID | None = None,
            start_date: date | None = None,
            end_date: date | None = None) -> list[RecommendationTier]:
        stmt = (
            select(RecommendationTier).where(
                RecommendationTier.status ==
                TierStatus.TRIGGERED.value).options(
                    selectinload(
                        RecommendationTier.recommendation).selectinload(
                            Recommendation.analysis)).order_by(
                                RecommendationTier.triggered_at))
        if start_date is not None:
            stmt = stmt.where(
                RecommendationTier.triggered_at >= datetime.combine(
                    start_date, time.min, tzinfo=UTC))
        if end_date is not None:
            stmt = stmt.where(
                RecommendationTier.triggered_at <= datetime.combine(
                    end_date, time.max, tzinfo=UTC))
        if user_id is not None:
            stmt = (
                stmt.join(
                    Recommendation, RecommendationTier.recommendation_id ==
                    Recommendation.id).join(
                        Analysis,
                        Recommendation.analysis_id == Analysis.id).where(
                            Analysis.user_id == user_id))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_triggered_tiers_for_ticker(
            self, ticker: str, *, start_date: date, end_date: date,
            portfolio_id: UUID | None = None,
            user_id: UUID | None = None) -> list[RecommendationTier]:
        conditions = [
            RecommendationTier.status == TierStatus.TRIGGERED.value,
            Analysis.ticker == ticker.upper(), RecommendationTier.triggered_at
            >= datetime.combine(start_date, time.min,
                                tzinfo=UTC), RecommendationTier.triggered_at
            <= datetime.combine(end_date, time.max, tzinfo=UTC)
        ]
        if portfolio_id is not None:
            conditions.append(Analysis.portfolio_id == portfolio_id)
        if user_id is not None:
            conditions.append(Analysis.user_id == user_id)
        stmt = (
            select(RecommendationTier).join(
                Recommendation,
                RecommendationTier.recommendation_id == Recommendation.id).
            join(Analysis, Recommendation.analysis_id == Analysis.id).where(
                *conditions).options(
                    selectinload(
                        RecommendationTier.recommendation).selectinload(
                            Recommendation.analysis)).order_by(
                                RecommendationTier.triggered_at))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
