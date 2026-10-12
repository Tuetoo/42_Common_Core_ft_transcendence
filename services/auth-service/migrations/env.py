import sys
from logging.config import fileConfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(
    0,
    str(
        next(
            c for c in Path(__file__).resolve().parents
            if (c / "shared" / "database.py").is_file())))

from alembic import context  # noqa: E402
from config import settings  # noqa: E402
from database import register_models  # noqa: E402
from sqlalchemy import Connection, engine_from_config, pool, text  # noqa: E402
from shared.database import Base, exclude_alembic_version  # noqa: E402

register_models()

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.assemble_sync_database_url(),
        target_metadata=target_metadata, literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=settings.POSTGRES_SCHEMA,
        include_object=exclude_alembic_version)
    with context.begin_transaction():
        context.run_migrations()


def _run_on(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata,
        version_table_schema=settings.POSTGRES_SCHEMA,
        include_object=exclude_alembic_version, compare_type=True,
        compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = settings.assemble_sync_database_url()
    connectable = engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        connection.execute(
            text(f"SET search_path TO {settings.POSTGRES_SCHEMA}"))
        connection.commit()
        _run_on(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
