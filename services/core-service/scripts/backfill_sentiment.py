import argparse
import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID
from database import SessionLocal, engine
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from domains.research.model import ResearchComment, ResearchNote
from domains.research.sentiment import (refresh_comment_sentiment,
                                        refresh_note_sentiment)
from shared.http_client import close_http_clients
from shared.redis import close_redis_connections

RefreshFn = Callable[[UUID], Awaitable[bool]]
SleepFn = Callable[[float], Awaitable[None]]


@dataclass
class Plan:
    note_ids: list[UUID] = field(default_factory=list)
    comment_ids: list[UUID] = field(default_factory=list)
    notes_missing: int = 0
    comments_missing: int = 0


@dataclass
class Result:
    stored: int = 0
    failed: list[tuple[str, UUID]] = field(default_factory=list)


async def build_plan(
        session: AsyncSession, *, kind: str, limit: int | None) -> Plan:
    plan = Plan()
    if kind in ("note", "all"):
        missing = or_(
            ResearchNote.sentiment_label.is_(None),
            ResearchNote.sentiment_analyzed_at < ResearchNote.updated_at)
        plan.notes_missing = int((
            await session.execute(
                select(func.count()).select_from(ResearchNote).where(missing)
            )).scalar_one())
        stmt = (
            select(ResearchNote.id).where(missing).order_by(
                ResearchNote.created_at.asc(), ResearchNote.id.asc()))
        if limit is not None:
            stmt = stmt.limit(limit)
        plan.note_ids = list((await session.execute(stmt)).scalars().all())
    if kind in ("comment", "all"):
        missing_c = or_(
            ResearchComment.sentiment_label.is_(None),
            and_(
                ResearchComment.updated_at.is_not(None),
                ResearchComment.sentiment_analyzed_at
                < ResearchComment.updated_at))
        plan.comments_missing = int((
            await session.execute(
                select(func.count()).select_from(ResearchComment).where(
                    missing_c))).scalar_one())
        stmt_c = (
            select(ResearchComment.id).where(missing_c).order_by(
                ResearchComment.created_at.asc(), ResearchComment.id.asc()))
        if limit is not None:
            stmt_c = stmt_c.limit(limit)
        plan.comment_ids = list((await
                                 session.execute(stmt_c)).scalars().all())
    return plan


async def apply_plan(
        plan: Plan, *, rate_per_minute: float,
        refresh_note: RefreshFn = refresh_note_sentiment,
        refresh_comment: RefreshFn = refresh_comment_sentiment,
        sleep: SleepFn = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic) -> Result:
    interval = 60.0 / rate_per_minute
    result = Result()
    next_at = clock()
    work: list[tuple[str, UUID, RefreshFn]] = [
        *(("note", note_id, refresh_note) for note_id in plan.note_ids),
        *(("comment", comment_id, refresh_comment)
          for comment_id in plan.comment_ids)
    ]
    for kind, row_id, refresh in work:
        wait = next_at - clock()
        if wait > 0:
            await sleep(wait)
        next_at = max(next_at, clock()) + interval
        if await refresh(row_id):
            result.stored += 1
        else:
            result.failed.append((kind, row_id))
    return result


def print_plan(plan: Plan) -> None:
    print(
        f"Notes without current sentiment:    {plan.notes_missing}"
        f" (this run: {len(plan.note_ids)})")
    print(
        f"Comments without current sentiment: {plan.comments_missing}"
        f" (this run: {len(plan.comment_ids)})")


def print_result(result: Result) -> None:
    print(f"\nStored: {result.stored}")
    if result.failed:
        print(
            f"Not stored (empty text, unchanged failure or LLM unavailable): "
            f"{len(result.failed)}")
        for kind, row_id in result.failed[:50]:
            print(f"   {kind} {row_id}")


DESCRIPTION = (
    "Operator script: compute the sentiment of existing\n"
    "research notes and comments that have no label or a label\n"
    "older than the last edit.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--apply", action="store_true",
        help="call ai-service and store results (default: preview)")
    parser.add_argument(
        "--kind", choices=("note", "comment", "all"), default="all")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="at most N rows of each kind in this run")
    parser.add_argument(
        "--rate", type=float, default=30.0,
        help="maximum ai-service calls per minute (default 30)")
    return parser


async def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.rate <= 0 or (args.limit is not None and args.limit <= 0):
        print("--rate and --limit must be positive.")
        return 2
    try:
        async with SessionLocal() as session:
            plan = await build_plan(session, kind=args.kind, limit=args.limit)
        print_plan(plan)
        if not args.apply:
            print("\nPreview only. Re-run with --apply to compute sentiment.")
            return 0
        if not plan.note_ids and not plan.comment_ids:
            print("\nNothing to do.")
            return 0
        result = await apply_plan(plan, rate_per_minute=args.rate)
        print_result(result)
        return 1 if result.failed else 0
    finally:
        await close_http_clients()
        await close_redis_connections()
        await engine.dispose()


def _run() -> Any:
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    _run()
