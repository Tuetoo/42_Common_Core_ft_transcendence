import asyncio
import configparser
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
import structlog
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from shared.database import Base

if TYPE_CHECKING:
    from shared.alembic_convert import Conversion

logger = structlog.get_logger(__name__)

_ALEMBIC_MAIN_OPTIONS = ("prepend_sys_path", "version_path_separator")


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
    config.set_main_option(
        "path_separator", section.get("path_separator", "os"))
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


def describe_difference(diff: Any) -> str:
    if isinstance(diff, list):
        return "; ".join(describe_difference(item) for item in diff)
    if not isinstance(diff, tuple) or not diff:
        return repr(diff)
    kind = str(diff[0])
    if kind.startswith("modify_") and len(diff) >= 4:
        return f"{kind} {diff[2]}.{diff[3]}"
    if kind in ("add_column", "remove_column") and len(diff) >= 4:
        return f"{kind} {diff[2]}.{getattr(diff[3], 'name', diff[3])}"
    target = diff[1] if len(diff) > 1 else None
    name = getattr(target, "name", None) or repr(target)
    table = getattr(getattr(target, "table", None), "name", None)
    return f"{kind} {table}.{name}" if table else f"{kind} {name}"


def _differences_sync(connection: Connection) -> list[str]:
    context = MigrationContext.configure(
        connection, opts={
            "compare_type": True,
            "compare_server_default": True,
            "include_object": exclude_alembic_version
        })
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        diffs = compare_metadata(context, Base.metadata)
    return [describe_difference(diff) for diff in diffs]


async def schema_differences(conn: AsyncConnection) -> list[str]:
    return await conn.run_sync(_differences_sync)


async def bootstrap_schema(
        engine: AsyncEngine, *, schema: str, alembic_ini: Path,
        register_models: Callable[[], None],
        conversion: "Conversion | None" = None) -> None:
    from shared.alembic_convert import CONVERSIONS

    if conversion is None:
        conversion = CONVERSIONS.get(schema)
    register_models()

    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    has_version_table, revision_before = await version_state(engine, schema)
    if conversion is not None and revision_before in conversion.archived:
        raise SchemaBootstrapError(
            f"schema {schema!r} is at revision {revision_before} of the "
            "archived migration chain. Convert it once (preview, "
            "then --apply) and start the service again: docker compose "
            f"--env-file .env.docker exec -T {conversion.service} python -m "
            "scripts.alembic_convert")
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
