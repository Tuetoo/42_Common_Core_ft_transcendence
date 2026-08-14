from collections.abc import AsyncGenerator, Callable
from typing import Any
from sqlalchemy import MetaData, func, select, text
from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession,
                                    async_sessionmaker, create_async_engine)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import Select
from shared.config import BaseServiceSettings

# index, unique, check, foreign key, primary key
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_service_engine(settings: BaseServiceSettings) -> AsyncEngine:
    engine = create_async_engine(
        settings.assemble_database_url(), echo=settings.DB_ECHO,
        pool_size=settings.DB_POOL_SIZE, max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,
        connect_args={"server_settings": {
            "search_path": f"{settings.POSTGRES_SCHEMA},public"}})
    return engine


def create_session_factory(engine: AsyncEngine
                           ) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, class_=AsyncSession,
                              expire_on_commit=False, autoflush=False)


def make_get_db(session_factory: async_sessionmaker[AsyncSession]
                ) -> Callable[[], AsyncGenerator[AsyncSession, None]]:
    async def get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()
    return get_db


async def count_query(session: AsyncSession, stmt: Select[Any]) -> int:
    count_stmt = select(func.count()).select_from(stmt.subquery())
    result = await session.execute(count_stmt)
    return int(result.scalar_one())


async def check_database_connection(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def dispose_engine(engine: AsyncEngine) -> None:
    await engine.dispose()
