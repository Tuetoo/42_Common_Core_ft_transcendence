import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import structlog
from ai.rag.embedding import check_embedding_configuration
from ai.rag.indexing import rebuild_bm25_from_qdrant
from ai.workflows.backtest_workflow import fail_stale_backtests_on_startup
from api.internal import router as internal_router
from config import settings
from database import engine, ensure_schema
from event_listener import build_durable_subscriber
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from qdrant import (check_qdrant_connection, close_qdrant_client,
                    ensure_collection)
from domains.analysis.api import router as analysis_router
from domains.analysis.queue import (analysis_queue,
                                    fail_stale_analyses_on_startup)
from domains.analytics.api import router as analytics_router
from domains.backtest.api import router as backtest_router
from domains.backtest.api import stop_background_backtests
from domains.decision.api import router as decisions_router
from domains.market.api import router as market_router
from domains.rag.api import router as rag_router
from shared.database import check_database_connection
from shared.events import start_background_task
from shared.exceptions import register_exception_handlers
from shared.http_client import close_http_clients
from shared.logging import RequestIdMiddleware, configure_logging
from shared.redis import check_redis_connection, close_redis_connections
from shared.storage import check_storage_connection, close_storage_clients

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    configure_logging(settings)
    logger.info("service_startup", service=settings.SERVICE_NAME)
    await ensure_schema(engine)
    check_embedding_configuration()
    await ensure_collection()
    bm25_chunk_count = await rebuild_bm25_from_qdrant()
    logger.info("bm25_index_rebuilt", chunk_count=bm25_chunk_count)
    durable_listener_task = start_background_task(
        build_durable_subscriber().run(), "durable_event_subscriber")
    await fail_stale_analyses_on_startup()
    await fail_stale_backtests_on_startup()
    analysis_queue.start()
    yield
    logger.info("service_shutdown", service=settings.SERVICE_NAME)
    await analysis_queue.stop()
    await stop_background_backtests()
    durable_listener_task.cancel()
    await asyncio.gather(durable_listener_task, return_exceptions=True)
    await engine.dispose()
    await close_redis_connections()
    await close_http_clients()
    await close_qdrant_client()
    close_storage_clients()


app = FastAPI(
    title="AlphaMind AI Service", version="1.0.0", docs_url="/api/docs",
    openapi_url="/api/openapi-ai.json", lifespan=lifespan)

app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins(),
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

register_exception_handlers(app)
Instrumentator().instrument(
    app, latency_lowr_buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10,
                               30)).expose(app, endpoint="/metrics")

api_router = APIRouter()
api_router.include_router(analysis_router, prefix="/analyses")
api_router.include_router(market_router, prefix="/market/instruments")
api_router.include_router(analytics_router, prefix="/analytics")
api_router.include_router(decisions_router, prefix="/decisions")
api_router.include_router(backtest_router, prefix="/backtests")
api_router.include_router(rag_router, prefix="/rag")

app.include_router(api_router, prefix=settings.API_V1_PREFIX)
app.include_router(internal_router)


@app.get("/health")
async def health() -> JSONResponse:
    dependencies: dict[str, bool] = {
        "database": await check_database_connection(engine),
        "redis": await check_redis_connection(settings),
        "qdrant": await check_qdrant_connection(),
        "storage": await check_storage_connection(settings)
    }
    healthy = all(dependencies.values())
    return JSONResponse(
        status_code=200 if healthy else 503, content={
            "status": "ok" if healthy else "degraded",
            "service": settings.SERVICE_NAME,
            **dependencies
        })
