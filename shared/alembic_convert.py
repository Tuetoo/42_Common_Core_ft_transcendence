from dataclasses import dataclass, field
from pathlib import Path
from alembic import command
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


@dataclass(frozen=True)
class Conversion:
    service: str
    schema: str
    legacy_head: str
    baseline: str
    archived: frozenset[str]
    now_defaults: tuple[tuple[str, str], ...] = ()
    renamed_constraints: tuple[tuple[str, str, str], ...] = ()


CONVERSIONS: dict[str, Conversion] = {
    "auth": Conversion(
        service="auth-service", schema="auth", legacy_head="f1130b000001",
        baseline="f1160b000001",
        archived=frozenset({"442fc97143aa", "f1120b000001", "f1130b000001"}),
        now_defaults=(("connections",
                       "created_at"), ("organization_members", "joined_at"),
                      ("user_sessions", "created_at")), renamed_constraints=((
                          "connections", "connections_blocked_by_fkey",
                          "fk_connections_blocked_by"), )),
    "core": Conversion(
        service="core-service", schema="core", legacy_head="f1130c000002",
        baseline="f1160c000001", archived=frozenset({
            "abe4d5b13e9e", "3c7e97d4a03e", "ded4e865c2b5", "e9b078a12928",
            "f1120c000001", "f1130c000001", "f1130c000002"
        }),
        now_defaults=(("api_keys", "created_at"), ("documents", "uploaded_at"),
                      ("notifications",
                       "created_at"), ("research_comments", "created_at"),
                      ("research_notes", "created_at"))),
    "ai": Conversion(
        service="ai-service", schema="ai", legacy_head="f1120a000002",
        baseline="f1160a000001", archived=frozenset({
            "941da8fcdd01", "05942b356f11", "619803f90625", "c3b5bfb73477",
            "f1120a000001", "f1120a000002"
        }), now_defaults=(("price_bars", "fetched_at"), ))
}


@dataclass
class ConversionReport:
    schema: str
    status: str
    revision: str | None
    statements: list[str] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    reason: str = ""
    head: str | None = None


async def _revision(conn: AsyncConnection, schema: str) -> str | None:
    exists = await conn.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name = 'alembic_version')"
        ), {"schema": schema})
    if not exists:
        return None
    value = await conn.scalar(
        text(f'SELECT version_num FROM "{schema}".alembic_version LIMIT 1'))
    return None if value is None else str(value)


async def _column_exists(
        conn: AsyncConnection, schema: str, table: str, column: str) -> bool:
    return bool(
        await conn.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE "
                "table_schema = :s AND table_name = :t AND column_name = :c)"),
            {
                "s": schema,
                "t": table,
                "c": column
            }))


async def _constraint_exists(
        conn: AsyncConnection, schema: str, table: str, name: str) -> bool:
    return bool(
        await conn.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint c "
                "JOIN pg_class r ON r.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = r.relnamespace "
                "WHERE n.nspname = :s AND r.relname = :t AND c.conname = :n)"),
            {
                "s": schema,
                "t": table,
                "n": name
            }))


async def alignment_statements(conn: AsyncConnection,
                               conversion: Conversion) -> list[str]:
    schema = conversion.schema
    statements: list[str] = []
    for table, column in conversion.now_defaults:
        if await _column_exists(conn, schema, table, column):
            statements.append(
                f'ALTER TABLE "{schema}"."{table}" '
                f'ALTER COLUMN "{column}" SET DEFAULT now()')
    for table, old, new in conversion.renamed_constraints:
        if await _constraint_exists(conn, schema, table, old):
            statements.append(
                f'ALTER TABLE "{schema}"."{table}" RENAME CONSTRAINT "{old}" '
                f'TO "{new}"')
    return statements


def _upgrade_on(connection: Connection, alembic_ini: Path) -> str | None:
    from shared.alembic_bootstrap import build_alembic_config

    config = build_alembic_config(alembic_ini)
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
    return MigrationContext.configure(connection).get_current_revision()


async def convert(
        engine: AsyncEngine, schema: str, *, apply: bool, alembic_ini: Path,
        conversion: Conversion | None = None) -> ConversionReport:
    from shared.alembic_bootstrap import schema_differences

    if conversion is None:
        conversion = CONVERSIONS[schema]
    async with engine.connect() as conn:
        revision = await _revision(conn, schema)
        if revision == conversion.baseline or (
                revision is not None and revision not in conversion.archived):
            return ConversionReport(
                schema, "already", revision,
                reason="not on the archived chain; nothing to do")
        if revision != conversion.legacy_head:
            return ConversionReport(
                schema, "refused", revision, reason=(
                    "no Alembic revision" if revision is None else
                    f"revision {revision} is older than the archived "
                    f"chain's head "
                    f"{conversion.legacy_head}; start the previous release "
                    f"once so it "
                    "upgrades to that head, then convert"))
        await conn.rollback()
        transaction = await conn.begin()
        try:
            statements = await alignment_statements(conn, conversion)
            for statement in statements:
                await conn.execute(text(statement))
            await conn.execute(
                text(
                    f'UPDATE "{schema}".alembic_version SET version_num = '
                    f":new "
                    "WHERE version_num = :old"), {
                        "new": conversion.baseline,
                        "old": conversion.legacy_head
                    })
            head = await conn.run_sync(_upgrade_on, alembic_ini)
            differences = await schema_differences(conn)
            if differences:
                await transaction.rollback()
                return ConversionReport(
                    schema, "refused", revision, statements, differences,
                    reason=(
                        "the schema still differs from the models after the "
                        "alignment and the later migrations; nothing was "
                        "changed"))
            if not apply:
                await transaction.rollback()
                return ConversionReport(
                    schema, "would_convert", revision, statements, head=head)
            await transaction.commit()
        except BaseException:
            if transaction.is_active:
                await transaction.rollback()
            raise
    return ConversionReport(schema, "converted", head, statements, head=head)


def format_report(report: ConversionReport, conversion: Conversion) -> str:
    lines = [f"schema {report.schema}: revision {report.revision}"]
    if report.status == "already":
        lines.append(f"ALREADY  {report.reason}")
        return "\n".join(lines)
    if report.status == "refused":
        lines.append(f"REFUSED  {report.reason}")
        lines.extend(f"  difference: {d}" for d in report.differences)
        return "\n".join(lines)
    verb = "applied" if report.status == "converted" else "would apply"
    lines.append(f"{verb} {len(report.statements)} alignment statement(s):")
    lines.extend(f"  {s};" for s in report.statements)
    lines.append(
        "schema matches the models after the alignment and the later "
        "migrations: yes")
    path = f"{conversion.legacy_head} -> {conversion.baseline}"
    if report.head and report.head != conversion.baseline:
        path += f" -> {report.head} (later migrations)"
    if report.status == "converted":
        lines.append(
            f"CONVERTED  revision {path} (one transaction, committed)")
    else:
        lines.append(
            f"PREVIEW  would move revision {path}; nothing was "
            "changed. Run again with --apply.")
    return "\n".join(lines)
