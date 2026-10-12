from pathlib import Path
from config import settings
from sqlalchemy.ext.asyncio import AsyncEngine
from shared.database import (bootstrap_schema, create_service_engine,
                             create_session_factory, make_get_db)

ALEMBIC_INI = Path(__file__).resolve().parent / "alembic.ini"

engine = create_service_engine(settings)
SessionLocal = create_session_factory(engine)
get_db = make_get_db(SessionLocal)


def register_models() -> None:
    import domains.auth.model
    import domains.organization.model
    import domains.social.model  # noqa: F401


async def ensure_schema(engine: AsyncEngine) -> None:
    await bootstrap_schema(
        engine, schema=settings.POSTGRES_SCHEMA, alembic_ini=ALEMBIC_INI,
        register_models=register_models)
