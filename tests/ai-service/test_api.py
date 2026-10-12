import asyncio
import csv
import io
import json
import sys
import types
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4
import httpx
import main
import pytest
import pytest_asyncio
from ai.llm.base import (LLMConfigError, LLMNonEnglishOutputError,
                         LLMRateLimitError, LLMResponse, LLMUsage)
from config import settings
from helpers import (_headers, _make_decision_with_outcome,
                     _ScriptedReviewRouter)
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import delete, func, select, text
from shared.config import get_settings
from shared.enums import AgentRole, AnalysisType, TierSide, TierStatus
from shared.events import (DurableEventSubscriber, EventName,
                           publish_durable_event)
from shared.exceptions import NotFoundError, UpstreamServiceError
from shared.http_client import build_http_client
from shared.schemas import PaginationParams
from tests.alembic_contract import *  # noqa: F401,F403
from tests.auth_tokens import bearer_headers

TRADING_DAY = date(2026, 9, 29)


@pytest.fixture
def alembic_schema() -> str:
    return "ai"


def _tier_counts(**values: int) -> dict[str, int]:
    counts = {
        "checked": 0,
        "triggered": 0,
        "expired": 0,
        "no_fresh_data": 0,
        "price_lookup_failed": 0,
        "still_open": 0,
        "pending": 0,
        "execution_failed": 0,
        "closed_meanwhile": 0,
        "execution_rejected": 0
    }
    counts.update(values)
    return counts


def _review_counts(**values: int) -> dict[str, int]:
    counts = {
        "recommendations_scored": 0,
        "recommendations_attempted": 0,
        "recommendations_failed": 0,
        "decisions_evaluated": 0,
        "decisions_attempted": 0,
        "decisions_failed": 0,
        "agent_metrics_updated": 0
    }
    counts.update(values)
    return counts


def _post_response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status, json=body, request=httpx.Request("POST", "http://core/x"))


def _get_response(status: int, body: Any) -> httpx.Response:
    return httpx.Response(
        status, json=body, request=httpx.Request("GET", "http://x"))


INTERNAL_TOKEN = "internal-token-for-tests"

DAY1 = datetime(2026, 3, 2, 15, tzinfo=UTC)
DAY2 = datetime(2026, 3, 4, 15, tzinfo=UTC)
DAY3 = datetime(2026, 3, 10, 15, tzinfo=UTC)
RANGE = {"from": "2026-03-01", "to": "2026-03-15"}

TIER_KEYS = {
    "tier_id", "analysis_id", "user_id", "portfolio_id", "ticker", "action",
    "quantity", "price", "reason"
}


@pytest.fixture
def enqueued(monkeypatch):
    from domains.analysis import api as analysis_api

    jobs: list = []
    monkeypatch.setattr(
        analysis_api.analysis_queue, "enqueue",
        lambda job: jobs.append(job) or 0)
    return jobs


@pytest.fixture
def internal_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "INTERNAL_API_TOKEN", INTERNAL_TOKEN)
    return INTERNAL_TOKEN


@pytest.fixture
def ai_events_published(monkeypatch):
    import shared.events as shared_events

    published: list[tuple[str, dict]] = []

    async def _capture(settings, event_name, payload, **_kwargs):
        published.append((str(event_name), payload))

    monkeypatch.setattr(shared_events, "publish_cross_service", _capture)
    return published


@pytest_asyncio.fixture
async def committed_rows(_schema_ready):
    from database import SessionLocal
    from domains.analysis.model import Analysis

    created: list[UUID] = []

    async def _make(status: str = "pending", ticker: str = "QQQ") -> UUID:
        async with SessionLocal() as session:
            row = Analysis(
                user_id=None, portfolio_id=None, ticker=ticker, status=status)
            session.add(row)
            await session.commit()
            created.append(row.id)
            return row.id

    yield _make

    async with SessionLocal() as session:
        for analysis_id in created:
            row = await session.get(Analysis, analysis_id)
            if row is not None:
                await session.delete(row)
        await session.commit()


@pytest.fixture
def analytics_owners(monkeypatch):
    import clients.core_client as core_client

    table: dict[str, object] = {}
    calls: list[str] = []

    async def _owner(portfolio_id):
        calls.append(portfolio_id)
        entry = table.get(portfolio_id)
        if entry == "down":
            raise UpstreamServiceError("core down")
        return entry

    monkeypatch.setattr(core_client, "get_portfolio_owner", _owner)

    def _add(owner_id, visibility="private"):
        pid = uuid4()
        table[str(pid)] = {
            "portfolio_id": str(pid),
            "owner_id": str(owner_id),
            "organization_id": None,
            "visibility": visibility
        }
        return pid

    _add.table = table  # type: ignore[attr-defined]
    _add.calls = calls  # type: ignore[attr-defined]
    return _add


def _llm_response(text: str) -> LLMResponse:
    return LLMResponse(
        content=text, provider="fake", model="fake-lite",
        usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


class _ScriptedRouter:

    def __init__(self, *answers):
        self.answers = list(answers)
        self.messages: list = []

    async def complete(self, tier, messages, **kwargs):
        self.messages.append((tier, messages, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _llm_response(answer)


@pytest.fixture
def sentiment_router(monkeypatch):
    import ai.user_sentiment as us

    def _install(*answers):
        router = _ScriptedRouter(*answers)
        monkeypatch.setattr(us, "LLMRouter", lambda: router)
        return router

    return _install


async def _load(analysis_id: UUID) -> tuple[str, str | None]:
    from database import SessionLocal
    from domains.analysis.model import Analysis

    async with SessionLocal() as session:
        row = await session.get(Analysis, analysis_id)
        assert row is not None
        return row.status, row.failure_reason


def _job(analysis_id: UUID):
    from domains.analysis.queue import AnalysisJob

    return AnalysisJob(
        analysis_id=analysis_id, analysis_type=AnalysisType.TECHNICAL)


async def _noop_mark_failed(analysis_id: UUID, reason: str) -> bool:
    return False


async def _analysis(
        db_session, *, user_id=None, portfolio_id=None, with_rec=False):
    from domains.analysis.repository import (AnalysisPersistenceRepository,
                                             AnalysisRepository)

    analysis = await AnalysisRepository(db_session).create(
        user_id=user_id, portfolio_id=portfolio_id, ticker="AMD")
    if with_rec:
        await AnalysisPersistenceRepository(db_session).save_recommendation(
            analysis_id=analysis.id, recommendation="buy", confidence=0.6,
            reasoning="x", confidence_adjustment_note=None,
            debate_converged=True)
    return analysis


async def _stored_backtest(db_session, user_id, status):
    from domains.backtest.model import BacktestMetrics
    from domains.backtest.repository import BacktestRepository

    repo = BacktestRepository(db_session)
    backtest = await repo.create(
        user_id=user_id, strategy_name="portfolio_snapshot_backtest",
        benchmark_symbol="SPY", start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 10), initial_capital=1000.0)
    backtest.status = status
    if status == "completed":
        db_session.add(BacktestMetrics(backtest_id=backtest.id))
    await db_session.flush()
    return backtest.id


async def _seed_analytics(db_session, portfolio_owners):
    from domains.analysis.model import (Agent, AgentResult, AgentRun, Analysis,
                                        Recommendation)
    from domains.analytics.model import RecommendationMetric
    from domains.decision.model import InvestmentDecision

    me, other = uuid4(), uuid4()
    my_portfolio = portfolio_owners(me)
    foreign_portfolio = portfolio_owners(other)
    agent = Agent(name=f"market-{uuid4().hex[:6]}")
    db_session.add(agent)
    await db_session.flush()

    async def _analysis_row(
            user, portfolio, ticker, status, created, rec=None):
        row = Analysis(
            user_id=user, portfolio_id=portfolio, ticker=ticker, status=status,
            created_at=created)
        db_session.add(row)
        await db_session.flush()
        run = AgentRun(
            analysis_id=row.id, agent_id=agent.id,
            status="completed" if status == "completed" else "failed",
            duration_ms=1000)
        db_session.add(run)
        await db_session.flush()
        db_session.add(AgentResult(agent_run_id=run.id, score=0.5))
        rec_row = None
        if rec is not None:
            action, confidence, score = rec
            rec_row = Recommendation(
                analysis_id=row.id, recommendation=action,
                confidence=confidence, reasoning="r", debate_converged=True,
                created_at=created)
            db_session.add(rec_row)
            await db_session.flush()
            if score is not None:
                db_session.add(
                    RecommendationMetric(
                        recommendation_id=rec_row.id, accuracy_score=score))
        await db_session.flush()
        return row, rec_row

    _, buy_rec = await _analysis_row(
        me, None, "AMD", "completed", DAY1, ("buy", 0.8, 1.0))
    await _analysis_row(
        me, None, "AMD", "completed", DAY2, ("sell", 0.6, None))
    await _analysis_row(me, None, "NVDA", "failed", DAY3)
    await _analysis_row(
        None, my_portfolio, "MSFT", "completed", DAY2, ("hold", 0.5, 0.0))
    await _analysis_row(None, my_portfolio, "MSFT", "failed", DAY3)
    await _analysis_row(
        None, foreign_portfolio, "TSLA", "completed", DAY2, ("buy", 0.9, 1.0))
    await _analysis_row(
        other, None, "AAPL", "completed", DAY2, ("buy", 0.9, 1.0))
    db_session.add(
        InvestmentDecision(
            user_id=me, portfolio_id=None, recommendation_id=buy_rec.id,
            ticker="AMD", action_taken="followed", notes="=HYPERLINK(evil)",
            created_at=DAY2))
    await db_session.flush()
    return me, other, agent.name


async def _committed_analysis_with_tiers(
        *, user_id=None, portfolio_id=None, ticker="EVT",
        statuses=(TierStatus.PENDING_ACCEPTANCE.value, ),
        side=TierSide.BUY.value, window_days=5):
    from database import SessionLocal
    from domains.analysis.model import (Analysis, Recommendation,
                                        RecommendationTier)

    async with SessionLocal() as session:
        analysis = Analysis(
            user_id=user_id, portfolio_id=portfolio_id, ticker=ticker,
            status="completed")
        session.add(analysis)
        await session.flush()
        rec = Recommendation(
            analysis_id=analysis.id, recommendation="buy", confidence=0.7,
            reasoning="r", debate_converged=True,
            window_expires_at=datetime.now(UTC) + timedelta(days=window_days))
        session.add(rec)
        await session.flush()
        tiers = []
        for i, status in enumerate(statuses):
            tier = RecommendationTier(
                recommendation_id=rec.id, side=side,
                target_price=Decimal("100") - i, quantity=Decimal("2"),
                status=status)
            session.add(tier)
            tiers.append(tier)
        await session.commit()
        return analysis.id, rec.id, [t.id for t in tiers]


async def _delete_analyses_with_tiers(analysis_ids):
    from database import SessionLocal
    from domains.analysis.model import (Analysis, Recommendation,
                                        RecommendationTier)

    async with SessionLocal() as session:
        rec_ids = ((
            await session.execute(
                select(Recommendation.id).where(
                    Recommendation.analysis_id.in_(analysis_ids))
            )).scalars().all())
        if rec_ids:
            await session.execute(
                delete(RecommendationTier).where(
                    RecommendationTier.recommendation_id.in_(rec_ids)))
            await session.execute(
                delete(Recommendation).where(Recommendation.id.in_(rec_ids)))
        await session.execute(
            delete(Analysis).where(Analysis.id.in_(analysis_ids)))
        await session.commit()


async def _tier_statuses_by_id(tier_ids):
    from database import SessionLocal
    from domains.analysis.model import RecommendationTier

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(RecommendationTier.id, RecommendationTier.status).where(
                    RecommendationTier.id.in_(tier_ids)))).all()
    by_id = dict(rows)
    return [by_id[t] for t in tier_ids]


class TestAnalysisRepository:

    @pytest.mark.asyncio
    async def test_missing_id_raises_and_listing_is_paginated(
            self, db_session):
        from domains.analysis.repository import AnalysisRepository

        repo = AnalysisRepository(db_session)
        with pytest.raises(NotFoundError):
            await repo.get_by_id(uuid4())

        user_id = uuid4()
        for ticker in ["AAA", "BBB", "CCC"]:
            await repo.create(
                user_id=user_id, portfolio_id=None, ticker=ticker)

        analyses, total = await repo.list_for_user(
            user_id, PaginationParams(page=1, page_size=2))
        assert total == 3
        assert len(analyses) == 2
        assert analyses[0].created_at >= analyses[1].created_at

    @pytest.mark.asyncio
    async def test_get_latest_recommendation_for_ticker(self, _schema_ready):
        from database import SessionLocal
        from domains.analysis.repository import (AnalysisPersistenceRepository,
                                                 AnalysisRepository)

        portfolio_id = uuid4()
        async with SessionLocal() as session:
            repo = AnalysisRepository(session)
            persist = AnalysisPersistenceRepository(session)
            older = await repo.create(
                user_id=None, portfolio_id=portfolio_id, ticker="NVDA")
            await persist.save_recommendation(
                analysis_id=older.id, recommendation="hold", confidence=0.5,
                reasoning="older", confidence_adjustment_note=None,
                debate_converged=True)
            await session.commit()

        async with SessionLocal() as session:
            repo = AnalysisRepository(session)
            persist = AnalysisPersistenceRepository(session)
            newer = await repo.create(
                user_id=None, portfolio_id=portfolio_id, ticker="NVDA")
            await persist.save_recommendation(
                analysis_id=newer.id, recommendation="buy", confidence=0.8,
                reasoning="newer", confidence_adjustment_note=None,
                debate_converged=True)
            await session.commit()

        async with SessionLocal() as session:
            current_repo = AnalysisRepository(session)
            found = await current_repo.get_latest_recommendation_for_ticker(
                "nvda", portfolio_id)
            assert found is not None
            rec, analysis = found
            assert rec.recommendation == "buy"
            assert analysis.id == newer.id

            await session.delete(await current_repo.get_by_id(older.id))
            await session.delete(await current_repo.get_by_id(newer.id))
            await session.commit()

    @pytest.mark.asyncio
    async def test_save_debate_session_and_arguments(self, db_session):
        from domains.analysis.repository import (AnalysisPersistenceRepository,
                                                 AnalysisRepository)

        analysis = await AnalysisRepository(db_session).create(
            user_id=None, portfolio_id=None, ticker="GOOG")
        session_row = await AnalysisPersistenceRepository(
            db_session).save_debate_session(
                analysis_id=analysis.id, arguments=[{
                    "round_number": 1,
                    "side": "bull",
                    "content": "growth",
                    "citations": ["src-1"]
                }, {
                    "round_number": 1,
                    "side": "bear",
                    "content": "valuation",
                    "citations": []
                }])
        await db_session.flush()
        await db_session.refresh(session_row, attribute_names=["arguments"])
        assert len(session_row.arguments) == 2


class TestAnalysisService:

    @pytest.mark.asyncio
    async def test_create_returns_pending_and_missing_recommendation_raises(
            self, db_session):
        from domains.analysis.repository import AnalysisRepository
        from domains.analysis.service import (AnalysisService,
                                              recommendation_read)

        result = await AnalysisService(db_session).create_analysis(
            user_id=uuid4(), portfolio_id=None, ticker="amd")
        assert result.status == "pending"
        assert result.ticker == "AMD"

        repo = AnalysisRepository(db_session)
        analysis = await repo.create(
            user_id=None, portfolio_id=None, ticker="INTC")
        loaded = await repo.get_by_id(analysis.id, with_details=True)
        with pytest.raises(NotFoundError):
            recommendation_read(loaded)

    @pytest.mark.asyncio
    async def test_get_analysis_detail_assembles_agent_runs_and_recommendation(
            self, db_session):
        from domains.analysis.repository import (AgentCatalogRepository,
                                                 AnalysisPersistenceRepository,
                                                 AnalysisRepository)
        from domains.analysis.service import AnalysisService

        analysis = await AnalysisRepository(db_session).create(
            user_id=None, portfolio_id=None, ticker="ORCL")
        agent = await AgentCatalogRepository(db_session).get_or_create(
            AgentRole.RISK.value)
        persist = AnalysisPersistenceRepository(db_session)
        await persist.save_agent_run(
            analysis_id=analysis.id, agent_id=agent.id, status="completed",
            started_at=None, completed_at=None, duration_ms=None, score=0.6,
            summary="ok", output=None)
        await persist.save_recommendation(
            analysis_id=analysis.id, recommendation="sell", confidence=0.4,
            reasoning="risk too high", confidence_adjustment_note="note",
            debate_converged=False)

        detail = await AnalysisService(db_session).get_analysis_detail(
            analysis.id)
        assert len(detail.agent_runs) == 1
        assert detail.agent_runs[0].agent_name == AgentRole.RISK.value
        assert detail.recommendation is not None
        assert detail.recommendation.ticker == "ORCL"
        assert detail.recommendation.recommendation == "sell"


class TestRunAnalysisWorkflow:

    @pytest.mark.asyncio
    async def test_full_workflow_persists_and_completes(
            self, _schema_ready, mock_cross_service):
        from ai.workflows.analysis_workflow import run_analysis_workflow
        from database import SessionLocal
        from domains.analysis.repository import AnalysisRepository

        async with SessionLocal() as setup_session:
            analysis = await AnalysisRepository(setup_session).create(
                user_id=None, portfolio_id=None, ticker="AAPL")
            await setup_session.commit()
            analysis_id = analysis.id

        await run_analysis_workflow(
            str(analysis_id), analysis_type=AnalysisType.TECHNICAL,
            requested_agents=None)

        async with SessionLocal() as verify_session:
            repo = AnalysisRepository(verify_session)
            persisted = await repo.get_by_id(analysis_id, with_details=True)
            completed = persisted.status == "completed"
            if completed:
                assert persisted.failure_reason is None
                assert len(persisted.recommendations) == 1
            else:
                assert (persisted.status,
                        persisted.failure_reason) == ("failed", "no_evidence")
                assert persisted.recommendations == []
            assert {r.agent.name
                    for r in persisted.agent_runs
                    } == {AgentRole.MARKET.value, AgentRole.TECHNICAL.value}

            from domains.memory.repository import EpisodicMemoryRepository

            memory = await EpisodicMemoryRepository(
                verify_session).get_by_analysis_id(analysis_id)
            if memory is not None:
                await verify_session.delete(memory)
            await verify_session.delete(persisted)
            await verify_session.commit()

        event_names = [name for name, _ in mock_cross_service]
        if not completed:
            assert event_names == ["analysis.started", "analysis.failed"]
            assert mock_cross_service[1][1]["failure_reason"] == "no_evidence"
            return
        assert event_names == [
            "analysis.started", "analysis.completed", "recommendation.ready"
        ]

        payloads_by_event = dict(mock_cross_service)
        assert payloads_by_event["analysis.completed"]["user_id"] == ""
        assert payloads_by_event["recommendation.ready"]["user_id"] is None

    @pytest.mark.asyncio
    async def test_workflow_exception_records_workflow_error(
            self, committed_rows, mock_cross_service, monkeypatch):
        import ai.workflows.analysis_workflow as wf

        analysis_id = await committed_rows("pending", ticker="ERR")

        async def failing_run_and_persist(**_kwargs) -> None:
            raise RuntimeError("graph exploded")

        monkeypatch.setattr(wf, "_run_and_persist", failing_run_and_persist)
        await wf.run_analysis_workflow(str(analysis_id))

        assert await _load(analysis_id) == ("failed", "workflow_error")
        assert [n for n, _ in mock_cross_service
                ] == ["analysis.started", "analysis.failed"]
        assert mock_cross_service[1][1]["failure_reason"] == "workflow_error"

    @pytest.mark.asyncio
    async def test_already_failed_row_is_not_rerun(
            self, committed_rows, mock_cross_service, monkeypatch):
        import ai.workflows.analysis_workflow as wf

        analysis_id = await committed_rows("failed", ticker="DEAD")
        called = False

        async def fake_run_and_persist(**_kwargs) -> None:
            nonlocal called
            called = True

        monkeypatch.setattr(wf, "_run_and_persist", fake_run_and_persist)
        await wf.run_analysis_workflow(str(analysis_id))

        assert called is False
        assert await _load(analysis_id) == ("failed", None)
        assert mock_cross_service == []


class TestAnalysisQueue:

    @pytest.mark.asyncio
    async def test_enqueue_restarts_a_dead_worker(self):
        from domains.analysis.queue import AnalysisQueue

        ran: list[UUID] = []

        async def runner(job) -> None:
            ran.append(job.analysis_id)

        queue = AnalysisQueue(
            runner=runner, mark_failed=_noop_mark_failed, timeout_seconds=5)
        queue.start()
        await queue.stop()
        first, second = uuid4(), uuid4()
        try:
            queue.enqueue(_job(first))
            queue.enqueue(_job(second))
            await asyncio.wait_for(queue.join(), timeout=5)
        finally:
            await queue.stop()
        assert ran == [first, second]

    @pytest.mark.asyncio
    async def test_timeout_cancels_marks_failed_publishes_and_continues(
            self, committed_rows, mock_cross_service):
        from domains.analysis.queue import AnalysisQueue, _default_mark_failed

        slow_id = await committed_rows("running", ticker="SLOW")
        next_id = await committed_rows("pending", ticker="NEXT")
        cancelled = asyncio.Event()
        ran: list[UUID] = []

        async def runner(job) -> None:
            ran.append(job.analysis_id)
            if job.analysis_id == slow_id:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        queue = AnalysisQueue(
            runner=runner, mark_failed=_default_mark_failed,
            timeout_seconds=0.2)
        try:
            queue.enqueue(_job(slow_id))
            queue.enqueue(_job(next_id))
            await asyncio.wait_for(queue.join(), timeout=10)
        finally:
            await queue.stop()

        assert cancelled.is_set()
        assert ran == [slow_id, next_id]
        assert await _load(slow_id) == ("failed", "timeout")
        failed_events = [
            p for name, p in mock_cross_service if name == "analysis.failed"
        ]
        assert failed_events == [{
            "analysis_id": str(slow_id),
            "user_id": "",
            "ticker": "SLOW",
            "portfolio_id": None,
            "failure_reason": "timeout"
        }]

    @pytest.mark.asyncio
    async def test_worker_survives_a_failing_job_and_a_failing_marker(
            self, committed_rows, mock_cross_service):
        from domains.analysis.queue import AnalysisQueue, _default_mark_failed

        bad_id = await committed_rows("running", ticker="BAD")
        good_id = await committed_rows("pending", ticker="GOOD")
        ran: list[UUID] = []

        async def runner(job) -> None:
            ran.append(job.analysis_id)
            if job.analysis_id == bad_id:
                raise RuntimeError("boom")

        queue = AnalysisQueue(
            runner=runner, mark_failed=_default_mark_failed, timeout_seconds=5)
        try:
            queue.enqueue(_job(bad_id))
            queue.enqueue(_job(good_id))
            await asyncio.wait_for(queue.join(), timeout=10)
            assert queue._worker is not None and not queue._worker.done()
        finally:
            await queue.stop()

        assert ran == [bad_id, good_id]
        assert await _load(bad_id) == ("failed", "worker_error")
        assert await _load(good_id) == ("pending", None)

        ran = []
        first, second = uuid4(), uuid4()

        async def failing_runner(job) -> None:
            ran.append(job.analysis_id)
            if job.analysis_id == first:
                raise RuntimeError("boom")

        async def broken_mark_failed(analysis_id: UUID, reason: str) -> bool:
            raise ConnectionError("db down")

        queue = AnalysisQueue(
            runner=failing_runner, mark_failed=broken_mark_failed,
            timeout_seconds=5)
        try:
            queue.enqueue(_job(first))
            queue.enqueue(_job(second))
            await asyncio.wait_for(queue.join(), timeout=5)
        finally:
            await queue.stop()
        assert ran == [first, second]

    @pytest.mark.asyncio
    async def test_stop_cancels_in_flight_and_marks_it_cancelled(
            self, committed_rows, mock_cross_service):
        from domains.analysis.queue import AnalysisQueue, _default_mark_failed

        analysis_id = await committed_rows("running", ticker="STOP")
        started = asyncio.Event()

        async def runner(job) -> None:
            started.set()
            await asyncio.sleep(3600)

        queue = AnalysisQueue(
            runner=runner, mark_failed=_default_mark_failed,
            timeout_seconds=60)
        queue.enqueue(_job(analysis_id))
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.wait_for(queue.stop(), timeout=10)

        assert await _load(analysis_id) == ("failed", "cancelled")


class TestStartupReconciliation:

    @pytest.mark.asyncio
    async def test_marks_only_pending_and_running_rows_failed(
            self, committed_rows, monkeypatch):
        import shared.events
        from domains.analysis.queue import fail_stale_analyses_on_startup

        published: list[tuple[str, dict]] = []

        async def fake_publish(settings, event_name, payload, **_kw):
            published.append((event_name, payload))

        monkeypatch.setattr(
            shared.events, "publish_cross_service", fake_publish)

        pending_id = await committed_rows("pending", ticker="PEND")
        running_id = await committed_rows("running", ticker="RUNN")
        completed_id = await committed_rows("completed", ticker="DONE")
        failed_id = await committed_rows("failed", ticker="FAIL")

        count = await fail_stale_analyses_on_startup()

        assert count >= 2
        assert await _load(pending_id) == ("failed", "service_restart")
        assert await _load(running_id) == ("failed", "service_restart")
        assert await _load(completed_id) == ("completed", None)
        assert await _load(failed_id) == ("failed", None)
        mine = {
            p["analysis_id"]: p
            for name, p in published
            if name == "analysis.failed" and p["analysis_id"] in
            {str(pending_id), str(running_id)}
        }
        assert mine[str(pending_id)] == {
            "analysis_id": str(pending_id),
            "user_id": "",
            "ticker": "PEND",
            "portfolio_id": None,
            "failure_reason": "service_restart"
        }
        assert mine[str(running_id)]["ticker"] == "RUNN"

    @pytest.mark.asyncio
    async def test_lifespan_reconciles_before_starting_worker(
            self, monkeypatch):
        import main

        calls: list[str] = []

        async def _anoop(*_a, **_kw):
            return 0

        class _FakeSubscriber:

            async def run(self) -> None:
                await asyncio.sleep(3600)

        class _FakeEngine:

            async def dispose(self) -> None:
                calls.append("engine_dispose")

        async def fake_reconcile() -> int:
            calls.append("reconcile")
            return 0

        def fake_start() -> None:
            calls.append("start")

        async def fake_stop() -> None:
            calls.append("stop")

        async def fake_reconcile_backtests() -> int:
            calls.append("reconcile_backtests")
            return 0

        async def fake_stop_backtests() -> None:
            calls.append("stop_backtests")

        for name in ("ensure_schema", "ensure_collection",
                     "rebuild_bm25_from_qdrant", "close_redis_connections",
                     "close_http_clients", "close_qdrant_client"):
            monkeypatch.setattr(main, name, _anoop)
        monkeypatch.setattr(main, "close_storage_clients", lambda: None)
        monkeypatch.setattr(main, "configure_logging", lambda _s: None)
        monkeypatch.setattr(main, "build_durable_subscriber", _FakeSubscriber)
        monkeypatch.setattr(main, "engine", _FakeEngine())
        monkeypatch.setattr(
            main, "fail_stale_analyses_on_startup", fake_reconcile)
        monkeypatch.setattr(
            main, "fail_stale_backtests_on_startup", fake_reconcile_backtests)
        monkeypatch.setattr(
            main, "stop_background_backtests", fake_stop_backtests)
        monkeypatch.setattr(main.analysis_queue, "start", fake_start)
        monkeypatch.setattr(main.analysis_queue, "stop", fake_stop)

        async with main.lifespan(main.app):
            assert calls == ["reconcile", "reconcile_backtests", "start"]
        assert calls == [
            "reconcile", "reconcile_backtests", "start", "stop",
            "stop_backtests", "engine_dispose"
        ]


class TestCreateAnalysis:

    @pytest.mark.asyncio
    async def test_requested_agents_need_a_research_agent(
            self, client, enqueued):
        cases = {
            "empty-list": ([], 422),
            "debate-only": (["bull", "bear"], 422),
            "portfolio-only": (["portfolio"], 422),
            "one-research-agent": (["bull", "macro"], 201)
        }
        for case_id, (agents, status) in cases.items():
            enqueued.clear()
            response = await client.post(
                "/api/v1/analyses", headers=_headers(uuid4()), json={
                    "ticker": "AMD",
                    "requested_agents": agents
                })
            assert response.status_code == status, case_id
            assert len(enqueued) == (1 if status == 201 else 0), case_id

    @pytest.mark.asyncio
    async def test_portfolio_must_be_accessible(
            self, client, enqueued, owners, monkeypatch):
        import clients.auth_client as auth_client

        me, other = uuid4(), uuid4()
        mine, theirs = owners(me), owners(other)
        down = uuid4()
        owners.table[down] = "down"

        ok = await client.post(
            "/api/v1/analyses", headers=_headers(me), json={
                "ticker": "AMD",
                "portfolio_id": str(mine)
            })
        foreign = await client.post(
            "/api/v1/analyses", headers=_headers(me), json={
                "ticker": "AMD",
                "portfolio_id": str(theirs)
            })
        missing = await client.post(
            "/api/v1/analyses", headers=_headers(me), json={
                "ticker": "AMD",
                "portfolio_id": str(uuid4())
            })
        unavailable = await client.post(
            "/api/v1/analyses", headers=_headers(me), json={
                "ticker": "AMD",
                "portfolio_id": str(down)
            })

        assert ok.status_code == 201
        assert foreign.status_code == 404
        assert missing.status_code == 404
        assert unavailable.status_code == 503
        assert len(enqueued) == 1

        org, member = uuid4(), uuid4()

        async def _orgs(user_id):
            return [org]

        monkeypatch.setattr(auth_client, "list_user_organization_ids", _orgs)
        shared = owners(uuid4(), org_id=org, visibility="organization")
        private = owners(uuid4(), org_id=org, visibility="private")

        shared_resp = await client.post(
            "/api/v1/analyses", headers=_headers(member), json={
                "ticker": "AMD",
                "portfolio_id": str(shared)
            })
        denied = await client.post(
            "/api/v1/analyses", headers=_headers(member), json={
                "ticker": "AMD",
                "portfolio_id": str(private)
            })
        assert shared_resp.status_code == 403
        assert denied.status_code == 404

    @pytest.mark.asyncio
    async def test_internal_create_enqueues_and_needs_no_token(
            self, client, enqueued):
        body_user = uuid4()
        created = await client.post(
            "/internal/analyses", json={
                "ticker": "amd",
                "user_id": str(body_user)
            })
        assert created.status_code == 201
        data = created.json()["data"]
        assert data["user_id"] == str(body_user)
        assert len(enqueued) == 1

        ref = await client.get(f"/internal/analyses/{data['id']}")
        detail = await client.get(
            f"/internal/analyses/{data['id']}/detail",
            headers={"X-Request-ID": "ai-trace-1"})
        assert ref.status_code == 200 and ref.json()["status"] == "pending"
        assert detail.status_code == 200
        assert detail.json()["request_id"] == "ai-trace-1"
        assert created.json()["request_id"] == created.headers["x-request-id"]
        assert detail.json()["data"]["id"] == data["id"]
        assert detail.json()["data"]["agent_runs"] == []

        response = await client.post(
            "/internal/analyses", json={
                "ticker": "msft",
                "analysis_type": "technical"
            })
        assert response.status_code == 201
        body = response.json()["data"]
        assert body["status"] == "pending"
        assert body["failure_reason"] is None
        assert len(enqueued) == 2
        assert str(enqueued[1].analysis_id) == body["id"]
        assert enqueued[1].analysis_type == AnalysisType.TECHNICAL

    @pytest.mark.asyncio
    async def test_global_queue_cap_returns_503_for_everyone(
            self, client, db_session, enqueued, monkeypatch):
        pending = (
            await db_session.execute(
                text(
                    "SELECT count(*) FROM ai.analyses WHERE status = 'pending'"
                ))).scalar_one()
        monkeypatch.setattr(
            settings, "ANALYSIS_QUEUE_MAX_PENDING", pending + 1)
        monkeypatch.setattr(settings, "ANALYSIS_MAX_ACTIVE_PER_USER", 100)
        ok = await client.post(
            "/api/v1/analyses", headers=_headers(uuid4()),
            json={"ticker": "AMD"})
        full = await client.post(
            "/api/v1/analyses", headers=_headers(uuid4()),
            json={"ticker": "AMD"})
        internal = await client.post(
            "/internal/analyses", json={"ticker": "AMD"})
        assert ok.status_code == 201
        assert full.status_code == 503
        assert full.headers.get("retry-after") == "60"
        assert internal.status_code == 503
        assert len(enqueued) == 1


class TestInternalCreateForUser:

    @pytest.mark.asyncio
    async def test_user_must_have_portfolio_access_and_an_active_writer_role(
            self, client, monkeypatch, enqueued):
        import api.internal as internal_api
        import clients.core_client as core_client
        from shared.user_state import UserState

        me, other = uuid4(), uuid4()
        mine, theirs = uuid4(), uuid4()
        owners = {
            str(mine): {
                "owner_id": str(me),
                "organization_id": None,
                "visibility": "private"
            },
            str(theirs): {
                "owner_id": str(other),
                "organization_id": None,
                "visibility": "private"
            }
        }

        async def _owner(pid):
            return owners.get(pid)

        monkeypatch.setattr(core_client, "get_portfolio_owner", _owner)
        ok = await client.post(
            "/internal/analyses", json={
                "ticker": "AMD",
                "user_id": str(me),
                "portfolio_id": str(mine)
            })
        assert ok.status_code == 201
        assert ok.json()["data"]["user_id"] == str(me)
        denied = await client.post(
            "/internal/analyses", json={
                "ticker": "AMD",
                "user_id": str(me),
                "portfolio_id": str(theirs)
            })
        assert denied.status_code == 404
        system = await client.post(
            "/internal/analyses", json={
                "ticker": "AMD",
                "portfolio_id": str(theirs)
            })
        assert system.status_code == 201
        assert system.json()["data"]["user_id"] is None
        assert len(enqueued) == 2

        states: dict[str, UserState | None] = {}

        async def _state(_settings, user_id):
            return states.get(str(user_id))

        monkeypatch.setattr(settings, "USER_STATE_CHECK_ENABLED", True)
        monkeypatch.setattr(internal_api, "get_user_state", _state)
        guest, inactive, unknown = uuid4(), uuid4(), uuid4()
        states[str(guest)] = UserState(role="guest", is_active=True)
        states[str(inactive)] = UserState(role="user", is_active=False)
        for user_id in (guest, inactive):
            r = await client.post(
                "/internal/analyses", json={
                    "ticker": "AMD",
                    "user_id": str(user_id)
                })
            assert r.status_code == 403, r.text
        r = await client.post(
            "/internal/analyses", json={
                "ticker": "AMD",
                "user_id": str(unknown)
            })
        assert r.status_code == 201
        assert len(enqueued) == 3


class TestReadAccess:

    @pytest.mark.asyncio
    async def test_analyses_are_readable_by_owner_or_portfolio_access(
            self, client, db_session, owners):
        owner, stranger = uuid4(), uuid4()
        analysis = await _analysis(db_session, user_id=owner, with_rec=True)
        base = f"/api/v1/analyses/{analysis.id}"

        assert (await client.get(base)).status_code == 401
        assert (
            await client.get(base, headers=_headers(owner))).status_code == 200
        for suffix in ("", "/recommendation", "/debate", "/reflection"):
            response = await client.get(
                base + suffix, headers=_headers(stranger))
            assert response.status_code == 404, suffix
        rec = await client.get(
            base + "/recommendation", headers=_headers(owner))
        assert rec.status_code == 200

        pid = owners(owner)
        on_portfolio = await _analysis(db_session, portfolio_id=pid)
        no_portfolio = await _analysis(db_session)

        url = f"/api/v1/analyses/{on_portfolio.id}"
        assert (
            await client.get(url, headers=_headers(owner))).status_code == 200
        assert (
            await client.get(url,
                             headers=_headers(stranger))).status_code == 404
        orphan = f"/api/v1/analyses/{no_portfolio.id}"
        assert (
            await client.get(orphan,
                             headers=_headers(owner))).status_code == 404

    @pytest.mark.asyncio
    async def test_decision_routes_are_owner_only(self, client, db_session):
        from domains.analysis.repository import AnalysisRepository
        from domains.decision.repository import (DecisionReviewRepository,
                                                 InvestmentDecisionRepository)

        owner, stranger = uuid4(), uuid4()
        analysis = await _analysis(db_session, user_id=owner, with_rec=True)
        loaded = await AnalysisRepository(db_session).get_by_id(
            analysis.id, with_details=True)
        decision = await InvestmentDecisionRepository(db_session).create(
            user_id=owner, portfolio_id=None,
            recommendation_id=loaded.recommendations[-1].id, ticker="AMD",
            action_taken="ignored", notes=None)
        await DecisionReviewRepository(db_session).create(
            decision_id=decision.id, review_text="r", lessons_learned=None)

        base = f"/api/v1/decisions/{decision.id}"
        assert (await client.get(base + "/review")).status_code == 401
        for method, suffix in (("get", "/outcome"), ("get", "/review"),
                               ("post", "/review")):
            response = await getattr(client, method)(
                base + suffix, headers=_headers(stranger))
            assert response.status_code == 404, suffix
        mine = await client.get(base + "/review", headers=_headers(owner))
        assert mine.status_code == 200

    @pytest.mark.asyncio
    async def test_failed_review_keeps_the_old_one_and_new_one_replaces(
            self, client, db_session, monkeypatch):
        import domains.decision.service as decision_service
        from domains.decision.model import DecisionReview
        from domains.decision.repository import DecisionReviewRepository

        class _Failing:

            async def complete(self, tier, messages, **kwargs):
                raise LLMConfigError("no key configured")

        cases = {
            "llm-error": None,
            "malformed-json": "not json",
            "no-review-text": json.dumps({"lessons_learned": "x"})
        }
        for case_id, content in cases.items():
            decision = await _make_decision_with_outcome(db_session)
            await DecisionReviewRepository(db_session).create(
                decision_id=decision.id, review_text="earlier review",
                lessons_learned=None)
            router = (
                _Failing()
                if content is None else _ScriptedReviewRouter(content))
            monkeypatch.setattr(
                decision_service, "LLMRouter", lambda r=router: r)
            url = f"/api/v1/decisions/{decision.id}/review"
            headers = _headers(decision.user_id)

            response = await client.post(url, headers=headers)

            assert response.status_code == 503, case_id
            assert response.headers["Retry-After"] == "60", case_id
            error_code = response.json()["error"]["code"]
            assert error_code == "review_unavailable", case_id
            kept = await client.get(url, headers=headers)
            review_text = kept.json()["data"]["review_text"]
            assert review_text == "earlier review", case_id

        decision = await _make_decision_with_outcome(db_session)
        url = f"/api/v1/decisions/{decision.id}/review"
        headers = _headers(decision.user_id)
        for text_ in ("first review", "second review"):
            router = _ScriptedReviewRouter(
                json.dumps({
                    "review_text": text_,
                    "lessons_learned": None
                }))
            monkeypatch.setattr(
                decision_service, "LLMRouter", lambda r=router: r)
            response = await client.post(url, headers=headers)
            assert response.status_code == 201

        rows = (
            await db_session.execute(
                select(DecisionReview.review_text).where(
                    DecisionReview.decision_id == decision.id)
            )).scalars().all()
        assert rows == ["second review"]

    @pytest.mark.asyncio
    async def test_guest_reads_are_allowed(self, client, monkeypatch):
        import clients.core_client as core_client

        async def _no_owner(portfolio_id):
            return None

        monkeypatch.setattr(core_client, "get_portfolio_owner", _no_owner)
        guest = bearer_headers(uuid4(), role="guest")
        assert (
            await client.get("/api/v1/analyses",
                             headers=guest)).status_code == 200
        assert (
            await client.get("/api/v1/decisions",
                             headers=guest)).status_code == 200
        overview = await client.get(
            "/api/v1/analytics/overview", headers=guest)
        assert overview.status_code == 200

    @pytest.mark.asyncio
    async def test_member_cannot_decide_on_a_shared_system_analysis(
            self, client, db_session, monkeypatch):
        import clients.auth_client as auth_client
        import clients.core_client as core_client
        from domains.analysis.model import (Analysis, Recommendation,
                                            RecommendationTier)

        alice, bob, org = uuid4(), uuid4(), uuid4()
        portfolio = uuid4()

        async def _owner(portfolio_id):
            return {
                "portfolio_id": portfolio_id,
                "owner_id": str(alice),
                "organization_id": str(org),
                "visibility": "organization"
            }

        async def _orgs(user_id):
            return [org]

        monkeypatch.setattr(core_client, "get_portfolio_owner", _owner)
        monkeypatch.setattr(auth_client, "list_user_organization_ids", _orgs)
        analysis = Analysis(
            user_id=None, portfolio_id=portfolio, ticker="SHR",
            status="completed")
        db_session.add(analysis)
        await db_session.flush()
        rec = Recommendation(
            analysis_id=analysis.id, recommendation="buy", confidence=0.6,
            reasoning="r", debate_converged=True,
            window_expires_at=datetime.now(UTC) + timedelta(days=3))
        db_session.add(rec)
        await db_session.flush()
        tier = RecommendationTier(
            recommendation_id=rec.id, side="buy", target_price=Decimal("10"),
            quantity=Decimal("1"), status=TierStatus.PENDING_ACCEPTANCE.value)
        db_session.add(tier)
        await db_session.flush()

        read = await client.get(
            f"/api/v1/analyses/{analysis.id}", headers=bearer_headers(bob))
        assert read.status_code == 200
        denied = await client.post(
            f"/api/v1/analyses/{analysis.id}/decisions",
            headers=bearer_headers(bob), json={"action_taken": "followed"})
        assert denied.status_code == 403
        await db_session.refresh(tier)
        assert tier.status == TierStatus.PENDING_ACCEPTANCE.value

        accepted = await client.post(
            f"/api/v1/analyses/{analysis.id}/decisions",
            headers=bearer_headers(alice), json={"action_taken": "followed"})
        assert accepted.status_code == 201, accepted.text


class TestBacktestRoutes:

    @pytest.mark.asyncio
    async def test_access_reads_and_deletes_are_owner_only(
            self, client, owners, db_session, monkeypatch):
        import domains.backtest.api as backtest_api
        from domains.backtest.model import Backtest, BacktestMetrics
        from domains.backtest.repository import BacktestRepository

        async def _no_call(*args, **kwargs):
            raise AssertionError(
                "snapshots must not be read for a foreign portfolio")

        monkeypatch.setattr(backtest_api, "get_portfolio_snapshots", _no_call)
        me = uuid4()
        theirs = owners(uuid4())
        response = await client.post(
            "/api/v1/backtests/portfolio", headers=_headers(me),
            json={"portfolio_id": str(theirs)})
        assert response.status_code == 404

        owner = uuid4()
        backtest = await BacktestRepository(db_session).create(
            user_id=owner, strategy_name="s", benchmark_symbol="SPY",
            start_date=date(2026, 1, 1), end_date=date(2026, 2, 1),
            initial_capital=1000.0)
        system = await BacktestRepository(db_session).create(
            user_id=None, strategy_name="s", benchmark_symbol="SPY",
            start_date=date(2026, 1, 1), end_date=date(2026, 2, 1),
            initial_capital=1000.0)
        url = f"/api/v1/backtests/{backtest.id}"
        assert (await client.get(url)).status_code == 401
        assert (
            await client.get(url, headers=_headers(owner))).status_code == 200
        stranger = _headers(uuid4())
        assert (await client.get(url, headers=stranger)).status_code == 404
        assert (
            await client.get(url + "/metrics",
                             headers=stranger)).status_code == 404
        sys_url = f"/api/v1/backtests/{system.id}"
        assert (
            await client.get(sys_url,
                             headers=_headers(owner))).status_code == 404

        for status in ("completed", "failed"):
            me = uuid4()
            backtest_id = await _stored_backtest(db_session, me, status)
            response = await client.delete(
                f"/api/v1/backtests/{backtest_id}", headers=_headers(me))
            assert response.status_code == 204, response.text
            assert await db_session.get(Backtest, backtest_id) is None
            orphans = (
                await db_session.execute(
                    select(func.count()).select_from(BacktestMetrics).where(
                        BacktestMetrics.backtest_id == backtest_id)
                )).scalar_one()
            assert orphans == 0, status
            again = await client.get(
                f"/api/v1/backtests/{backtest_id}", headers=_headers(me))
            assert again.status_code == 404, status

        backtest_id = await _stored_backtest(db_session, uuid4(), "completed")
        response = await client.delete(
            f"/api/v1/backtests/{backtest_id}",
            headers=bearer_headers(uuid4(), role="guest"))
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_fewer_than_two_snapshots_is_refused_up_front(
            self, client, owners, monkeypatch):
        import domains.backtest.api as backtest_api

        me = uuid4()
        pid = owners(me)
        started: list = []
        monkeypatch.setattr(backtest_api, "_start_background", started.append)
        for count in (0, 1):

            async def _snapshots(*args, count=count, **kwargs):
                return [{
                    "snapshot_date": "2026-09-29",
                    "total_value": 100.0
                }][:count]

            monkeypatch.setattr(
                backtest_api, "get_portfolio_snapshots", _snapshots)
            response = await client.post(
                "/api/v1/backtests/portfolio", headers=_headers(me),
                json={"portfolio_id": str(pid)})
            assert response.status_code == 422, response.text
            message = response.json()["error"]["message"]
            assert f"has {count} daily snapshot(s)" in message
            assert "second market close" in message
            assert started == []

        async def _unfunded_first(*args, **kwargs):
            return [{
                "snapshot_date": "2026-09-28",
                "total_value": 0.0
            }, {
                "snapshot_date": "2026-09-29",
                "total_value": 100.0
            }]

        monkeypatch.setattr(
            backtest_api, "get_portfolio_snapshots", _unfunded_first)
        response = await client.post(
            "/api/v1/backtests/portfolio", headers=_headers(me),
            json={"portfolio_id": str(pid)})
        assert response.status_code == 422, response.text
        assert "positive total value" in response.json()["error"]["message"]
        assert started == []

    @pytest.mark.asyncio
    async def test_create_commits_before_starting_task(
            self, client, db_session, monkeypatch):
        import domains.backtest.api as backtest_api
        from domains.analysis.repository import RecommendationTierRepository

        order: list[str] = []

        async def _tiers(self, *args, **kwargs):
            return [object()]

        real_commit = db_session.commit

        async def _commit():
            order.append("commit")
            await real_commit()

        def _start(coro):
            order.append("start")
            coro.close()

        monkeypatch.setattr(
            RecommendationTierRepository, "list_triggered_tiers_for_ticker",
            _tiers)
        monkeypatch.setattr(db_session, "commit", _commit)
        monkeypatch.setattr(backtest_api, "_start_background", _start)

        response = await client.post(
            "/api/v1/backtests", headers=_headers(uuid4()), json={
                "strategy_name": "s",
                "ticker": "AMD",
                "start_date": "2026-01-01",
                "end_date": "2026-02-01",
                "initial_capital": 1000
            })
        assert response.status_code == 201
        assert order == ["commit", "start"]

    @pytest.mark.asyncio
    async def test_reserved_strategy_name_is_422(self, client):
        reserved = ("ai_recommendation_replay", "portfolio_snapshot_backtest")
        for name in reserved:
            response = await client.post(
                "/api/v1/backtests", headers=_headers(uuid4()), json={
                    "strategy_name": name,
                    "ticker": "AMD",
                    "start_date": "2026-01-01",
                    "end_date": "2026-02-01",
                    "initial_capital": 1000
                })
            assert response.status_code == 422, name

    @pytest.mark.asyncio
    async def test_create_refused_above_active_cap(
            self, client, db_session, monkeypatch):
        import domains.backtest.api as backtest_api
        from domains.analysis.repository import RecommendationTierRepository
        from domains.backtest.model import Backtest
        from domains.backtest.repository import BacktestRepository
        from shared.enums import WorkflowStatus

        async def _tiers(self, *args, **kwargs):
            return [object()]

        def _start(coro):
            coro.close()

        monkeypatch.setattr(
            RecommendationTierRepository, "list_triggered_tiers_for_ticker",
            _tiers)
        monkeypatch.setattr(backtest_api, "_start_background", _start)

        user = uuid4()
        other = uuid4()
        body = {
            "strategy_name": "s",
            "ticker": "AMD",
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "initial_capital": 1000
        }
        cap = backtest_api.BACKTEST_MAX_ACTIVE_PER_USER
        for _ in range(cap):
            response = await client.post(
                "/api/v1/backtests", headers=bearer_headers(user), json=body)
            assert response.status_code == 201

        refused = await client.post(
            "/api/v1/backtests", headers=bearer_headers(user), json=body)
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "rate_limit_exceeded"
        assert (
            await client.post(
                "/api/v1/backtests", headers=bearer_headers(other),
                json=body)).status_code == 201

        repo = BacktestRepository(db_session)
        assert await repo.count_active_for_user(user) == cap
        row = (
            await db_session.execute(
                select(Backtest).where(Backtest.user_id == user).limit(1)
            )).scalar_one()
        row.status = WorkflowStatus.FAILED.value
        await db_session.commit()
        assert await repo.count_active_for_user(user) == cap - 1
        assert (
            await client.post(
                "/api/v1/backtests", headers=bearer_headers(user),
                json=body)).status_code == 201


class TestTriggeredTiers:

    @pytest.mark.asyncio
    async def test_triggered_tiers_scope_to_one_user_and_to_a_window(
            self, db_session):
        from domains.analysis.model import (Analysis, Recommendation,
                                            RecommendationTier)
        from domains.analysis.repository import RecommendationTierRepository

        mine, other = uuid4(), uuid4()
        ids = {}
        for user in (mine, other):
            analysis = Analysis(
                user_id=user, portfolio_id=uuid4(), ticker="RPL",
                status="completed")
            db_session.add(analysis)
            await db_session.flush()
            rec = Recommendation(
                analysis_id=analysis.id, recommendation="buy", confidence=0.6,
                reasoning="r", debate_converged=True)
            db_session.add(rec)
            await db_session.flush()
            tier = RecommendationTier(
                recommendation_id=rec.id, side="buy",
                target_price=Decimal("5"), quantity=Decimal("1"),
                status=TierStatus.TRIGGERED.value,
                triggered_at=datetime.now(UTC), triggered_price=Decimal("5"))
            db_session.add(tier)
            await db_session.flush()
            ids[user] = tier.id

        repo = RecommendationTierRepository(db_session)
        scoped = {t.id for t in await repo.list_triggered_tiers(user_id=mine)}
        assert ids[mine] in scoped and ids[other] not in scoped
        everything = {t.id for t in await repo.list_triggered_tiers()}
        assert {ids[mine], ids[other]} <= everything

        user = uuid4()
        analysis = Analysis(
            user_id=user, portfolio_id=uuid4(), ticker="WIN",
            status="completed")
        db_session.add(analysis)
        await db_session.flush()
        rec = Recommendation(
            analysis_id=analysis.id, recommendation="buy", confidence=0.6,
            reasoning="r", debate_converged=True)
        db_session.add(rec)
        await db_session.flush()
        now = datetime.now(UTC)
        window_ids = {}
        for label, when in (("old", now - timedelta(days=40)), ("inside",
                                                                now)):
            tier = RecommendationTier(
                recommendation_id=rec.id, side="buy",
                target_price=Decimal("5"), quantity=Decimal("1"),
                status=TierStatus.TRIGGERED.value, triggered_at=when,
                triggered_price=Decimal("5"))
            db_session.add(tier)
            await db_session.flush()
            window_ids[label] = tier.id

        found = {
            t.id
            for t in await repo.list_triggered_tiers(
                user_id=user, start_date=(
                    now - timedelta(days=5)).date(), end_date=now.date())
        }
        assert found == {window_ids["inside"]}


class TestAnalyticsOverview:

    @pytest.mark.asyncio
    async def test_filters_scope_all_and_core_outage(
            self, client, db_session, analytics_owners):
        me, _, _ = await _seed_analytics(db_session, analytics_owners)
        by_ticker = await client.get(
            "/api/v1/analytics/overview", headers=_headers(me), params={
                **RANGE, "ticker": "amd"
            })
        assert by_ticker.json()["data"]["total_analyses"] == 2
        by_action = await client.get(
            "/api/v1/analytics/overview", headers=_headers(me), params={
                **RANGE, "action": "sell"
            })
        assert by_action.json()["data"]["total_analyses"] == 1
        by_status = await client.get(
            "/api/v1/analytics/overview", headers=_headers(me), params={
                **RANGE, "status": "failed"
            })
        assert by_status.json()["data"]["status_counts"] == {"failed": 2}

        denied = await client.get(
            "/api/v1/analytics/overview", headers=_headers(me), params={
                **RANGE, "scope": "all"
            })
        assert denied.status_code == 403
        admin = await client.get(
            "/api/v1/analytics/overview",
            headers=bearer_headers(uuid4(), "admin"), params={
                **RANGE, "scope": "all"
            })
        assert admin.status_code == 200
        assert admin.json()["data"]["total_analyses"] >= 7

        for pid in list(analytics_owners.table):
            analytics_owners.table[pid] = "down"
        response = await client.get(
            "/api/v1/analytics/overview", headers=_headers(me), params=RANGE)
        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_bad_input_is_422(self, client):
        bad = [{
            "from": "2026-03-10",
            "to": "2026-03-01"
        }, {
            "from": "2023-01-01",
            "to": "2026-03-01"
        }, {
            "from": "not-a-date"
        }, {
            "action": "short"
        }, {
            "status": "exploded"
        }, {
            "scope": "everyone"
        }, {
            "ticker": "BAD TICKER"
        }]
        for params in bad:
            response = await client.get(
                "/api/v1/analytics/overview", headers=_headers(uuid4()),
                params=params)
            assert response.status_code == 422, params


class TestAnalyticsTimeseries:

    @pytest.mark.asyncio
    async def test_daily_series_is_zero_filled_and_weekly_series_aggregates(
            self, client, db_session, analytics_owners):
        me, _, _ = await _seed_analytics(db_session, analytics_owners)
        response = await client.get(
            "/api/v1/analytics/timeseries", headers=_headers(me), params={
                **RANGE, "metric": "analyses",
                "interval": "day"
            })
        points = response.json()["data"]["points"]
        assert len(points) == 15
        by_day = {p["period_start"]: p for p in points}
        assert by_day["2026-03-02"]["value"] == 1.0
        assert by_day["2026-03-04"]["count"] == 2
        assert by_day["2026-03-10"]["count"] == 2
        assert by_day["2026-03-03"] == {
            "period_start": "2026-03-03",
            "value": 0.0,
            "count": 0
        }

        confidence = await client.get(
            "/api/v1/analytics/timeseries", headers=_headers(me), params={
                **RANGE, "metric": "confidence",
                "interval": "week"
            })
        points = confidence.json()["data"]["points"]
        assert [p["period_start"]
                for p in points] == ["2026-02-23", "2026-03-02", "2026-03-09"]
        assert points[1]["count"] == 3
        assert points[1]["value"] == pytest.approx(0.6333, abs=1e-3)
        assert points[2] == {
            "period_start": "2026-03-09",
            "value": None,
            "count": 0
        }
        accuracy = await client.get(
            "/api/v1/analytics/timeseries", headers=_headers(me), params={
                **RANGE, "metric": "accuracy",
                "interval": "week"
            })
        week = accuracy.json()["data"]["points"][1]
        assert week == {"period_start": "2026-03-02", "value": 0.5, "count": 2}


class TestAnalyticsExport:

    @pytest.mark.asyncio
    async def test_csv_exports_for_each_dataset(
            self, client, db_session, analytics_owners):
        me, _, _ = await _seed_analytics(db_session, analytics_owners)
        response = await client.get(
            "/api/v1/analytics/export.csv", headers=_headers(me), params={
                **RANGE, "dataset": "analyses"
            })
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        assert response.headers["x-export-truncated"] == "false"
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert len(rows) == 5
        assert {r["ticker"] for r in rows} == {"AMD", "NVDA", "MSFT"}
        assert {r["owner"] for r in rows} == {"user", "system"}
        assert rows[0]["created_at"] >= rows[-1]["created_at"]

        recs = await client.get(
            "/api/v1/analytics/export.csv", headers=_headers(me), params={
                **RANGE, "dataset": "recommendations",
                "action": "buy"
            })
        rows = list(csv.DictReader(io.StringIO(recs.text)))
        assert [(r["ticker"], r["action"], r["accuracy_score"])
                for r in rows] == [("AMD", "buy", "1.0")]
        decisions = await client.get(
            "/api/v1/analytics/export.csv", headers=_headers(me), params={
                **RANGE, "dataset": "decisions"
            })
        rows = list(csv.DictReader(io.StringIO(decisions.text)))
        assert len(rows) == 1
        assert rows[0]["action_taken"] == "followed"
        assert rows[0]["recommended_action"] == "buy"
        assert rows[0]["notes"] == "'=HYPERLINK(evil)"


class TestSentiment:

    @pytest.mark.asyncio
    async def test_bad_input_is_422_and_llm_unavailable_is_503(
            self, client, sentiment_router):
        bodies = [{
            "text": ""
        }, {
            "text": "   "
        }, {}, {
            "text": "x",
            "kind": "tweet"
        }]
        for body in bodies:
            response = await client.post("/internal/sentiment", json=body)
            assert response.status_code == 422, body

        errors = {
            "rate-limited": LLMRateLimitError("quota"),
            "non-english": LLMNonEnglishOutputError("x", reason="script")
        }
        for case_id, error in errors.items():
            sentiment_router(error)
            response = await client.post(
                "/internal/sentiment", json={"text": "hi"})
            assert response.status_code == 503, case_id

    @pytest.mark.asyncio
    async def test_retry_truncation_and_label_helpers(
            self, client, sentiment_router, monkeypatch):
        from ai.user_sentiment import label_for_score, truncate_text

        router = sentiment_router("not json", '{"score": -0.5}')
        ok = await client.post("/internal/sentiment", json={"text": "hmm"})
        assert ok.json()["label"] == "negative"
        assert len(router.messages) == 2
        sentiment_router("nope", '{"label": "positive"}')
        failed = await client.post("/internal/sentiment", json={"text": "hmm"})
        assert failed.status_code == 503

        monkeypatch.setattr(settings, "SENTIMENT_MAX_INPUT_CHARS", 100)
        router = sentiment_router('{"score": 0.2}')
        text = ("word " * 5000).strip()
        response = await client.post(
            "/internal/sentiment", json={"text": text})
        assert response.status_code == 200
        sent = router.messages[0][1][1].content
        body = sent.removeprefix("<text>\n").removesuffix("\n</text>")
        assert len(body) <= 100
        assert not body.endswith(" wor")

        assert truncate_text("  short  ", 100) == "short"
        assert truncate_text("abcdefghij", 5) == "abcde"
        assert label_for_score(0.149) == "neutral"


class TestInternalRoutes:

    @pytest.mark.asyncio
    async def test_stats_are_flat_integer_counts(self, client, db_session):
        from domains.analysis.model import Analysis, Recommendation
        from domains.backtest.repository import BacktestRepository

        before = (await client.get("/internal/stats")).json()
        for status in ("completed", "failed", "pending"):
            db_session.add(Analysis(ticker="STA", status=status))
        await db_session.flush()
        done = Analysis(ticker="STA", status="completed")
        db_session.add(done)
        await db_session.flush()
        db_session.add(
            Recommendation(
                analysis_id=done.id, recommendation="buy", confidence=0.5,
                reasoning="r", debate_converged=True))
        await BacktestRepository(db_session).create(
            user_id=None, strategy_name="s", benchmark_symbol="SPY",
            start_date=date(2026, 1, 1), end_date=date(2026, 2,
                                                       1), initial_capital=1.0)
        await db_session.flush()

        response = await client.get("/internal/stats")
        assert response.status_code == 200
        after = response.json()
        assert set(after) == {
            "analyses_total", "analyses_completed", "analyses_failed",
            "recommendations", "backtests"
        }
        assert all(isinstance(v, int) for v in after.values())
        delta = {k: after[k] - before[k] for k in after}
        assert delta == {
            "analyses_total": 4,
            "analyses_completed": 2,
            "analyses_failed": 1,
            "recommendations": 1,
            "backtests": 1
        }

    @pytest.mark.asyncio
    async def test_closed_days_are_skipped_and_today_uses_new_york(
            self, client, monkeypatch):
        from datetime import datetime as real_datetime
        import ai.workflows.tier_monitoring_workflow as tm
        import api.internal as internal
        import shared.market_calendar as market_calendar

        async def _must_not_run():
            raise AssertionError(
                "tier monitoring must not run on a closed day")

        real_market_today = internal._market_today
        monkeypatch.setattr(tm, "run_tier_monitoring_workflow", _must_not_run)
        for day in (date(2026, 11, 26), date(2026, 12, 25), date(2026, 10, 3)):
            monkeypatch.setattr(internal, "_market_today", lambda d=day: d)
            response = await client.post("/internal/scheduler/tier-monitoring")
            assert response.status_code == 200, day
            assert response.json() == {
                "skipped": True,
                "reason": "non_trading_day",
                "date": day.isoformat()
            }, day

        class _Clock(real_datetime):

            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 9, 30, 2, 0,
                                     tzinfo=UTC).astimezone(tz)

        monkeypatch.setattr(internal, "_market_today", real_market_today)
        monkeypatch.setattr(market_calendar, "datetime", _Clock)
        assert internal._market_today() == date(2026, 9, 29)

    @pytest.mark.asyncio
    async def test_job_status_follows_failed_units(self, client, monkeypatch):
        import ai.workflows.review_workflow as rw
        import ai.workflows.tier_monitoring_workflow as tm
        import api.internal as internal

        cases = {
            "every-unit-failed-or-stale": (
                _tier_counts(checked=3, execution_failed=2,
                             no_fresh_data=1), 500),
            "all-stale": (_tier_counts(checked=3, no_fresh_data=3), 500),
            "some-still-open": (
                _tier_counts(checked=3, execution_failed=2,
                             still_open=1), 200),
            "closed-meanwhile": (
                _tier_counts(checked=2, closed_meanwhile=2), 200),
            "nothing-checked": (_tier_counts(), 200),
            "pending-tiers-are-not-units": (
                _tier_counts(checked=6, pending=5,
                             price_lookup_failed=1), 500),
            "one-tier-processed": (
                _tier_counts(checked=3, price_lookup_failed=2,
                             expired=1), 200),
            "only-pending-tiers": (_tier_counts(checked=4, pending=4), 200)
        }
        monkeypatch.setattr(internal, "_market_today", lambda: TRADING_DAY)
        for case_id, (counts, status) in cases.items():

            async def _run(counts=counts):
                return dict(counts)

            monkeypatch.setattr(tm, "run_tier_monitoring_workflow", _run)
            response = await client.post("/internal/scheduler/tier-monitoring")
            assert response.status_code == status, case_id
            body = response.json()
            for key, value in counts.items():
                assert body[key] == value, case_id
            if status == 500:
                assert body["all_units_failed"] == ["tiers"], case_id
            else:
                assert "all_units_failed" not in body, case_id

        cases = {
            "filings-failed-for-every-ticker": ((2, ["AAA", "BBB"]), (2, []),
                                                (True,
                                                 False), 500, ["filings"]),
            "news-failed-for-every-ticker": ((2, []), (2, ["AAA", "BBB"]),
                                             (True, False), 500, ["news"]),
            "some-tickers-failed": ((2, ["BBB"]), (2, ["AAA"]), (True, False),
                                    200, None),
            "sources-not-configured-or-quota-deferred": ((0, []), (0, []),
                                                         (False,
                                                          False), 200, None),
            "macro-failed": ((2, []), (2, []), (True, True), 500, ["macro"])
        }
        for case_id, case in cases.items():
            filings, news, macro, status, steps = case
            per_ticker = {
                "filings": {
                    "attempted": filings[0],
                    "failed_tickers": filings[1]
                },
                "news": {
                    "attempted": news[0],
                    "failed_tickers": news[1]
                }
            }
            macro_result = {
                "documents_created": 0,
                "attempted": macro[0],
                "failed": macro[1]
            }

            async def _tickers(per_ticker=per_ticker):
                return dict(per_ticker)

            async def _macro(macro_result=macro_result):
                return dict(macro_result)

            fake = types.ModuleType("ai.workflows.auto_ingestion_workflow")
            fake.__dict__.update(
                run_auto_ingestion_workflow=_tickers,
                run_macro_auto_ingestion_workflow=_macro)
            monkeypatch.setitem(
                sys.modules, "ai.workflows.auto_ingestion_workflow", fake)
            response = await client.post("/internal/scheduler/rag-ingest")
            assert response.status_code == status, case_id
            body = response.json()
            assert body["per_ticker"] == per_ticker, case_id
            assert body["macro"] == macro_result, case_id
            assert body.get("all_units_failed") == steps, case_id

        cases = {
            "all-recommendations-failed": (
                _review_counts(
                    recommendations_attempted=2,
                    recommendations_failed=2), 500, ["recommendations"]),
            "all-decisions-failed": (
                _review_counts(
                    decisions_attempted=3, decisions_failed=3,
                    recommendations_attempted=1,
                    recommendations_scored=1), 500, ["decisions"]),
            "some-recommendations-failed": (
                _review_counts(
                    recommendations_attempted=2, recommendations_failed=1,
                    recommendations_scored=1), 200, None),
            "nothing-to-review": (_review_counts(), 200, None)
        }
        for case_id, (counts, status, steps) in cases.items():

            async def _run(counts=counts):
                return dict(counts)

            monkeypatch.setattr(rw, "run_review_workflow", _run)
            response = await client.post("/internal/review/run")
            assert response.status_code == status, case_id
            body = response.json()
            for key, value in counts.items():
                assert body[key] == value, case_id
            assert body.get("all_units_failed") == steps, case_id

    @pytest.mark.asyncio
    async def test_fx_rate_survives_a_redis_error(self, client, monkeypatch):
        import ai.providers.registry as registry
        import api.internal as internal

        class _Down:

            async def get(self, key):
                raise ConnectionError("redis down")

            async def set(self, *args, **kwargs):
                raise ConnectionError("redis down")

        class _Provider:

            async def get_current_price(self, ticker):
                return SimpleNamespace(price=Decimal("0.79"))

        monkeypatch.setattr(internal, "get_cache_client", lambda _s: _Down())
        monkeypatch.setattr(
            registry, "get_market_data_provider", lambda: _Provider())
        response = await client.get(
            "/internal/fx-rate", params={
                "from_currency": "usd",
                "to_currency": "gbp"
            })
        assert response.status_code == 200, response.text
        assert response.json() == {"rate": 0.79}

    def test_internal_routes_not_in_openapi(self):
        import main

        paths = main.app.openapi()["paths"]
        assert not [p for p in paths if p.startswith("/internal")]


class TestHealth:

    @pytest.mark.asyncio
    async def test_ok_when_all_dependencies_up(self, client, monkeypatch):
        dependencies = {
            "database": "check_database_connection",
            "redis": "check_redis_connection",
            "qdrant": "check_qdrant_connection",
            "storage": "check_storage_connection"
        }

        async def _up(*_args, **_kwargs) -> bool:
            return True

        for func_name in dependencies.values():
            monkeypatch.setattr(main, func_name, _up)
        response = await client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"]
        for key in dependencies:
            assert body[key] is True


class _FakeResponse:
    status = 200

    def __init__(self, body: str = "{}") -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestInternalToken:

    @pytest.mark.asyncio
    async def test_token_is_required_unless_empty_in_development(
            self, client, internal_token, monkeypatch):
        url = f"/internal/analyses/{uuid4()}"
        client.headers.pop("X-Internal-Token", None)
        assert (await client.get(url)).status_code == 401

        monkeypatch.setattr(get_settings(), "INTERNAL_API_TOKEN", "")
        monkeypatch.setattr(get_settings(), "APP_ENV", "development")
        assert (await client.get(url)).status_code == 404

    @pytest.mark.asyncio
    async def test_service_clients_send_the_header(
            self, monkeypatch, internal_token):
        from clients import auth_client, core_client

        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=[])

        fake = build_http_client(3.0, transport=httpx.MockTransport(handler))
        monkeypatch.setattr(
            core_client, "get_http_client", lambda timeout=5.0: fake)
        monkeypatch.setattr(
            auth_client, "get_http_client", lambda timeout=5.0: fake)
        await core_client.get_portfolio_holdings(str(uuid4()))
        await core_client.get_tracked_tickers()
        await auth_client.list_user_organization_ids(str(uuid4()))
        assert len(seen) == 3
        for request in seen:
            assert request.url.path.startswith("/internal/")
            assert request.headers.get("X-Internal-Token") == INTERNAL_TOKEN
        await fake.aclose()

    def test_operator_scripts_send_the_header(
            self, monkeypatch, internal_token):
        from scripts import analysis_cli, smoke_check

        captured = []

        def fake_urlopen(request, timeout=None):
            captured.append(request)
            return _FakeResponse(json.dumps({"data": {"id": "x"}}))

        monkeypatch.setenv("INTERNAL_API_TOKEN", INTERNAL_TOKEN)
        monkeypatch.setattr(
            analysis_cli.urllib.request, "urlopen", fake_urlopen)
        assert analysis_cli.main(["create", "AAPL"]) == 0
        assert captured[0].get_header("X-internal-token") == INTERNAL_TOKEN

        monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", INTERNAL_TOKEN)
        captured.clear()
        monkeypatch.setattr(
            smoke_check.urllib.request, "urlopen", fake_urlopen)
        smoke_check._http("GET", "/internal/analyses/abc")
        smoke_check._http("GET", "/health")
        assert captured[0].get_header("X-internal-token") == INTERNAL_TOKEN
        assert captured[1].get_header("X-internal-token") is None


class TestCoreClient:

    async def _record(self, monkeypatch, response):
        import clients.core_client as cc

        class _FakePostClient:

            def __init__(self, response: httpx.Response | Exception):
                self.response = response
                self.calls: list[dict] = []

            async def post(self, url, json):
                self.calls.append(json)
                if isinstance(self.response, Exception):
                    raise self.response
                return self.response

        fake = _FakePostClient(response)
        monkeypatch.setattr(cc, "get_http_client", lambda timeout: fake)
        result = await cc.record_transaction(
            "p1", transaction_type="buy", ticker="AMD", quantity=1.0,
            price=10.0, transaction_date="2026-09-29",
            idempotency_key="tier:abc")
        return result, fake

    @pytest.mark.asyncio
    async def test_record_transaction_key_rejections_and_transient_errors(
            self, monkeypatch):
        from clients.core_client import TransactionRejectedError

        result, fake = await self._record(
            monkeypatch, _post_response(200, {"id": "t1"}))
        assert result == "t1"
        assert fake.calls[0]["idempotency_key"] == "tier:abc"

        for status in (400, 404, 409, 422):
            with pytest.raises(TransactionRejectedError) as exc:
                await self._record(
                    monkeypatch,
                    _post_response(
                        status, {
                            "error": {
                                "code": "x",
                                "message": "Insufficient cash"
                            }
                        }))
            assert exc.value.status_code == status
            assert exc.value.detail == "Insufficient cash", status

        failures = {
            "429": _post_response(429, {}),
            "500": _post_response(500, {}),
            "503": _post_response(503, {}),
            "timeout": httpx.ReadTimeout("slow")
        }
        for case_id, failure in failures.items():
            with pytest.raises(UpstreamServiceError) as upstream:
                await self._record(monkeypatch, failure)
            assert not isinstance(
                upstream.value, TransactionRejectedError), case_id

        expected = (
            "Core-service refused a transaction for a business reason (4xx "
            "other than 408/429: insufficient cash, not enough shares held, "
            "409 conflict, unknown portfolio). Retrying the same order the "
            "next day will not change the answer, unlike a 5xx/timeout.")
        assert "default_message" in vars(TransactionRejectedError)
        assert TransactionRejectedError.default_message == expected

    @pytest.mark.asyncio
    async def test_get_helpers_and_cache_failure(self, monkeypatch):
        import clients.core_client as core_client

        class _FakeGetClient:

            def __init__(self, response: httpx.Response | Exception):
                self.response = response
                self.calls: list[tuple[str, dict[str, Any]]] = []

            async def get(self, url: str, **kwargs: Any) -> httpx.Response:
                self.calls.append((url, kwargs))
                if isinstance(self.response, Exception):
                    raise self.response
                return self.response

        fake = _FakeGetClient(_get_response(404, {"detail": "no"}))
        monkeypatch.setattr(
            core_client, "get_http_client", lambda timeout=3.0: fake)
        assert await core_client.get_portfolio_owner("p1") is None
        assert fake.calls[0][0].endswith("/internal/portfolios/p1/owner")

        fake.response = _get_response(500, {})
        with pytest.raises(UpstreamServiceError,
                           match="resolving owner of p1"):
            await core_client.get_portfolio_owner("p1")

        fake.response = _get_response(200, ["a.txt"])
        assert await core_client.get_existing_document_filenames(
            None, "news") == ["a.txt"]
        assert fake.calls[-1][1] == {"params": {"category": "news"}}

        fake.response = httpx.ConnectError("down")
        with pytest.raises(UpstreamServiceError,
                           match="resolving tracked tickers"):
            await core_client.get_tracked_tickers()

        class _BrokenCache:

            async def get(self, key):
                raise RedisConnectionError("down")

            async def set(self, *args, **kwargs):
                raise RedisConnectionError("down")

        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return [{"ticker": "AMD"}]

        class _Http:

            async def get(self, url, **kwargs):
                return _Response()

        monkeypatch.setattr(
            core_client, "get_cache_client", lambda settings: _BrokenCache())
        monkeypatch.setattr(
            core_client, "get_http_client", lambda timeout: _Http())
        assert await core_client.get_portfolio_holdings("p1") == [{
            "ticker": "AMD"
        }]


class TestSchemas:

    def test_ticker_types_keep_the_shared_rule(self):
        from domains.analysis.schema import AnalysisCreate
        from domains.rag.schema import RagAskRequest

        assert AnalysisCreate(ticker=" brk.b ").ticker == "BRK.B"
        with pytest.raises(ValidationError, match="Invalid ticker"):
            AnalysisCreate(ticker="!!")
        assert RagAskRequest(question="q", ticker="  ").ticker is None
        assert RagAskRequest(question="q", ticker="amd").ticker == "AMD"


class TestEvents:

    @pytest.mark.asyncio
    async def test_portfolio_deleted_cancels_unexecuted_tiers_once(
            self, _schema_ready, ai_events_published):
        import event_listener

        subscriber = event_listener.build_durable_subscriber()
        assert EventName.PORTFOLIO_DELETED in subscriber._handlers

        owner, portfolio = uuid4(), uuid4()
        analysis_id, rec_id, tier_ids = await _committed_analysis_with_tiers(
            user_id=None, portfolio_id=portfolio, ticker="DEL", statuses=(
                TierStatus.PENDING_ACCEPTANCE.value,
                TierStatus.ACCEPTED_AWAITING_TRIGGER.value,
                TierStatus.TRIGGERED.value))
        other_analysis, _, other_tiers = await _committed_analysis_with_tiers(
            portfolio_id=uuid4(), ticker="DEL")
        payload = {"portfolio_id": str(portfolio), "user_id": str(owner)}
        try:
            await event_listener._handle_portfolio_deleted(payload)
            assert await _tier_statuses_by_id(tier_ids) == [
                TierStatus.EXPIRED.value, TierStatus.EXPIRED.value,
                TierStatus.TRIGGERED.value
            ]
            assert await _tier_statuses_by_id(other_tiers) == [
                TierStatus.PENDING_ACCEPTANCE.value
            ]
            assert [n
                    for n, _ in ai_events_published] == ["tier.cancelled"] * 2
            for _, event in ai_events_published:
                assert set(event) == TIER_KEYS
                assert event["reason"] == "portfolio_deleted"
                assert event["user_id"] == str(owner)
                assert event["portfolio_id"] == str(portfolio)
            assert {e["tier_id"]
                    for _, e in ai_events_published
                    } == {str(t)
                          for t in tier_ids[:2]}

            from database import SessionLocal
            from domains.analysis.model import Recommendation

            async with SessionLocal() as session:
                note = (
                    await session.get(Recommendation,
                                      rec_id)).tier_generation_note
            assert "portfolio was deleted" in note

            ai_events_published.clear()
            await event_listener._handle_portfolio_deleted(payload)
            assert ai_events_published == []
        finally:
            await _delete_analyses_with_tiers([analysis_id, other_analysis])

    @pytest.mark.asyncio
    async def test_malformed_event_payloads_are_ignored(
            self, ai_events_published, monkeypatch):
        import ai.workflows.rag_ingestion_workflow as wf
        import event_listener

        await event_listener._handle_portfolio_deleted(
            {"portfolio_id": "nope"})
        assert ai_events_published == []

        called: list[Any] = []

        async def _run(**kwargs: Any) -> None:
            called.append(kwargs)

        monkeypatch.setattr(wf, "run_rag_ingestion_workflow", _run)
        await event_listener._handle_document_uploaded({
            "document_id": str(uuid4()),
            "storage_bucket": "b"
        })
        assert called == []

    @pytest.mark.asyncio
    async def test_backtest_publish_helper_never_raises(
            self, _schema_ready, ai_events_published):
        import ai.workflows.backtest_workflow as wf

        await wf.publish_backtest_finished(uuid4(), completed=True)
        assert ai_events_published == []


class TestDurableEvents:

    @staticmethod
    def _unique_event_name() -> str:
        return f"test.event.{uuid4().hex[:8]}"

    @pytest.mark.asyncio
    async def test_messages_published_before_a_subscriber_exists_are_kept(
            self):
        event_name = self._unique_event_name()
        await publish_durable_event(settings, event_name, {"n": 1})
        await publish_durable_event(settings, event_name, {"n": 2})

        received: list[int] = []

        async def handler(payload: dict) -> None:
            received.append(payload["n"])

        subscriber = DurableEventSubscriber(
            settings, consumer_name="test-worker")
        subscriber.on(event_name, handler)

        task = asyncio.create_task(subscriber.run())
        try:
            for _ in range(50):
                if received == [1, 2]:
                    break
                await asyncio.sleep(0.1)
        finally:
            task.cancel()

        assert received == [1, 2]

    @pytest.mark.asyncio
    async def test_handler_exception_leaves_message_pending_not_lost(self):
        event_name = self._unique_event_name()
        await publish_durable_event(settings, event_name, {"n": 1})

        async def crashing_handler(payload: dict) -> None:
            raise RuntimeError("simulated crash")

        subscriber = DurableEventSubscriber(
            settings, consumer_name="test-worker")
        subscriber.on(event_name, crashing_handler)

        task = asyncio.create_task(subscriber.run())
        await asyncio.sleep(0.5)
        task.cancel()

        from shared.redis import get_queue_client

        client = get_queue_client(settings)
        pending = await client.xpending(f"stream:{event_name}", "workers")
        assert pending["pending"] == 1

    @pytest.mark.asyncio
    async def test_permanently_failing_message_is_dead_lettered(self):
        from redis.exceptions import ResponseError
        import shared.events as events_module
        from shared.redis import get_queue_client

        event_name = self._unique_event_name()
        stream_name = f"stream:{event_name}"
        await publish_durable_event(settings, event_name, {"n": 1})

        attempt_count = 0

        async def always_failing_handler(payload: dict) -> None:
            nonlocal attempt_count
            attempt_count += 1
            raise RuntimeError("permanent failure")

        client = get_queue_client(settings)
        try:
            await client.xgroup_create(
                stream_name, events_module._CONSUMER_GROUP, id="0",
                mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

        subscriber = DurableEventSubscriber(
            settings, consumer_name="test-worker")
        subscriber.on(event_name, always_failing_handler)

        resp = await client.xreadgroup(
            events_module._CONSUMER_GROUP, "test-worker", {stream_name: ">"},
            count=1)
        message_id, fields = resp[0][1][0]
        await subscriber._handle_one(
            client, stream_name, event_name, always_failing_handler,
            message_id, fields)

        for _ in range(events_module._MAX_DELIVERY_ATTEMPTS + 2):
            if attempt_count >= events_module._MAX_DELIVERY_ATTEMPTS:
                break
            cursor = "0-0"
            while True:
                cursor, claimed, _ = await client.xautoclaim(
                    stream_name, events_module._CONSUMER_GROUP, "test-worker",
                    min_idle_time=0, start_id=cursor, count=50)
                for claimed_id, claimed_fields in claimed:
                    await subscriber._handle_one(
                        client, stream_name, event_name,
                        always_failing_handler, claimed_id, claimed_fields)
                if cursor == "0-0":
                    break

        assert attempt_count == events_module._MAX_DELIVERY_ATTEMPTS
        pending = await client.xpending(
            stream_name, events_module._CONSUMER_GROUP)
        assert pending["pending"] == 0
