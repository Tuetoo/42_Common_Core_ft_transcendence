import structlog
from config import settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from shared.database import Base
from database import register_all_models

logger = structlog.get_logger(__name__)


async def ensure_schema(engine: AsyncEngine) -> None:
    if settings.APP_ENV == "production":
        logger.info("schema_bootstrap_skipped_production")
        return
    register_all_models()
    async with engine.begin() as conn:
        await conn.execute(text(
            f"CREATE SCHEMA IF NOT EXISTS {settings.POSTGRES_SCHEMA}"))
        await conn.run_sync(Base.metadata.create_all)
    logger.info("schema_bootstrap_complete", schema=settings.POSTGRES_SCHEMA)
