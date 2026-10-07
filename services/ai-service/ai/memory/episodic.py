from uuid import UUID
from ai.graph.state import EpisodicMemoryHit
from config import settings
from database import SessionLocal
from sqlalchemy import select
from domains.analysis.model import Recommendation
from domains.memory.repository import EpisodicMemoryRepository
from shared.enums import RecommendationAction


def stored_judge_verdict(
    leaning: str | None, confidence: float | None
) -> tuple[RecommendationAction | None, float | None]:
    if leaning is None or confidence is None:
        return None, None
    try:
        return RecommendationAction(leaning), float(confidence)
    except ValueError:
        return None, None


async def retrieve_episodic_context(
        ticker: str, *, user_id: str | None,
        limit: int | None = None) -> list[EpisodicMemoryHit]:
    owner = UUID(user_id) if user_id else None
    async with SessionLocal() as session:
        rows = await EpisodicMemoryRepository(session).list_recent_for_ticker(
            ticker, user_id=owner, limit=(
                limit
                if limit is not None else settings.EPISODIC_CONTEXT_LIMIT))
        judged: dict[UUID, tuple[str | None, float | None]] = {}
        if rows:
            result = await session.execute(
                select(
                    Recommendation.analysis_id, Recommendation.judge_leaning,
                    Recommendation.judge_confidence).where(
                        Recommendation.analysis_id.in_([
                            r.analysis_id for r in rows
                        ])).order_by(Recommendation.created_at))
            for analysis_id, leaning, confidence in result.all():
                judged[analysis_id] = (leaning, confidence)

    hits: list[EpisodicMemoryHit] = []
    for row in rows:
        try:
            action = RecommendationAction(row.recommendation)
        except ValueError:
            continue
        judge_leaning, judge_confidence = stored_judge_verdict(
            *judged.get(row.analysis_id, (None, None)))
        hits.append(
            EpisodicMemoryHit(
                analysis_id=str(row.analysis_id), ticker=row.ticker,
                analyzed_at=row.created_at, recommendation=action,
                confidence=row.confidence, outcome=row.outcome,
                outcome_return=row.outcome_return, judge_leaning=judge_leaning,
                judge_confidence=judge_confidence))
    return hits
