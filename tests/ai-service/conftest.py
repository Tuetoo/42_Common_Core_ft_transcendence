import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4
import pytest
import pytest_asyncio
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AI_SERVICE_DIR = _REPO_ROOT / "services" / "ai-service"

sys.path.insert(0, str(_AI_SERVICE_DIR))
sys.path.insert(0, str(_REPO_ROOT))

TEST_INTERNAL_API_TOKEN = "test-internal-token"

_pg: PostgresContainer | None = None
_redis: RedisContainer | None = None


def pytest_configure(config):
    global _pg, _redis
    _pg = PostgresContainer("postgres:16-alpine")
    _redis = RedisContainer("redis:7-alpine")
    _pg.start()
    _redis.start()

    os.environ["POSTGRES_HOST"] = _pg.get_container_host_ip()
    os.environ["POSTGRES_PORT"] = str(_pg.get_exposed_port(5432))
    os.environ["POSTGRES_USER"] = _pg.username
    os.environ["POSTGRES_PASSWORD"] = _pg.password
    os.environ["POSTGRES_DB"] = _pg.dbname
    os.environ["REDIS_HOST"] = _redis.get_container_host_ip()
    os.environ["REDIS_PORT"] = str(_redis.get_exposed_port(6379))
    os.environ["JWT_SECRET"] = "test-secret-not-for-production-0123456789"
    os.environ["INTERNAL_API_TOKEN"] = TEST_INTERNAL_API_TOKEN
    os.environ["USER_STATE_CHECK_ENABLED"] = "false"


def pytest_unconfigure(config):
    if _redis is not None:
        _redis.stop()
    if _pg is not None:
        _pg.stop()


@pytest_asyncio.fixture(scope="session")
async def _schema_ready():
    from config import settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    admin_engine = create_async_engine(settings.assemble_database_url())
    async with admin_engine.begin() as conn:
        await conn.execute(
            text(f"CREATE SCHEMA IF NOT EXISTS {settings.POSTGRES_SCHEMA}"))
    await admin_engine.dispose()

    from database import register_models

    register_models()
    from database import engine as app_engine
    from shared.database import Base

    async with app_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield
    await app_engine.dispose()
    from shared.redis import close_redis_connections

    await close_redis_connections()


@pytest_asyncio.fixture
async def db_session(_schema_ready):
    from database import engine
    from sqlalchemy.ext.asyncio import AsyncSession

    async with engine.connect() as connection:
        await connection.begin()
        session = AsyncSession(
            bind=connection, expire_on_commit=False,
            join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            await session.close()
            await connection.rollback()


@pytest_asyncio.fixture
async def client(db_session, mock_cross_service):
    import main
    from database import get_db
    from httpx import ASGITransport, AsyncClient

    async def _override_get_db():
        yield db_session

    main.app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"X-Internal-Token": TEST_INTERNAL_API_TOKEN
                                    }) as ac:
        yield ac
    main.app.dependency_overrides.clear()


@pytest.fixture
def mock_cross_service(monkeypatch):
    import clients.core_client as core_client
    import shared.events as shared_events

    async def _fake_get_portfolio_holdings(portfolio_id):
        return []

    published_events: list[tuple[str, dict]] = []

    async def _fake_publish_cross_service(settings, event_name, payload):
        published_events.append((event_name, payload))

    monkeypatch.setattr(
        core_client, "get_portfolio_holdings", _fake_get_portfolio_holdings)
    monkeypatch.setattr(
        shared_events, "publish_cross_service", _fake_publish_cross_service)
    return published_events


@pytest.fixture(autouse=True)
def _no_agent_prefetch(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "AGENT_PREFETCH_ENABLED", False)


@pytest.fixture
def clean_pacer(monkeypatch):
    from ai.llm.base import request_pacer
    from config import settings

    request_pacer.reset()
    for name in ("LLM_GOOGLE_CHEAP_MIN_INTERVAL_SECONDS",
                 "LLM_GOOGLE_HIGHEST_ACCURACY_MIN_INTERVAL_SECONDS",
                 "LLM_GOOGLE_LITE_POOL_MIN_DISPATCH_INTERVAL_SECONDS"):
        monkeypatch.setattr(settings, name, 0.0)
    yield
    request_pacer.reset()


@pytest.fixture
def ingestion_sources(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "SEC_EDGAR_USER_AGENT", "Test t@x.com")
    monkeypatch.setattr(settings, "NEWSAPI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "FRED_API_KEY", "test-key")
    return settings


@pytest.fixture
def owners(monkeypatch):
    import clients.core_client as core_client
    from shared.exceptions import UpstreamServiceError

    table: dict[UUID, object] = {}

    async def _fake_owner(portfolio_id: str):
        entry = table.get(UUID(portfolio_id))
        if entry == "down":
            raise UpstreamServiceError("core-service down")
        return entry

    monkeypatch.setattr(core_client, "get_portfolio_owner", _fake_owner)

    def _add(owner_id, *, org_id=None, visibility="private"):
        pid = uuid4()
        table[pid] = {
            "portfolio_id": str(pid),
            "owner_id": str(owner_id),
            "organization_id": str(org_id) if org_id else None,
            "visibility": visibility
        }
        return pid

    _add.table = table  # type: ignore[attr-defined]
    return _add


@pytest_asyncio.fixture
async def rows(_schema_ready):
    from database import SessionLocal
    from sqlalchemy import text
    from domains.analysis.model import (Analysis, Recommendation,
                                        RecommendationTier)

    created_analyses: list[UUID] = []

    class _Rows:

        async def analysis(
                self, *, ticker: str, portfolio_id: UUID | None,
                status: str = "running") -> UUID:
            async with SessionLocal() as s:
                row = Analysis(
                    user_id=None, portfolio_id=portfolio_id, ticker=ticker,
                    status=status)
                s.add(row)
                await s.commit()
                created_analyses.append(row.id)
                return row.id

        async def recommendation(
                self, *, ticker: str, portfolio_id: UUID, action: str,
                tiers: list[tuple[str, str, str, str]],
                confidence: float = 0.7, window_days: int = 7,
                accepted: bool = False) -> tuple[UUID, UUID, list[UUID]]:
            analysis_id = await self.analysis(
                ticker=ticker, portfolio_id=portfolio_id, status="completed")
            async with SessionLocal() as s:
                rec = Recommendation(
                    analysis_id=analysis_id, recommendation=action,
                    confidence=confidence, reasoning="old reasoning text",
                    debate_converged=True,
                    window_expires_at=datetime.now(UTC) +
                    timedelta(days=window_days),
                    accepted_at=datetime.now(UTC) if accepted else None)
                s.add(rec)
                await s.flush()
                tier_rows = [
                    RecommendationTier(
                        recommendation_id=rec.id, side=side,
                        target_price=Decimal(p), quantity=Decimal(q),
                        status=st) for side, p, q, st in tiers
                ]
                s.add_all(tier_rows)
                await s.commit()
                return analysis_id, rec.id, [t.id for t in tier_rows]

        async def tier_statuses(self, rec_id: UUID) -> list[str]:
            async with SessionLocal() as s:
                result = await s.execute(
                    text(
                        "SELECT status FROM ai.recommendation_tiers "
                        "WHERE recommendation_id = :r ORDER BY target_price"),
                    {"r": rec_id})
                return [r[0] for r in result.all()]

    yield _Rows()

    async with SessionLocal() as s:
        for aid in created_analyses:
            params = {"a": aid}
            for stmt in (
                "DELETE FROM ai.episodic_memories WHERE analysis_id = :a",
                "DELETE FROM ai.investment_decisions WHERE recommendation_id "
                "IN (SELECT id FROM ai.recommendations "
                "WHERE analysis_id = :a)",
                "DELETE FROM ai.recommendation_tiers WHERE recommendation_id "
                "IN (SELECT id FROM ai.recommendations "
                "WHERE analysis_id = :a)",
                "DELETE FROM ai.recommendations WHERE analysis_id = :a",
                "DELETE FROM ai.reflections WHERE analysis_id = :a",
                "DELETE FROM ai.debate_arguments WHERE debate_session_id IN "
                "(SELECT id FROM ai.debate_sessions WHERE analysis_id = :a)",
                "DELETE FROM ai.debate_sessions WHERE analysis_id = :a",
                "DELETE FROM ai.agent_results WHERE agent_run_id IN "
                "(SELECT id FROM ai.agent_runs WHERE analysis_id = :a)",
                "DELETE FROM ai.agent_runs WHERE analysis_id = :a",
                "DELETE FROM ai.analyses WHERE id = :a",
            ):
                await s.execute(text(stmt), params)
        await s.commit()


@pytest.fixture
def workflow_env(monkeypatch, mock_cross_service):
    import ai.workflows.analysis_workflow as wf

    async def _noop(*args, **kwargs):
        return False

    holdings: list[dict] = [{"ticker": "AMD", "quantity": 5.0}]

    async def _holdings(portfolio_id):
        return holdings

    monkeypatch.setattr(wf, "maybe_remember_trajectory", _noop)
    monkeypatch.setattr(wf, "archive_agent_report", _noop)
    monkeypatch.setattr(wf, "get_portfolio_holdings", _holdings)
    return wf, mock_cross_service


@pytest.fixture
def _portfolio_backend(monkeypatch):
    import ai.tools.portfolio_tool as portfolio_tool_module
    from helpers import _cash, _msft_amd_holdings

    monkeypatch.setattr(
        portfolio_tool_module, "get_portfolio_holdings", _msft_amd_holdings)
    monkeypatch.setattr(
        portfolio_tool_module, "get_portfolio_cash_balance", _cash)
