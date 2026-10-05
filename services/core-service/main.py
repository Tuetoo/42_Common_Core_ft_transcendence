import asyncio
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from typing import Any
import structlog
from api.apikeys_api import router as apikeys_router
from api.internal import router as internal_router
from api.public import router as public_router
from api.websocket import router as websocket_router
from api.yjs_websocket import router as yjs_websocket_router
from config import settings
from database import engine
from db_bootstrap import ensure_schema
from event_listener import build_durable_subscriber, build_subscriber
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from job_health import load_job_health
from middleware import register_middleware
from outbox import run_outbox_dispatcher
from prometheus_fastapi_instrumentator import Instrumentator
from websocket_manager import \
    run_cross_replica_listener as run_websocket_fanout_listener
from yjs_relay import (relay, run_active_room_heartbeat,
                       run_cross_replica_listener)
from domains.notification.api import router as notification_router
from domains.portfolio.api import router as portfolio_router
from domains.portfolio.api import watchlist_router
from domains.research.api import router as research_router
from domains.research.moderation_api import router as moderation_router
from domains.research.yjs_guard import validator as yjs_update_validator
from shared.database import check_database_connection
from shared.events import start_background_task
from shared.exceptions import register_exception_handlers
from shared.http_client import close_http_clients
from shared.logging import configure_logging
from shared.redis import check_redis_connection, close_redis_connections
from shared.request_id import RequestIdMiddleware
from shared.storage import (check_storage_connection, close_storage_clients,
                            ensure_bucket)

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging(settings)
    logger.info("service_startup", service=settings.SERVICE_NAME)
    await ensure_schema(engine)
    await ensure_bucket(settings, settings.S3_BUCKET_DOCUMENTS)
    await load_job_health()
    loops: dict[str, Coroutine[Any, Any, None]] = {
        "event_subscriber": build_subscriber().run(),
        "durable_event_subscriber": build_durable_subscriber().run(),
        "yjs_cross_replica_listener": run_cross_replica_listener(),
        "yjs_active_room_heartbeat": run_active_room_heartbeat(),
        "websocket_fanout_listener": run_websocket_fanout_listener(),
        "outbox_dispatcher": run_outbox_dispatcher()
    }
    background_tasks = [
        start_background_task(loop, name) for name, loop in loops.items()
    ]
    yield
    logger.info("service_shutdown", service=settings.SERVICE_NAME)
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    await relay.persist_all_dirty()
    await yjs_update_validator.aclose()
    await engine.dispose()
    await close_redis_connections()
    await close_http_clients()
    close_storage_clients()


app = FastAPI(
    title="AlphaMind Core Service", version="1.0.0", docs_url="/api/docs",
    openapi_url="/api/openapi-core.json", lifespan=lifespan)

register_middleware(app)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins(),
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
register_exception_handlers(app)
Instrumentator().instrument(
    app, latency_lowr_buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10,
                               30)).expose(app, endpoint="/metrics")

api_router = APIRouter()
api_router.include_router(public_router)
api_router.include_router(apikeys_router)
api_router.include_router(portfolio_router, prefix="/portfolios")
api_router.include_router(watchlist_router, prefix="/watchlists")
api_router.include_router(research_router, prefix="/research")
api_router.include_router(notification_router, prefix="/notifications")

app.include_router(api_router, prefix=settings.API_V1_PREFIX)
app.include_router(
    moderation_router, prefix=f"{settings.API_V1_PREFIX}/moderation")
app.include_router(internal_router)
app.include_router(websocket_router)
app.include_router(yjs_websocket_router)


@app.get("/health")
async def health() -> JSONResponse:
    dependencies: dict[str, bool] = {
        "database": await check_database_connection(engine),
        "redis": await check_redis_connection(settings),
        "storage": await check_storage_connection(settings)
    }
    healthy = all(dependencies.values())
    return JSONResponse(
        status_code=200 if healthy else 503, content={
            "status": "ok" if healthy else "degraded",
            "service": settings.SERVICE_NAME,
            **dependencies
        })
