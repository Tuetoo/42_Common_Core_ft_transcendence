import asyncio
import configparser
from collections.abc import AsyncGenerator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID
import structlog
from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, MetaData, func, select, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession,
                                    async_sessionmaker, create_async_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import Select
from shared.config import BaseServiceSettings

logger = structlog.get_logger(__name__)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s"
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPKMixin:
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True,
        server_default=func.gen_random_uuid())


class TimestampColumnsMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False)


def create_service_engine(settings: BaseServiceSettings) -> AsyncEngine:
    return create_async_engine(
        settings.assemble_database_url(), echo=settings.DB_ECHO,
        pool_size=settings.DB_POOL_SIZE, max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True, connect_args={
            "server_settings": {
                "search_path": f"{settings.POSTGRES_SCHEMA},public"
            }
        })


def create_session_factory(
        engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False,
        autoflush=False)


def make_get_db(
    session_factory: async_sessionmaker[AsyncSession]
) -> Callable[[], AsyncGenerator[AsyncSession, None]]:

    async def get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

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

_ALEMBIC_MAIN_OPTIONS = ("prepend_sys_path", "path_separator")


class SchemaBootstrapError(RuntimeError):
    pass


def build_alembic_config(alembic_ini: Path) -> Config:
    parser = configparser.ConfigParser()
    if not parser.read(alembic_ini):
        raise FileNotFoundError(f"alembic.ini not found: {alembic_ini}")
    section = parser["alembic"]
    config = Config()
    script_location = Path(section.get("script_location", "migrations"))
    if not script_location.is_absolute():
        script_location = alembic_ini.parent / script_location
    config.set_main_option("script_location", str(script_location))
    for key in _ALEMBIC_MAIN_OPTIONS:
        if key in section:
            config.set_main_option(key, section[key])
    return config


def _upgrade_head(alembic_ini: Path) -> None:
    command.upgrade(build_alembic_config(alembic_ini), "head")


async def version_state(engine: AsyncEngine,
                        schema: str) -> tuple[bool, str | None]:
    async with engine.connect() as conn:
        exists = bool(
            await conn.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :schema "
                    "AND table_name = 'alembic_version')"),
                {"schema": schema}))
        if not exists:
            return False, None
        revision = await conn.scalar(
            text(
                f'SELECT version_num FROM "{schema}".alembic_version LIMIT 1'))
    return True, revision


async def count_tables(engine: AsyncEngine, schema: str) -> int:
    async with engine.connect() as conn:
        count = await conn.scalar(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_type = 'BASE TABLE' "
                "AND table_name <> 'alembic_version'"), {"schema": schema})
    return int(count or 0)


def exclude_alembic_version(
        object_: Any, name: str | None, type_: str, *_args: Any) -> bool:
    return not (type_ == "table" and name == "alembic_version")


async def bootstrap_schema(
        engine: AsyncEngine, *, schema: str, alembic_ini: Path,
        register_models: Callable[[], None]) -> None:
    register_models()

    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    has_version_table, revision_before = await version_state(engine, schema)
    if revision_before is None:
        existing_tables = await count_tables(engine, schema)
        if existing_tables:
            raise SchemaBootstrapError(
                f"schema {schema!r} has {existing_tables} table(s) but no "
                f"Alembic "
                "revision, so its state is unknown. Restore a backup, or "
                "inspect "
                "it by hand; the service does not guess.")
        if has_version_table:
            logger.warning(
                "schema_bootstrap_empty_version_table", schema=schema)

    await asyncio.to_thread(_upgrade_head, alembic_ini)

    _, revision_after = await version_state(engine, schema)
    logger.info(
        "schema_bootstrap_complete", schema=schema, action="upgrade",
        revision_before=revision_before, revision_after=revision_after)
