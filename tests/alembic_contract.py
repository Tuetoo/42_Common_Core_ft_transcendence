import asyncio
from uuid import uuid4
import database
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from shared.database import (Base, SchemaBootstrapError, bootstrap_schema,
                             build_alembic_config, count_tables,
                             create_service_engine, schema_differences)

__all__ = [
    "scratch", "test_downgrade_to_base_and_upgrade_again",
    "test_fresh_schema_is_built_by_migrations_and_matches_models",
    "test_no_model_default_is_a_quoted_function_call",
    "test_unversioned_tables_are_refused"
]


@pytest.fixture
async def scratch(_schema_ready, alembic_schema, monkeypatch):
    from config import settings

    schema = f"{alembic_schema}_r116_{uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "POSTGRES_SCHEMA", schema)
    engine = create_service_engine(settings)
    try:
        yield schema, engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


def alembic_config():
    return build_alembic_config(database.ALEMBIC_INI)


async def bootstrap(engine, schema):
    return await bootstrap_schema(
        engine, schema=schema, alembic_ini=database.ALEMBIC_INI,
        register_models=database.register_models)


async def revision(engine, schema):
    async with engine.connect() as conn:
        return await conn.scalar(
            text(f'SELECT version_num FROM "{schema}".alembic_version'))


async def differences(engine):
    async with engine.connect() as conn:
        return await schema_differences(conn)


def head():
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


async def upgrade_to(engine, schema, target):
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    await asyncio.to_thread(command.upgrade, alembic_config(), target)


async def test_fresh_schema_is_built_by_migrations_and_matches_models(scratch):
    schema, engine = scratch
    await bootstrap(engine, schema)
    assert await revision(engine, schema) == head()
    assert await differences(engine) == []
    await bootstrap(engine, schema)
    assert await revision(engine, schema) == head()


async def test_downgrade_to_base_and_upgrade_again(scratch):
    schema, engine = scratch
    await bootstrap(engine, schema)
    await asyncio.to_thread(command.downgrade, alembic_config(), "base")
    assert await count_tables(engine, schema) == 0
    await asyncio.to_thread(command.upgrade, alembic_config(), "head")
    assert await differences(engine) == []


def test_no_model_default_is_a_quoted_function_call():
    database.register_models()
    frozen = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            default = column.server_default
            arg = getattr(default, "arg", None)
            if isinstance(arg, str) and "(" in arg:
                frozen.append(f"{table.name}.{column.name}={arg!r}")
    assert frozen == []


async def test_unversioned_tables_are_refused(scratch):
    schema, engine = scratch
    await bootstrap(engine, schema)
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP TABLE "{schema}".alembic_version'))
    with pytest.raises(SchemaBootstrapError, match="no Alembic revision"):
        await bootstrap(engine, schema)
