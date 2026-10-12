from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
import structlog
from api.internal import router as internal_router
from config import settings
from database import SessionLocal, engine, ensure_schema
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from security import RateLimitedError, rate_limited_handler
from user_state_sync import mirror_all_users_safely
from domains.admin.api import router as admin_router
from domains.auth.api import router as auth_router
from domains.organization.api import router as organization_router
from domains.social.api import router as social_router
from shared.database import check_database_connection
from shared.exceptions import register_exception_handlers
from shared.http_client import close_http_clients
from shared.logging import RequestIdMiddleware, configure_logging
from shared.redis import check_redis_connection, close_redis_connections

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging(settings)
    logger.info("service_startup", service=settings.SERVICE_NAME)
    await ensure_schema(engine)
    await mirror_all_users_safely(SessionLocal)
    Path(settings.AVATAR_STORAGE_DIR).mkdir(parents=True, exist_ok=True)
    yield
    logger.info("service_shutdown")
    await engine.dispose()
    await close_redis_connections()
    await close_http_clients()


app = FastAPI(
    title="AlphaMind Auth Service", version="1.0.0", docs_url="/api/docs",
    openapi_url="/api/openapi-auth.json", lifespan=lifespan)

app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins(),
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

register_exception_handlers(app)
app.add_exception_handler(RateLimitedError, rate_limited_handler)
Instrumentator().instrument(
    app, latency_lowr_buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10,
                               30)).expose(app, endpoint="/metrics")

api_router = APIRouter()
api_router.include_router(auth_router, prefix="/auth")
api_router.include_router(organization_router, prefix="/organizations")
api_router.include_router(social_router, prefix="/connections")
api_router.include_router(admin_router, prefix="/admin")

app.include_router(api_router, prefix=settings.API_V1_PREFIX)
app.include_router(internal_router)

app.mount(
    "/static/avatars",
    StaticFiles(directory=settings.AVATAR_STORAGE_DIR, check_dir=False),
    name="avatars")


@app.get("/health")
async def health() -> JSONResponse:
    dependencies: dict[str, bool] = {
        "database": await check_database_connection(engine),
        "redis": await check_redis_connection(settings)
    }
    healthy = all(dependencies.values())
    return JSONResponse(
        status_code=200 if healthy else 503, content={
            "status": "ok" if healthy else "degraded",
            "service": settings.SERVICE_NAME,
            **dependencies
        })
