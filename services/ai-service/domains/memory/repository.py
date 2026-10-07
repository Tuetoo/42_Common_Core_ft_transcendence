from typing import Any, cast
from uuid import UUID
from sqlalchemy import ColumnElement, delete, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from domains.memory.model import AgentMemory, EpisodicMemory


class EpisodicMemoryRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
            self, *, user_id: UUID | None, analysis_id: UUID, ticker: str,
            recommendation: str, confidence: float,
            summary: str) -> EpisodicMemory:
        memory = EpisodicMemory(
            user_id=user_id, analysis_id=analysis_id, ticker=ticker.upper(),
            recommendation=recommendation, confidence=confidence,
            summary=summary)
        self.session.add(memory)
        await self.session.flush()
        return memory

    async def get_by_analysis_id(
            self, analysis_id: UUID) -> EpisodicMemory | None:
        stmt = select(EpisodicMemory).where(
            EpisodicMemory.analysis_id == analysis_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_recent_for_ticker(
            self, ticker: str, *, user_id: UUID | None,
            limit: int = 3) -> list[EpisodicMemory]:
        scope = (
            EpisodicMemory.user_id.is_(None) if user_id is None else or_(
                EpisodicMemory.user_id == user_id,
                EpisodicMemory.user_id.is_(None)))
        stmt = (
            select(EpisodicMemory).where(
                EpisodicMemory.ticker == ticker.upper(),
                scope).order_by(EpisodicMemory.created_at.desc()).limit(limit))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update_outcome(
            self, memory: EpisodicMemory, outcome: str,
            outcome_return: float | None = None) -> None:
        memory.outcome = outcome
        if outcome_return is not None:
            memory.outcome_return = outcome_return
        await self.session.flush()


def _owned_by(user_id: UUID | None) -> ColumnElement[bool]:
    if user_id is None:
        return AgentMemory.user_id.is_(None)
    return AgentMemory.user_id == user_id


class AgentMemoryRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
            self, *, agent_id: UUID, user_id: UUID | None, ticker: str | None,
            score: float, content: dict[str, Any],
            memory_type: str = "trajectory") -> AgentMemory:
        memory = AgentMemory(
            agent_id=agent_id, user_id=user_id,
            ticker=ticker.upper() if ticker else None, score=score,
            content=content, memory_type=memory_type)
        self.session.add(memory)
        await self.session.flush()
        return memory

    async def recent_qualifying_for_agent(
            self, agent_id: UUID, *, user_id: UUID | None,
            ticker: str | None = None, limit: int = 2) -> list[AgentMemory]:
        owned = (AgentMemory.agent_id == agent_id, _owned_by(user_id))
        rows: list[AgentMemory] = []
        if ticker:
            same_ticker_stmt = (
                select(AgentMemory).where(
                    *owned, AgentMemory.ticker == ticker.upper()).order_by(
                        AgentMemory.created_at.desc()).limit(limit))
            rows.extend(
                (await self.session.execute(same_ticker_stmt)).scalars().all())

        if len(rows) < limit:
            seen_ids = {m.id for m in rows}
            other_stmt = (
                select(AgentMemory).where(*owned).order_by(
                    AgentMemory.created_at.desc()).limit(
                        limit - len(rows) + len(seen_ids)))
            for memory in ((await
                            self.session.execute(other_stmt)).scalars().all()):
                if memory.id not in seen_ids and len(rows) < limit:
                    rows.append(memory)
        return rows

    async def prune_beyond_retention(
            self, agent_id: UUID, ticker: str | None, *, user_id: UUID | None,
            keep: int = 3) -> int:
        scope = (
            AgentMemory.agent_id == agent_id, AgentMemory.ticker == ticker,
            _owned_by(user_id))
        keep_ids_stmt = (
            select(AgentMemory.id).where(*scope).order_by(
                AgentMemory.created_at.desc()).limit(keep))
        keep_ids = (await self.session.execute(keep_ids_stmt)).scalars().all()
        delete_stmt = delete(AgentMemory).where(
            *scope, AgentMemory.id.not_in(keep_ids))
        result = await self.session.execute(delete_stmt)
        await self.session.flush()
        return cast(CursorResult[Any], result).rowcount
