import sys
from logging.config import fileConfig
from pathlib import Path
from alembic import context
from sqlalchemy import engine_from_config, pool, text
from database import register_all_models

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(next(c for c in Path(__file__).resolve().parents if (
    c / "shared" / "database.py").is_file())))


from config import settings   # noqa: E402
from shared.database import Base  # noqa: E402
register_all_models()
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def _include_object(object: object, name: str | None, type_: str,
                    *_args: object) -> bool:
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    context.configure(url=settings.assemble_sync_database_url(),
                      target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"},
                      version_table_schema=settings.POSTGRES_SCHEMA,
                      include_object=_include_object)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = settings.assemble_sync_database_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.",
                                     poolclass=pool.NullPool)
    with connectable.connect() as connection:
        connection.execute(text("SET search_path TO "
                                f"{settings.POSTGRES_SCHEMA}"))
        connection.commit()
        context.configure(connection=connection,
                          target_metadata=target_metadata,
                          version_table_schema=settings.POSTGRES_SCHEMA,
                          include_object=_include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
