from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import structlog
from fastapi import FastAPI
from shared.database import dispose_engine
from shared.logging import configure_logging
from shared.qdrant import close_qdrant_client, ensure_collection
from shared.redis import close_redis_connections

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    logger.info("application_startup", app_name=app.title)
    await ensure_collection()
    yield
    logger.info("application_shutdown")
    await dispose_engine()
    await close_redis_connections()
    await close_qdrant_client()
