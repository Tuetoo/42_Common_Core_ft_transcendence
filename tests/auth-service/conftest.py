import os
import sys
from pathlib import Path
import pytest
import pytest_asyncio
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AUTH_SERVICE_DIR = _REPO_ROOT / "services" / "auth-service"

sys.path.insert(0, str(_AUTH_SERVICE_DIR))
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
    os.environ["SMTP_HOST"] = "localhost"
    os.environ["SMTP_PORT"] = "1"


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
            text(f"DROP SCHEMA IF EXISTS {settings.POSTGRES_SCHEMA} CASCADE"))
        await conn.execute(
            text(f"CREATE SCHEMA IF NOT EXISTS {settings.POSTGRES_SCHEMA}"))
    await admin_engine.dispose()

    from database import engine as app_engine
    import domains.auth.model  # noqa: F401
    import domains.organization.model  # noqa: F401
    import domains.social.model  # noqa: F401
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
async def client(db_session):
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
async def real_client(_schema_ready):
    import main
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"X-Internal-Token": TEST_INTERNAL_API_TOKEN
                                    }) as ac:
        yield ac


@pytest.fixture
def mock_email(monkeypatch):
    sent = []

    async def _fake_send(settings, *, to, subject, html_body, text_body=None):
        sent.append({"to": to, "subject": subject, "html_body": html_body})

    import shared.email as email_module

    monkeypatch.setattr(email_module, "send_email", _fake_send)
    return sent


@pytest_asyncio.fixture(autouse=True)
async def _reset_rate_limits():
    from config import settings
    from shared.redis import get_rate_limit_client

    await get_rate_limit_client(settings).flushdb()
    yield


@pytest.fixture
def captured_events(monkeypatch):
    sent = []

    async def _fake(settings, name, payload, **kw):
        if str(name) != "user.presence.changed":
            sent.append((str(name), payload))

    import events_out

    monkeypatch.setattr(events_out, "publish_cross_service", _fake)
    return sent


@pytest.fixture
def state_check(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "USER_STATE_CHECK_ENABLED", True)
    return settings
