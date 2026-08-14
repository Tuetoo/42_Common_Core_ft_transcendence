from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from shared.database import check_database_connection
from shared.exceptions import register_exception_handlers
from shared.logging import configure_logging
from shared.http_client import close_http_clients
from shared.redis import check_redis_connection, close_redis_connections
from api.internal import router as internal_router
from api.router import api_router
from config import settings
from database import engine
from db_bootstrap import ensure_schema

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging(settings)
    logger.info("service_startup", service=settings.SERVICE_NAME)
    await ensure_schema(engine)
    Path(settings.AVATAR_STORAGE_DIR).mkdir(parents=True, exist_ok=True)
    yield
    logger.info("service_shutdown")
    await engine.dispose()
    await close_redis_connections()
    await close_http_clients()

app = FastAPI(title="AlphaMind Auth Service", version="1.0.0",
              docs_url="/api/docs", openapi_url="/api/openapi.json",
              lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins(),
                   allow_credentials=True, allow_methods=["*"],
                   allow_headers=["*"])
register_exception_handlers(app)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")
app.include_router(api_router, prefix=settings.API_V1_PREFIX)
app.include_router(internal_router)
app.mount("/static/avatars", StaticFiles(directory=settings.AVATAR_STORAGE_DIR,
                                         check_dir=False), name="avatars")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "service": settings.SERVICE_NAME,
        "database": await check_database_connection(engine),
        "redis": await check_redis_connection(settings),
    }
