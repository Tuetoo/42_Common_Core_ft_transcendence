import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any
from uuid import UUID
import structlog
from config import settings
from observability import bind_analysis_queue, record_analysis_failed
from sqlalchemy.ext.asyncio import AsyncSession
from shared.enums import AgentRole, AnalysisFailureReason, AnalysisType

logger = structlog.get_logger(__name__)

_CANCEL_GRACE_SECONDS = 30.0


@dataclass(frozen=True)
class AnalysisJob:
    analysis_id: UUID
    analysis_type: AnalysisType = AnalysisType.FULL
    requested_agents: list[AgentRole] | None = None
    user_id: UUID | None = None


JobRunner = Callable[[AnalysisJob], Coroutine[Any, Any, None]]
FailureMarker = Callable[[UUID, str], Awaitable[bool]]
OwnerActiveCheck = Callable[[UUID], Awaitable[bool | None]]


async def _default_runner(job: AnalysisJob) -> None:
    from ai.workflows.analysis_workflow import run_analysis_workflow

    await run_analysis_workflow(
        str(job.analysis_id), analysis_type=job.analysis_type,
        requested_agents=job.requested_agents)


async def _default_mark_failed(analysis_id: UUID, reason: str) -> bool:
    from ai.workflows.analysis_workflow import mark_analysis_failed

    return await mark_analysis_failed(analysis_id, reason)


async def _default_owner_active(user_id: UUID) -> bool | None:
    if not settings.USER_STATE_CHECK_ENABLED:
        return None
    from shared.user_state import get_user_state

    state = await get_user_state(settings, user_id)
    return None if state is None else state.is_active


class AnalysisQueue:

    def __init__(
            self, *, runner: JobRunner | None = None,
            mark_failed: FailureMarker | None = None,
            timeout_seconds: float | None = None,
            owner_active: OwnerActiveCheck | None = None) -> None:
        self._runner = runner or _default_runner
        self._mark_failed = mark_failed or _default_mark_failed
        self._owner_active = owner_active or _default_owner_active
        self._timeout_override = timeout_seconds
        self._queue: asyncio.Queue[AnalysisJob] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._current: AnalysisJob | None = None

    @property
    def timeout_seconds(self) -> float:
        if self._timeout_override is not None:
            return self._timeout_override
        return float(settings.ANALYSIS_TIMEOUT_SECONDS)

    @property
    def current(self) -> AnalysisJob | None:
        return self._current

    def pending_count(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0

    def _get_queue(self) -> asyncio.Queue[AnalysisJob]:
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    def start(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        if self._worker is not None and self._worker.done():
            logger.error("analysis_queue_worker_restarted")
        self._get_queue()
        self._worker = asyncio.create_task(
            self._work(), name="analysis-queue-worker")

    async def stop(self) -> None:
        worker = self._worker
        if worker is None:
            return
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        self._worker = None

    def enqueue(self, job: AnalysisJob) -> int:
        queue = self._get_queue()
        ahead = queue.qsize() + (1 if self._current is not None else 0)
        queue.put_nowait(job)
        self.start()
        logger.info(
            "analysis_enqueued", analysis_id=str(job.analysis_id),
            jobs_ahead=ahead)
        return ahead

    async def join(self) -> None:
        await self._get_queue().join()

    async def _work(self) -> None:
        queue = self._get_queue()
        while True:
            job = await queue.get()
            self._current = job
            try:
                await self._run_one(job)
            except Exception:
                logger.exception(
                    "analysis_queue_job_handling_failed",
                    analysis_id=str(job.analysis_id))
            finally:
                self._current = None
                queue.task_done()

    async def _owner_inactive(self, job: AnalysisJob) -> bool:
        if job.user_id is None:
            return False
        try:
            active = await self._owner_active(job.user_id)
        except Exception as exc:
            logger.warning(
                "analysis_queue_owner_check_failed",
                analysis_id=str(job.analysis_id), error=str(exc))
            return False
        return active is False

    async def _run_one(self, job: AnalysisJob) -> None:
        analysis_id = str(job.analysis_id)
        if await self._owner_inactive(job):
            logger.warning(
                "analysis_queue_job_skipped_owner_inactive",
                analysis_id=analysis_id, user_id=str(job.user_id))
            await self._safe_mark_failed(
                job.analysis_id, AnalysisFailureReason.CANCELLED.value)
            return
        logger.info(
            "analysis_queue_job_started", analysis_id=analysis_id,
            timeout_seconds=self.timeout_seconds,
            queued_behind=self.pending_count())
        task: asyncio.Task[None] = asyncio.create_task(
            self._runner(job), name=f"analysis-{analysis_id}")
        try:
            done, _ = await asyncio.wait({task}, timeout=self.timeout_seconds)
        except asyncio.CancelledError:
            await _cancel_and_wait(task)
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    self._mark_failed(
                        job.analysis_id,
                        AnalysisFailureReason.CANCELLED.value))
            logger.warning(
                "analysis_queue_job_cancelled", analysis_id=analysis_id)
            raise

        if not done:
            await _cancel_and_wait(task)
            logger.error(
                "analysis_queue_job_timed_out", analysis_id=analysis_id,
                timeout_seconds=self.timeout_seconds)
            await self._safe_mark_failed(
                job.analysis_id, AnalysisFailureReason.TIMEOUT.value)
            return

        exc = (
            task.exception()
            if not task.cancelled() else asyncio.CancelledError())
        if exc is None:
            logger.info("analysis_queue_job_finished", analysis_id=analysis_id)
            return
        logger.error(
            "analysis_queue_job_errored", analysis_id=analysis_id,
            error_type=type(exc).__name__, error=str(exc))
        reason = (
            AnalysisFailureReason.CANCELLED.value if isinstance(
                exc, asyncio.CancelledError) else
            AnalysisFailureReason.WORKER_ERROR.value)
        await self._safe_mark_failed(job.analysis_id, reason)

    async def _safe_mark_failed(self, analysis_id: UUID, reason: str) -> None:
        try:
            await self._mark_failed(analysis_id, reason)
        except Exception:
            logger.exception(
                "analysis_queue_mark_failed_error",
                analysis_id=str(analysis_id), failure_reason=reason)


async def _cancel_and_wait(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.wait({task}, timeout=_CANCEL_GRACE_SECONDS)
    if not task.done():
        logger.error("analysis_queue_job_ignored_cancel", task=task.get_name())
        return
    if not task.cancelled():
        task.exception()


_ADMISSION_LOCK_KEY = 1120015


class QueueFullError(Exception):
    pass


async def check_admission(
        session: AsyncSession, *, user_id: UUID | None,
        enforce_user_cap: bool) -> None:
    from sqlalchemy import func, select, text
    from domains.analysis.model import Analysis
    from shared.enums import WorkflowStatus
    from shared.exceptions import RateLimitExceededError

    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _ADMISSION_LOCK_KEY})
    pending = (
        await session.execute(
            select(func.count()).select_from(Analysis).where(
                Analysis.status == WorkflowStatus.PENDING.value)
        )).scalar_one()
    if pending >= settings.ANALYSIS_QUEUE_MAX_PENDING:
        logger.warning("analysis_queue_full", pending=pending)
        raise QueueFullError(
            f"The analysis queue is full ({pending} pending). Try again later."
        )
    if not enforce_user_cap or user_id is None:
        return
    active = (
        await session.execute(
            select(func.count()).select_from(Analysis).where(
                Analysis.user_id == user_id).where(
                    Analysis.status.in_((
                        WorkflowStatus.PENDING.value,
                        WorkflowStatus.RUNNING.value))))).scalar_one()
    if active >= settings.ANALYSIS_MAX_ACTIVE_PER_USER:
        raise RateLimitExceededError(
            f"You already have {active} analyses pending or running "
            f"(limit {settings.ANALYSIS_MAX_ACTIVE_PER_USER}). Wait for one "
            "to finish before starting another.")


async def fail_stale_analyses_on_startup() -> int:
    from database import SessionLocal
    from domains.analysis.repository import AnalysisRepository
    from shared.events import EventName

    reason = AnalysisFailureReason.SERVICE_RESTART.value
    async with SessionLocal() as session:
        failed = await AnalysisRepository(session).fail_all_active(reason)
        await session.commit()

    from ai_events import analysis_event_payload, publish_event

    for analysis_id, ticker, user_id, portfolio_id in failed:
        record_analysis_failed(reason)
        logger.warning(
            "analysis_failed_on_startup", analysis_id=str(analysis_id),
            ticker=ticker, failure_reason=reason)
        await publish_event(
            EventName.ANALYSIS_FAILED,
            analysis_event_payload(
                analysis_id=analysis_id, user_id=user_id, ticker=ticker,
                portfolio_id=portfolio_id, failure_reason=reason,
                include_failure_reason=True))
    logger.info(
        "analysis_startup_reconciliation_complete", failed_count=len(failed))
    return len(failed)


analysis_queue = AnalysisQueue()
bind_analysis_queue(analysis_queue)
