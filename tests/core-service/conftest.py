import os
import sys
from pathlib import Path
from uuid import uuid4
import pytest
import pytest_asyncio
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE_SERVICE_DIR = _REPO_ROOT / "services" / "core-service"

sys.path.insert(0, str(_CORE_SERVICE_DIR))
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

    import apikeys  # noqa: F401
    import outbox  # noqa: F401
    from database import engine as app_engine
    import domains.notification.model  # noqa: F401
    import domains.portfolio.model  # noqa: F401
    import domains.research.model  # noqa: F401
    from shared.database import Base

    async with app_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield
    await app_engine.dispose()
    from shared.redis import close_redis_connections

    try:
        await close_redis_connections()
    except RuntimeError:
        pass


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


@pytest_asyncio.fixture
async def real_client(_schema_ready, mock_cross_service):
    import main
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"X-Internal-Token": TEST_INTERNAL_API_TOKEN
                                    }) as ac:
        yield ac


@pytest.fixture
def mock_cross_service(monkeypatch):
    import clients.ai_client as ai_client
    import clients.auth_client as auth_client
    from shared.schemas import UserRef

    async def _fake_get_user_ref(user_id):
        return UserRef(
            id=user_id, username=f"user-{str(user_id)[:8]}",
            avatar_url="https://example.com/avatar.svg", is_online=False)

    async def _fake_list_user_organization_ids(user_id):
        return []

    async def _fake_get_latest_recommendation(ticker, portfolio_id):
        return None

    async def _fake_get_daily_closes(ticker, *, lookback_days=5, timeout=15.0):
        return None

    async def _fake_request_analysis(
            ticker, portfolio_id=None, *, user_id=None):
        return None

    async def _fake_get_fx_rate(from_currency, to_currency):
        from decimal import Decimal

        if from_currency.upper() == to_currency.upper():
            return Decimal(1)
        return Decimal("0.79")

    monkeypatch.setattr(auth_client, "get_user_ref", _fake_get_user_ref)
    monkeypatch.setattr(
        auth_client, "list_user_organization_ids",
        _fake_list_user_organization_ids)
    monkeypatch.setattr(
        ai_client, "get_latest_recommendation",
        _fake_get_latest_recommendation)
    monkeypatch.setattr(ai_client, "get_daily_closes", _fake_get_daily_closes)
    monkeypatch.setattr(ai_client, "request_analysis", _fake_request_analysis)
    monkeypatch.setattr(ai_client, "get_fx_rate", _fake_get_fx_rate)


@pytest.fixture
def organization_roles(monkeypatch):
    import clients.auth_client as auth_client

    org_id = uuid4()
    roles: dict[str, str] = {}

    async def _ids(user_id):
        return [org_id] if str(user_id) in roles else []

    async def _roles(user_id):
        role = roles.get(str(user_id))
        return {org_id: role} if role else {}

    monkeypatch.setattr(auth_client, "list_user_organization_ids", _ids)
    monkeypatch.setattr(auth_client, "list_user_organization_roles", _roles)
    return org_id, roles


@pytest.fixture
def published_events(monkeypatch):
    import outbox

    events: list[tuple[str, dict]] = []

    async def _publish(settings, event_name, payload, **kwargs):
        events.append((str(event_name), payload))

    monkeypatch.setattr(outbox, "publish_durable_event", _publish)
    return events


@pytest.fixture
async def no_sentiment_analysis(monkeypatch):
    import asyncio
    import clients.ai_client as ai_client
    from domains.research import sentiment

    async def _none(text, kind):
        return None

    monkeypatch.setattr(ai_client, "analyze_sentiment", _none)
    yield
    await asyncio.gather(*sentiment._running.values())
