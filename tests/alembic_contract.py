import asyncio
from dataclasses import replace
from uuid import uuid4
import db_bootstrap
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from shared import alembic_bootstrap
from shared.alembic_bootstrap import SchemaBootstrapError, build_alembic_config
from shared.alembic_convert import CONVERSIONS, convert
from shared.database import Base, create_service_engine

__all__ = [
    "scratch", "test_conversion_refuses_a_remaining_difference",
    "test_downgrade_to_base_and_upgrade_again",
    "test_fresh_schema_is_built_by_migrations_and_matches_models",
    "test_legacy_head_is_refused_then_converted",
    "test_no_model_default_is_a_quoted_function_call",
    "test_older_archived_revision_and_unversioned_tables_are_refused"
]


@pytest.fixture
async def scratch(_schema_ready, alembic_schema, monkeypatch):
    from config import settings

    schema = f"{alembic_schema}_r116_{uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "POSTGRES_SCHEMA", schema)
    engine = create_service_engine(settings)
    conversion = replace(CONVERSIONS[alembic_schema], schema=schema)
    try:
        yield schema, engine, conversion
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


def alembic_config():
    return build_alembic_config(db_bootstrap.ALEMBIC_INI)


async def bootstrap(engine, schema, conversion):
    return await alembic_bootstrap.bootstrap_schema(
        engine, schema=schema, alembic_ini=db_bootstrap.ALEMBIC_INI,
        register_models=db_bootstrap.register_models, conversion=conversion)


async def revision(engine, schema):
    async with engine.connect() as conn:
        return await conn.scalar(
            text(f'SELECT version_num FROM "{schema}".alembic_version'))


async def differences(engine):
    async with engine.connect() as conn:
        return await alembic_bootstrap.schema_differences(conn)


def head():
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


async def upgrade_to(engine, schema, target):
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    await asyncio.to_thread(command.upgrade, alembic_config(), target)


async def convert_schema(engine, schema, conversion, *, apply):
    return await convert(
        engine, schema, apply=apply, conversion=conversion,
        alembic_ini=db_bootstrap.ALEMBIC_INI)


async def test_fresh_schema_is_built_by_migrations_and_matches_models(scratch):
    schema, engine, conversion = scratch
    await bootstrap(engine, schema, conversion)
    assert await revision(engine, schema) == head()
    assert await differences(engine) == []
    await bootstrap(engine, schema, conversion)
    assert await revision(engine, schema) == head()


async def test_downgrade_to_base_and_upgrade_again(scratch):
    schema, engine, conversion = scratch
    await bootstrap(engine, schema, conversion)
    await asyncio.to_thread(command.downgrade, alembic_config(), "base")
    assert await alembic_bootstrap.count_tables(engine, schema) == 0
    await asyncio.to_thread(command.upgrade, alembic_config(), "head")
    assert await differences(engine) == []


def test_no_model_default_is_a_quoted_function_call():
    db_bootstrap.register_models()
    frozen = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            default = column.server_default
            arg = getattr(default, "arg", None)
            if isinstance(arg, str) and "(" in arg:
                frozen.append(f"{table.name}.{column.name}={arg!r}")
    assert frozen == []


async def make_legacy(engine, schema, conversion):
    await upgrade_to(engine, schema, conversion.baseline)
    async with engine.begin() as conn:
        for table, column in conversion.now_defaults:
            await conn.execute(
                text(
                    f'ALTER TABLE "{schema}"."{table}" ALTER COLUMN '
                    f'"{column}" '
                    "SET DEFAULT '2026-08-03 06:41:20+00'::timestamptz"))
        for table, old, new in conversion.renamed_constraints:
            await conn.execute(
                text(
                    f'ALTER TABLE "{schema}"."{table}" '
                    f'RENAME CONSTRAINT "{new}" TO "{old}"'))
        await conn.execute(
            text(f'UPDATE "{schema}".alembic_version SET version_num = :v'),
            {"v": conversion.legacy_head})


async def test_legacy_head_is_refused_then_converted(scratch):
    schema, engine, conversion = scratch
    await make_legacy(engine, schema, conversion)
    if conversion.now_defaults:
        assert await differences(engine) != []

    with pytest.raises(SchemaBootstrapError,
                       match=r"scripts\.alembic_convert"):
        await bootstrap(engine, schema, conversion)

    preview = await convert_schema(engine, schema, conversion, apply=False)
    assert preview.status == "would_convert"
    assert preview.head == head()
    assert len(preview.statements) == (
        len(conversion.now_defaults) + len(conversion.renamed_constraints))
    assert await revision(engine, schema) == conversion.legacy_head
    if conversion.now_defaults:
        assert await differences(engine) != []

    done = await convert_schema(engine, schema, conversion, apply=True)
    assert done.status == "converted"
    assert await revision(engine, schema) == head()
    assert await differences(engine) == []
    again = await convert_schema(engine, schema, conversion, apply=True)
    assert again.status == "already"
    await bootstrap(engine, schema, conversion)
    assert await revision(engine, schema) == head()


async def test_conversion_refuses_a_remaining_difference(scratch):
    schema, engine, conversion = scratch
    await make_legacy(engine, schema, conversion)
    table = sorted(Base.metadata.tables)[0]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f'ALTER TABLE "{schema}"."{table}" ADD COLUMN r116_extra int'))
    report = await convert_schema(engine, schema, conversion, apply=True)
    assert report.status == "refused"
    assert any("r116_extra" in d for d in report.differences)
    assert await revision(engine, schema) == conversion.legacy_head


async def test_older_archived_revision_and_unversioned_tables_are_refused(
        scratch):
    schema, engine, conversion = scratch
    older = sorted(conversion.archived - {conversion.legacy_head})[0]
    await bootstrap(engine, schema, conversion)
    async with engine.begin() as conn:
        await conn.execute(
            text(f'UPDATE "{schema}".alembic_version SET version_num = :v'),
            {"v": older})
    report = await convert_schema(engine, schema, conversion, apply=True)
    assert report.status == "refused" and "older" in report.reason
    with pytest.raises(SchemaBootstrapError, match="archived"):
        await bootstrap(engine, schema, conversion)

    async with engine.begin() as conn:
        await conn.execute(text(f'DROP TABLE "{schema}".alembic_version'))
    with pytest.raises(SchemaBootstrapError, match="no Alembic revision"):
        await bootstrap(engine, schema, conversion)
