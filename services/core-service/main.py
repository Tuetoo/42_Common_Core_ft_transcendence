import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from shared.database import check_database_connection
from shared.exceptions import register_exception_handlers
from shared.logging import configure_logging
from shared.redis import check_redis_connection, close_redis_connections
from api.router import api_router
from config import settings
from database import engine
from event_listener import build_subscriber
from middleware import register_middleware

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging(settings)
    logger.info("service_startup", service=settings.SERVICE_NAME)
    subscriber = build_subscriber()
    listerner_task = asyncio.create_task(subscriber.run())
    yield
    logger.info("service_shutdown", service=settings.SERVICE_NAME)
    listerner_task.cancel()
    await engine.dispose()
    await close_redis_connections()


app = FastAPI(title="AlphaMind Core Service", version="1.0.0",
              docs_url="/api/docs", openapi_url="/api/openapi.json",
              lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"],
                   allow_credentials=True, allow_methods=["*"],
                   allow_headers=["*"])

register_middleware(app)
register_exception_handlers(app)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health")
async def health() -> dict:
    return {
        "service": settings.SERVICE_NAME,
        "database": await check_database_connection(engine),
        "redis": await check_redis_connection(settings),
    }
