import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
import structlog
from api.internal import router as internal_router
from api.router import api_router
from api.websocket import router as websocket_router
from api.yjs_websocket import router as yjs_websocket_router
from db_bootstrap import ensure_schema
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from shared.database import check_database_connection
from shared.exceptions import register_exception_handlers
from shared.http_client import close_http_clients
from shared.logging import configure_logging
from shared.redis import check_redis_connection, close_redis_connections
from shared.storage import (check_storage_connection, close_storage_clients,
                            ensure_bucket)
from config import settings
from database import engine
from event_listener import build_subscriber, build_durable_subscriber
from middleware import register_middleware
from yjs_relay import run_cross_replica_listener

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging(settings)
    logger.info("service_startup", service=settings.SERVICE_NAME)
    await ensure_schema(engine)
    await ensure_bucket(settings, settings.S3_BUCKET_DOCUMENTS)
    subscriber = build_subscriber()
    listener_task = asyncio.create_task(subscriber.run())
    durable_subscriber = build_durable_subscriber()
    durable_listener_task = asyncio.create_task(durable_subscriber.run())
    yjs_listener_task = asyncio.create_task(run_cross_replica_listener())
    yield
    logger.info("service_shutdown", service=settings.SERVICE_NAME)
    listener_task.cancel()
    durable_listener_task.cancel()
    yjs_listener_task.cancel()
    await engine.dispose()
    await close_redis_connections()
    await close_http_clients()
    close_storage_clients()


app = FastAPI(title="AlphaMind Core Service", version="1.0.0",
              docs_url="/api/docs", openapi_url="/api/openapi-core.json",
              lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins(),
                   allow_credentials=True, allow_methods=["*"],
                   allow_headers=["*"])

register_middleware(app)
register_exception_handlers(app)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")
app.include_router(api_router, prefix=settings.API_V1_PREFIX)
app.include_router(internal_router)
app.include_router(websocket_router)
app.include_router(yjs_websocket_router)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "service": settings.SERVICE_NAME,
        "database": await check_database_connection(engine),
        "redis": await check_redis_connection(settings),
        "storage": await check_storage_connection(settings),
    }
