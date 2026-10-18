import asyncio
from collections.abc import Coroutine
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID
import structlog
from ai_events import backtest_deleted_payload, publish_event
from clients.core_client import get_portfolio_snapshots
from database import get_db
from deps import ensure_portfolio_access, get_analysis_writer, get_current_user
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from domains.analysis.repository import RecommendationTierRepository
from domains.backtest.model import Backtest
from domains.backtest.repository import BacktestRepository
from domains.backtest.schema import (PORTFOLIO_SNAPSHOT_STRATEGY,
                                     RECOMMENDATION_REPLAY_STRATEGY,
                                     BacktestCreate, BacktestDetailRead,
                                     BacktestMetricsRead, BacktestRead,
                                     BenchmarkResultRead,
                                     PortfolioBacktestCreate,
                                     RecommendationReplayCreate)
from shared.enums import WorkflowStatus
from shared.events import EventName
from shared.exceptions import (ConflictError, NotFoundError,
                               RateLimitExceededError, ValidationAppError)
from shared.schemas import APIResponse
from shared.security import CurrentUser

router = APIRouter(tags=["backtests"])
logger = structlog.get_logger(__name__)

BACKTEST_MAX_ACTIVE_PER_USER = 2

_background_tasks: set[asyncio.Task[None]] = set()


def _start_background(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def stop_background_backtests() -> None:
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _ensure_backtest_capacity(
        db: AsyncSession, user: CurrentUser) -> None:
    active = await BacktestRepository(db).count_active_for_user(
        user.user_id, lock=True)
    if active >= BACKTEST_MAX_ACTIVE_PER_USER:
        logger.info(
            "backtest_rejected_limit", user_id=str(user.user_id),
            active=active, limit=BACKTEST_MAX_ACTIVE_PER_USER)
        raise RateLimitExceededError(
            f"You already have {active} backtests pending or running "
            f"(limit {BACKTEST_MAX_ACTIVE_PER_USER}). Wait for one to finish "
            "before starting another.")


async def _get_own_backtest(
        db: AsyncSession, backtest_id: UUID, user: CurrentUser) -> Backtest:
    backtest = await BacktestRepository(db).get_by_id(
        backtest_id, with_details=True)
    if backtest.user_id != user.user_id:
        raise NotFoundError(f"Backtest {backtest_id} not found")
    return backtest


def _detail_read(backtest: Backtest) -> BacktestDetailRead:
    return BacktestDetailRead(
        **BacktestRead.model_validate(backtest).model_dump(), metrics=(
            BacktestMetricsRead.model_validate(backtest.metrics)
            if backtest.metrics else None),
        benchmark_result=(
            BenchmarkResultRead.model_validate(backtest.benchmark_results[-1])
            if backtest.benchmark_results else None))


@router.post(
    "", status_code=201, response_model=APIResponse[BacktestRead],
    description=(
        "Backtest a ticker by replaying the recommendation tiers that "
        "triggered "
        "for it in the date range (the caller's own, or one portfolio's when "
        "`portfolio_id` is set). Returns 422 when there are none; the replay "
        "runs"
        " in the background."))
async def create_backtest(
    payload: BacktestCreate, user: CurrentUser = Depends(get_analysis_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[BacktestRead]:
    if payload.portfolio_id is not None:
        await ensure_portfolio_access(user, payload.portfolio_id)
    tiers = await RecommendationTierRepository(
        db).list_triggered_tiers_for_ticker(
            payload.ticker, start_date=payload.start_date,
            end_date=payload.end_date, portfolio_id=payload.portfolio_id,
            user_id=user.user_id if payload.portfolio_id is None else None)
    if not tiers:
        raise ValidationAppError(
            f"No triggered recommendation tiers for {payload.ticker} between "
            f"{payload.start_date} and {payload.end_date}" + (
                f" in portfolio {payload.portfolio_id}" if payload.
                portfolio_id else ""))

    await _ensure_backtest_capacity(db, user)
    backtest = BacktestRead.model_validate(
        await BacktestRepository(db).create(
            user_id=user.user_id, strategy_name=payload.strategy_name,
            benchmark_symbol=payload.benchmark_symbol,
            start_date=payload.start_date, end_date=payload.end_date,
            initial_capital=payload.initial_capital))
    await db.commit()
    from ai.workflows.backtest_workflow import run_backtest_workflow

    _start_background(
        run_backtest_workflow(
            str(backtest.id), ticker=payload.ticker, portfolio_id=(
                str(payload.portfolio_id) if payload.portfolio_id else None)))
    return APIResponse(data=backtest)


@router.post(
    "/recommendation-replay", status_code=201,
    response_model=APIResponse[BacktestRead], description=(
        "Replay every recommendation tier of the caller that has triggered. "
        "The "
        "trade universe and the date range come from that history, not from "
        "the "
        "request. Returns 422 when no tier has triggered yet; the replay runs "
        "in "
        "the background."))
async def create_recommendation_replay(
    payload: RecommendationReplayCreate,
    user: CurrentUser = Depends(get_analysis_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[BacktestRead]:
    tiers = await RecommendationTierRepository(db).list_triggered_tiers(
        user_id=user.user_id)
    trigger_dates = [
        tier.triggered_at for tier in tiers if tier.triggered_at is not None
    ]
    if not trigger_dates:
        raise ValidationAppError(
            "No triggered recommendation tiers yet to replay")

    await _ensure_backtest_capacity(db, user)
    backtest = await BacktestRepository(db).create(
        user_id=user.user_id, strategy_name=RECOMMENDATION_REPLAY_STRATEGY,
        benchmark_symbol=payload.benchmark_symbol,
        start_date=min(trigger_dates).date(),
        end_date=datetime.now(UTC).date(),
        initial_capital=payload.initial_capital)
    await db.commit()
    from ai.workflows.backtest_workflow import \
        run_recommendation_replay_workflow

    _start_background(run_recommendation_replay_workflow(str(backtest.id)))
    return APIResponse(data=BacktestRead.model_validate(backtest))


@router.post(
    "/portfolio", status_code=201, response_model=APIResponse[BacktestRead],
    description=(
        "Backtest a portfolio's actual composition over time, from its daily "
        "snapshots. Without dates the whole snapshot history is used. Returns "
        "422"
        " when the portfolio has no snapshot history yet; the backtest runs "
        "in "
        "the background."))
async def create_portfolio_backtest(
    payload: PortfolioBacktestCreate,
    user: CurrentUser = Depends(get_analysis_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[BacktestRead]:
    await ensure_portfolio_access(user, payload.portfolio_id)
    snapshots = await get_portfolio_snapshots(
        str(payload.portfolio_id), start_date=payload.start_date,
        end_date=payload.end_date)
    if len(snapshots) < 2:
        raise ValidationAppError(
            f"This portfolio has {len(snapshots)} daily snapshot(s) in the "
            "requested window; a backtest needs at least 2. Snapshots are "
            "written by the market-close job (17:30 New York time on "
            "trading days), so a new portfolio can be backtested after its "
            "second market close.")
    snapshots.sort(key=lambda s: s["snapshot_date"])
    funded = next(
        (i for i, s in enumerate(snapshots) if float(s["total_value"]) > 0),
        len(snapshots))
    if len(snapshots) - funded < 2:
        raise ValidationAppError(
            f"This portfolio has {len(snapshots) - funded} daily snapshot(s) "
            "with a positive total value in the requested window, counted "
            "from its first funded day; a backtest needs at least 2.")
    resolved_start = payload.start_date or date.fromisoformat(
        snapshots[funded]["snapshot_date"])
    resolved_end = payload.end_date or date.fromisoformat(
        snapshots[-1]["snapshot_date"])
    initial_capital = float(snapshots[funded]["total_value"])

    await _ensure_backtest_capacity(db, user)
    backtest = await BacktestRepository(db).create(
        user_id=user.user_id, strategy_name=PORTFOLIO_SNAPSHOT_STRATEGY,
        benchmark_symbol=payload.benchmark_symbol, start_date=resolved_start,
        end_date=resolved_end, initial_capital=initial_capital)
    await db.commit()
    from ai.workflows.backtest_workflow import \
        run_portfolio_backtest_workflow

    _start_background(
        run_portfolio_backtest_workflow(
            str(backtest.id), portfolio_id=str(payload.portfolio_id)))
    return APIResponse(data=BacktestRead.model_validate(backtest))


@router.get(
    "", response_model=APIResponse[list[BacktestRead]],
    description="The caller's own backtests, newest first.")
async def list_backtests(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[list[BacktestRead]]:
    backtests = await BacktestRepository(db).list_for_user(
        user.user_id, limit=limit, offset=offset)
    return APIResponse(
        data=[BacktestRead.model_validate(b) for b in backtests])


@router.get("/{backtest_id}", response_model=APIResponse[BacktestDetailRead])
async def get_backtest(
    backtest_id: UUID, user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[BacktestDetailRead]:
    backtest = await _get_own_backtest(db, backtest_id, user)
    return APIResponse(data=_detail_read(backtest))


@router.get(
    "/{backtest_id}/metrics", response_model=APIResponse[BacktestMetricsRead])
async def get_backtest_metrics(
    backtest_id: UUID, user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[BacktestMetricsRead]:
    backtest = await _get_own_backtest(db, backtest_id, user)
    if backtest.metrics is None:
        status_note = (
            " (still running)"
            if backtest.status == WorkflowStatus.RUNNING.value else "")
        raise NotFoundError(
            f"No metrics computed yet for backtest {backtest_id}{status_note}")
    return APIResponse(
        data=BacktestMetricsRead.model_validate(backtest.metrics))


@router.delete(
    "/{backtest_id}", status_code=204, description=(
        "Delete a finished backtest (completed or failed) with all its "
        "results. "
        "Another user's backtest is a 404; one still pending or running is a "
        "409."))
async def delete_backtest(
    backtest_id: UUID, background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_analysis_writer),
    db: AsyncSession = Depends(get_db, scope="function")
) -> Response:
    backtest = await _get_own_backtest(db, backtest_id, user)
    if backtest.status in (WorkflowStatus.PENDING.value,
                           WorkflowStatus.RUNNING.value):
        raise ConflictError(
            f"Backtest {backtest_id} is still {backtest.status}; delete it "
            "after it has completed or failed.")
    payload = backtest_deleted_payload(
        backtest_id=backtest.id, user_id=backtest.user_id,
        strategy_name=backtest.strategy_name)
    await BacktestRepository(db).delete(backtest)
    background_tasks.add_task(
        publish_event, EventName.BACKTEST_DELETED, payload)
    return Response(status_code=204)
