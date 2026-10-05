from datetime import UTC, datetime
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from domains.analysis.model import Agent, AgentResult, AgentRun, Recommendation
from domains.analytics.model import AgentMetric, RecommendationMetric
from shared.enums import AgentStatus


class AnalyticsRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_agent_metrics(self) -> list[AgentMetric]:
        result = await self.session.execute(select(AgentMetric))
        return list(result.scalars().all())

    async def list_agent_ids(self) -> list[UUID]:
        result = await self.session.execute(select(Agent.id))
        return list(result.scalars().all())

    async def compute_agent_rollup(
            self, agent_id: UUID) -> tuple[float | None, float | None]:
        attempted_statuses = (
            AgentStatus.COMPLETED.value, AgentStatus.FAILED.value)
        result = await self.session.execute(
            select(AgentRun.status, AgentResult.score).join(
                AgentResult, AgentResult.agent_run_id == AgentRun.id).where(
                    AgentRun.agent_id == agent_id).where(
                        AgentRun.status.in_(attempted_statuses)))
        rows = result.all()
        if not rows:
            return None, None

        scores = [float(score) for _, score in rows if score is not None]
        avg_score = sum(scores) / len(scores) if scores else None

        completed = sum(
            1 for status, _ in rows if status == AgentStatus.COMPLETED.value)
        success_rate = completed / len(rows)

        return avg_score, success_rate

    async def upsert_agent_metric(
            self, *, agent_id: UUID, avg_score: float | None,
            success_rate: float | None) -> AgentMetric:
        stmt = pg_insert(AgentMetric).values(
            agent_id=agent_id, avg_score=avg_score, success_rate=success_rate)
        stmt = stmt.on_conflict_do_update(
            index_elements=["agent_id"], set_={
                "avg_score": avg_score,
                "success_rate": success_rate
            })
        await self.session.execute(stmt)
        await self.session.flush()
        result = await self.session.execute(
            select(AgentMetric).where(AgentMetric.agent_id == agent_id))
        return result.scalar_one()

    async def list_recommendations_pending_metric(
            self, *, min_age_days: int,
            limit: int = 200) -> list[Recommendation]:
        cutoff = datetime.now(UTC).timestamp() - min_age_days * 86400
        cutoff_dt = datetime.fromtimestamp(cutoff, tz=UTC)
        already_scored = select(RecommendationMetric.recommendation_id)
        stmt = (
            select(Recommendation).where(
                Recommendation.created_at <= cutoff_dt).where(
                    Recommendation.id.not_in(already_scored)).limit(limit))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
