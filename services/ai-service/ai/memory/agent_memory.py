from typing import Any
from uuid import UUID
import structlog
from ai.agents.evidence_grounding import non_english_reason
from ai.graph.state import EpisodicMemoryHit
from config import settings
from database import SessionLocal
from sqlalchemy import select
from domains.analysis.model import Recommendation
from domains.analysis.repository import AgentCatalogRepository
from domains.memory.repository import (AgentMemoryRepository,
                                       EpisodicMemoryRepository)
from shared.enums import AgentRole, RecommendationAction

logger = structlog.get_logger(__name__)

_MIN_SCORE_TO_REMEMBER = 0.7
_DEFAULT_LIMIT = 2
_ROW_SCAN_LIMIT = 10
_MAX_STEPS_PER_TRAJECTORY = 4
MODEL_STEPS_ONLY_KEY = "model_steps_only"


def owner_uuid(user_id: str | None) -> UUID | None:
    try:
        return UUID(user_id) if user_id else None
    except ValueError:
        logger.warning("procedural_memory.owner_id_invalid", user_id=user_id)
        return None


def _is_model_chosen_call(step: dict[str, Any]) -> bool:
    return bool(
        step.get("action") and step.get("is_tool_result", True)
        and not step.get("prefetched"))


def build_trajectory_content(
        *, final_answer: str, scratchpad: list[dict[str,
                                                    Any]]) -> dict[str, Any]:
    steps = [{
        "action": step.get("action"),
        "action_input": step.get("action_input")
    } for step in scratchpad
             if _is_model_chosen_call(step)][:_MAX_STEPS_PER_TRAJECTORY]
    return {
        "final_answer": final_answer,
        "steps": steps,
        MODEL_STEPS_ONLY_KEY: True
    }


def usable_hint_steps(content: object) -> list[dict[str, Any]]:
    if not isinstance(content, dict) or not content.get(MODEL_STEPS_ONLY_KEY):
        return []
    steps = content.get("steps")
    if not isinstance(steps, list):
        return []
    return [s for s in steps if isinstance(s, dict) and s.get("action")]


def procedural_memory_skip_reason(
        *, score: float, final_answer: str, is_well_supported: bool,
        hit_iteration_cap: bool, verification_warnings: list[str] | None,
        no_evidence: bool) -> str | None:
    if score < _MIN_SCORE_TO_REMEMBER:
        return "score_below_threshold"
    if not is_well_supported:
        return "not_well_supported"
    if hit_iteration_cap:
        return "hit_iteration_cap"
    if verification_warnings:
        return "verification_warnings"
    if no_evidence:
        return "no_evidence"
    if non_english_reason(final_answer) is not None:
        return "non_english"
    return None


async def maybe_remember_trajectory(
        *, agent_id: UUID, user_id: UUID | None, ticker: str | None,
        score: float, final_answer: str, scratchpad: list[dict[str, Any]],
        is_well_supported: bool, hit_iteration_cap: bool,
        verification_warnings: list[str] | None, no_evidence: bool) -> bool:
    skip_reason = procedural_memory_skip_reason(
        score=score, final_answer=final_answer,
        is_well_supported=is_well_supported,
        hit_iteration_cap=hit_iteration_cap,
        verification_warnings=verification_warnings, no_evidence=no_evidence)
    if skip_reason is not None:
        logger.info("procedural_memory.write_skipped", reason=skip_reason)
        return False
    content = build_trajectory_content(
        final_answer=final_answer, scratchpad=scratchpad)
    if not content["steps"]:
        logger.info(
            "procedural_memory.write_skipped", reason="no_model_chosen_calls")
        return False
    async with SessionLocal() as session:
        repo = AgentMemoryRepository(session)
        await repo.create(
            agent_id=agent_id, user_id=user_id, ticker=ticker, score=score,
            content=content)
        await repo.prune_beyond_retention(
            agent_id,
            ticker.upper() if ticker else None, user_id=user_id)
        await session.commit()
    return True


async def retrieve_procedural_hints(
        role: AgentRole, ticker: str, *, user_id: UUID | None,
        limit: int = _DEFAULT_LIMIT) -> str | None:
    async with SessionLocal() as session:
        agent = await AgentCatalogRepository(session).get_by_name(role.value)
        if agent is None:
            return None
        rows = await AgentMemoryRepository(
            session).recent_qualifying_for_agent(
                agent.id, user_id=user_id, ticker=ticker,
                limit=max(limit, _ROW_SCAN_LIMIT))

    usable = [(row, steps) for row in rows
              if (steps := usable_hint_steps(row.content))][:limit]
    if len(usable) < len(rows[:limit]):
        logger.info(
            "procedural_memory.rows_without_model_calls_skipped",
            role=role.value, scanned=len(rows), used=len(usable))
    if not usable:
        return None

    lines = [
        "Past high-scoring tool-use patterns for this role: calls the "
        "model chose itself, beyond the calls code makes before the first "
        "turn (for reference, not a script to follow verbatim; they carry "
        "no facts about today):"
    ]
    for row, steps in usable:
        step_summary = "; ".join(
            f"{s.get('action')}({s.get('action_input')})" for s in steps)
        lines.append(f"- [score={row.score:.2f}] tools used: {step_summary}")
    return "\n".join(lines)


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
