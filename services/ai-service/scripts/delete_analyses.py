import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from ai.knowledge.archival import delete_archived_report
from ai.rag.indexing import delete_document_completely
from config import settings
from database import SessionLocal
from qdrant import get_qdrant_client
from qdrant_client.http import models as qmodels
from scripts.smoke_check import one_line
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

IN_FLIGHT_STATUSES = frozenset({"pending", "running"})

ANALYSIS_KEYED_TABLES: tuple[str, ...] = (
    "episodic_memories", "reflections", "agent_runs", "debate_sessions",
    "recommendations", "agent_reports")

ORPHAN_CHILD_CHECKS: tuple[tuple[str, str, str, bool], ...] = (
    ("recommendation_tiers", "recommendation_id", "recommendations", True),
    ("recommendation_metrics", "recommendation_id", "recommendations",
     True), ("agent_results", "agent_run_id", "agent_runs", True),
    ("debate_arguments", "debate_session_id", "debate_sessions",
     True), ("kg_relations", "source_report_id", "agent_reports", True),
    ("investment_decisions", "recommendation_id", "recommendations", False),
    ("decision_outcomes", "decision_id", "investment_decisions", False),
    ("decision_reviews", "decision_id", "investment_decisions", False))

MEMORY_TAIL = timedelta(minutes=120)

MAX_ORPHAN_DOCUMENT_FRACTION = 0.25

HANDLED_FKS: set[str] = {
    *(f"ai.{table}.analysis_id" for table in ANALYSIS_KEYED_TABLES),
    "ai.recommendation_tiers.recommendation_id",
    "ai.recommendation_metrics.recommendation_id",
    "ai.agent_results.agent_run_id", "ai.debate_arguments.debate_session_id",
    "ai.kg_relations.source_report_id",
    "ai.investment_decisions.recommendation_id",
    "ai.decision_outcomes.decision_id", "ai.decision_reviews.decision_id",
    "ai.kg_relations.source_entity_id", "ai.kg_relations.target_entity_id"
}

FK_ROOTS: list[tuple[str, str]] = [("ai", "analyses"), ("ai", "agent_reports"),
                                   ("ai", "document_chunks"),
                                   ("ai", "agent_memories"),
                                   ("ai", "kg_entities")]

_QDRANT_SCROLL_BATCH = 256

_FK_SQL = """
    SELECT kcu.table_schema, kcu.table_name, kcu.column_name,
           ccu.table_schema, ccu.table_name, ccu.column_name
    FROM information_schema.referential_constraints rc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_schema = rc.constraint_schema
     AND kcu.constraint_name = rc.constraint_name
    JOIN information_schema.constraint_column_usage ccu
      ON ccu.constraint_schema = rc.unique_constraint_schema
     AND ccu.constraint_name = rc.unique_constraint_name
    WHERE ccu.table_schema = :schema AND ccu.table_name = :table
    ORDER BY kcu.table_schema, kcu.table_name, kcu.column_name
"""


@dataclass(frozen=True)
class ForeignKey:
    table_schema: str
    table: str
    column: str
    ref_schema: str
    ref_table: str
    ref_column: str

    @property
    def referencing(self) -> str:
        return f"{self.table_schema}.{self.table}.{self.column}"

    def describe(self) -> str:
        return (
            f"{self.referencing} -> "
            f"{self.ref_schema}.{self.ref_table}.{self.ref_column}")


async def referencing_foreign_keys(
        session: AsyncSession, schema: str, table: str) -> list[ForeignKey]:
    rows = (
        await
        session.execute(sql_text(_FK_SQL), {
            "schema": schema,
            "table": table
        })).all()
    return [ForeignKey(*(str(value) for value in row)) for row in rows]


async def foreign_key_tree(
        session: AsyncSession, schema: str, table: str, *,
        max_depth: int = 4) -> list[tuple[int, ForeignKey]]:
    tree: list[tuple[int, ForeignKey]] = []
    visited: set[tuple[str, str]] = {(schema, table)}

    async def _walk(parent_schema: str, parent_table: str, depth: int) -> None:
        if depth > max_depth:
            return
        for fk in await referencing_foreign_keys(session, parent_schema,
                                                 parent_table):
            tree.append((depth, fk))
            key = (fk.table_schema, fk.table)
            if key not in visited:
                visited.add(key)
                await _walk(fk.table_schema, fk.table, depth + 1)

    await _walk(schema, table, 1)
    return tree


async def check_foreign_key_coverage(
        session: AsyncSession, roots: list[tuple[str, str]],
        handled: set[str]) -> list[str]:
    unhandled: list[str] = []
    discovered: set[str] = set()
    for schema, table in roots:
        tree = await foreign_key_tree(session, schema, table)
        print(
            f"Foreign keys referencing {schema}.{table} (from "
            f"information_schema):")
        if not tree:
            print("  (none)")
        for depth, fk in tree:
            discovered.add(fk.referencing)
            marker = "handled" if fk.referencing in handled else "NOT HANDLED"
            print(f"  {'  ' * (depth - 1)}- {fk.describe()}  [{marker}]")
            if (fk.referencing not in handled
                    and fk.referencing not in unhandled):
                unhandled.append(fk.referencing)
    for expected in sorted(handled - discovered):
        print(f"  note: handled column {expected} has no FK in this database")
    if unhandled:
        print(
            "\nABORTING any apply: unhandled foreign key(s) found: " +
            ", ".join(unhandled) + ". Update the script to handle them first.")
    return unhandled


def mode_banner(confirm: bool) -> str:
    return (
        "=== CONFIRM (changes will be applied) ===" if confirm else
        ("=== PREVIEW (read-only; re-run with --confirm to apply) ==="))


@dataclass
class ReportInfo:
    report_id: UUID
    role: str
    category: str
    created_at: datetime
    content: str
    qdrant_points: int | None = None
    document_chunks: int = 0
    kg_relations: int = 0


@dataclass
class MemoryInfo:
    memory_id: UUID
    role: str
    created_at: datetime
    score: float
    final_answer: str


@dataclass
class AnalysisTarget:
    analysis_id: UUID
    ticker: str | None
    status: str | None
    failure_reason: str | None
    created_at: datetime | None
    selected_by: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    reports: list[ReportInfo] = field(default_factory=list)
    memories: list[MemoryInfo] = field(default_factory=list)
    memory_note: str = ""
    decisions: list[str] = field(default_factory=list)
    skip_reason: str | None = None


@dataclass
class OrphanDocument:
    document_id: str
    qdrant_points: int
    document_chunks: int
    embedding_jobs: int
    ticker: str | None
    category: str | None
    sample: str

    @property
    def has_content(self) -> bool:
        return self.qdrant_points > 0 or self.document_chunks > 0


@dataclass
class Plan:
    targets: list[AnalysisTarget] = field(default_factory=list)
    not_found: list[UUID] = field(default_factory=list)
    orphan_children: dict[str, int] = field(default_factory=dict)
    orphan_user_rows: dict[str, int] = field(default_factory=dict)
    orphan_documents: list[OrphanDocument] = field(default_factory=list)
    rag_sweep_note: str | None = None
    rag_sweep_allowed: bool = True
    kg_orphans_now: int = 0
    kg_orphans_after: list[str] = field(default_factory=list)
    unhandled_fks: list[str] = field(default_factory=list)
    reports: list[ReportInfo] = field(default_factory=list)
    reports_not_found: list[UUID] = field(default_factory=list)
    applied_reports: int = 0
    applied_analyses: int = 0
    applied_orphan_children: int = 0
    applied_documents: int = 0
    applied_kg_entities: int = 0

    @property
    def deletable(self) -> list[AnalysisTarget]:
        return [t for t in self.targets if t.skip_reason is None]


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(sql_text(sql), params)).scalar_one_or_none()


async def _count(session: AsyncSession, sql: str, **params: Any) -> int:
    return int(await _scalar(session, sql, **params) or 0)


async def _table_exists(
        session: AsyncSession, schema: str, table: str) -> bool:
    return bool(
        await _scalar(
            session, "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :s AND table_name = :t)", s=schema, t=table))


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


async def _select_ids(
        session: AsyncSession, ids: Sequence[UUID], failed: bool,
        since: date | None,
        until: date | None) -> tuple[dict[UUID, list[str]], list[UUID]]:
    selected: dict[UUID, list[str]] = {}

    def _add(analysis_id: UUID, why: str) -> None:
        selected.setdefault(analysis_id, []).append(why)

    not_found: list[UUID] = []
    for analysis_id in ids:
        exists = await _scalar(
            session, "SELECT id FROM ai.analyses WHERE id = :id",
            id=analysis_id)
        if exists is None:
            not_found.append(analysis_id)
        else:
            _add(analysis_id, "--id")
    if failed:
        for (analysis_id, ) in (await session.execute(sql_text(
                "SELECT id FROM ai.analyses WHERE status = 'failed' ORDER "
                "BY created_at"))).all():
            _add(analysis_id, "--failed")
    if since is not None and until is not None:
        for (analysis_id, ) in (await session.execute(
                sql_text(
                    "SELECT id FROM ai.analyses WHERE created_at >= :start "
                    "AND created_at < :end ORDER BY created_at"),
                {"start": _day_start(since),
                 "end": _day_start(until + timedelta(days=1))})).all():
            _add(analysis_id, f"--since {since} --until {until}")
    for table in ANALYSIS_KEYED_TABLES:
        for (analysis_id, ) in (await session.execute(sql_text(
                f"SELECT DISTINCT c.analysis_id FROM ai.{table} c "
                "WHERE c.analysis_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM ai.analyses a WHERE a.id = c.analysis_id)")
                                                      )).all():
            _add(analysis_id, f"orphan rows in ai.{table}")
    still_missing = [i for i in not_found if i not in selected]
    return selected, still_missing


async def _decision_lines(session: AsyncSession,
                          analysis_id: UUID) -> list[str]:
    rows = (
        await session.execute(
            sql_text(
                "SELECT d.id, d.user_id, d.action_taken, d.created_at, "
                "d.notes "
                "FROM ai.investment_decisions d "
                "JOIN ai.recommendations r ON r.id = d.recommendation_id "
                "WHERE r.analysis_id = :id ORDER BY d.created_at"),
            {"id": analysis_id})).all()
    return [
        f"decision {row[0]} user={row[1]} action_taken={row[2]} "
        f"created_at={row[3].isoformat()} notes={one_line(row[4], 100)}"
        for row in rows
    ]


async def _qdrant_point_count(document_id: str) -> int | None:
    try:
        result = await get_qdrant_client().count(
            collection_name=settings.QDRANT_COLLECTION_RESEARCH,
            count_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="document_id", match=qmodels.MatchValue(
                            value=document_id))
                ]), exact=True)
    except Exception as exc:
        print(
            f"  (Qdrant count unavailable for {document_id}: "
            f"{type(exc).__name__}: {exc})")
        return None
    return int(result.count)


async def _report_info(session: AsyncSession, row: Any) -> ReportInfo:
    report = ReportInfo(
        report_id=row[0], role=row[1], category=row[2], created_at=row[3],
        content=row[4] or "")
    report.document_chunks = await _count(
        session,
        "SELECT COUNT(*) FROM ai.document_chunks WHERE document_id = :r",
        r=report.report_id)
    report.kg_relations = await _count(
        session,
        "SELECT COUNT(*) FROM ai.kg_relations WHERE source_report_id = :r",
        r=report.report_id)
    report.qdrant_points = await _qdrant_point_count(str(report.report_id))
    return report


async def _reports(session: AsyncSession,
                   analysis_id: UUID) -> list[ReportInfo]:
    rows = (
        await session.execute(
            sql_text(
                "SELECT id, agent_role, category, created_at, content FROM "
                "ai.agent_reports "
                "WHERE analysis_id = :id ORDER BY created_at, id"),
            {"id": analysis_id})).all()
    return [await _report_info(session, row) for row in rows]


async def _single_reports(
        session: AsyncSession, report_ids: Sequence[UUID], plan: Plan) -> None:
    covered = {r.report_id for t in plan.deletable for r in t.reports}
    for report_id in dict.fromkeys(report_ids):
        if report_id in covered:
            continue
        row = (
            await session.execute(
                sql_text(
                    "SELECT id, agent_role, category, created_at, content "
                    "FROM ai.agent_reports WHERE id = :id"),
                {"id": report_id})).first()
        if row is None:
            plan.reports_not_found.append(report_id)
            continue
        plan.reports.append(await _report_info(session, row))


async def _counts(session: AsyncSession, analysis_id: UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ANALYSIS_KEYED_TABLES:
        counts[table] = await _count(
            session,
            f"SELECT COUNT(*) FROM ai.{table} WHERE analysis_id = :id",
            id=analysis_id)
    counts["recommendation_tiers"] = await _count(
        session,
        "SELECT COUNT(*) FROM ai.recommendation_tiers WHERE recommendation_id "
        "IN "
        "(SELECT id FROM ai.recommendations WHERE analysis_id = :id)",
        id=analysis_id)
    counts["recommendation_metrics"] = await _count(
        session, "SELECT COUNT(*) FROM ai.recommendation_metrics WHERE "
        "recommendation_id IN "
        "(SELECT id FROM ai.recommendations WHERE analysis_id = :id)",
        id=analysis_id)
    counts["agent_results"] = await _count(
        session, "SELECT COUNT(*) FROM ai.agent_results WHERE agent_run_id IN "
        "(SELECT id FROM ai.agent_runs WHERE analysis_id = :id)",
        id=analysis_id)
    counts["debate_arguments"] = await _count(
        session,
        "SELECT COUNT(*) FROM ai.debate_arguments WHERE debate_session_id IN "
        "(SELECT id FROM ai.debate_sessions WHERE analysis_id = :id)",
        id=analysis_id)
    return counts


async def memory_window(
        session: AsyncSession,
        target: AnalysisTarget) -> tuple[datetime, datetime, list[str]] | str:
    if target.created_at is None or target.ticker is None:
        return "analysis row is gone; no time or ticker to match memory by"
    runs = (
        await session.execute(
            sql_text(
                "SELECT MIN(r.completed_at), MAX(r.completed_at) FROM "
                "ai.agent_runs r "
                "WHERE r.analysis_id = :id"),
            {"id": target.analysis_id})).one()
    first_done, last_done = runs[0], runs[1]
    if first_done is None:
        return "no agent run completed, so no procedural memory was written"
    last_report = await _scalar(
        session,
        "SELECT MAX(created_at) FROM ai.agent_reports WHERE analysis_id = :id",
        id=target.analysis_id)
    last_activity = max(d for d in (last_done, last_report) if d is not None)
    roles = [
        str(r[0]) for r in (
            await session.execute(
                sql_text(
                    "SELECT DISTINCT g.name FROM ai.agent_runs r JOIN "
                    "ai.agents g "
                    "ON g.id = r.agent_id WHERE r.analysis_id = :id ORDER BY 1"
                ), {"id": target.analysis_id})).all()
    ]
    overlap = await _scalar(
        session,
        "SELECT a.id FROM ai.analyses a JOIN ai.agent_runs r ON r.analysis_id "
        "= a.id "
        "WHERE a.ticker = :t AND a.id <> :id AND a.created_at < :created "
        "AND r.completed_at >= :first LIMIT 1", t=target.ticker,
        id=target.analysis_id, created=target.created_at, first=first_done)
    if overlap is not None:
        return (
            f"an earlier {target.ticker} analysis ({overlap}) was still "
            f"running; "
            "its memory cannot be told apart, so none is matched")
    end = last_activity + MEMORY_TAIL
    next_start = await _scalar(
        session,
        "SELECT MIN(created_at) FROM ai.analyses WHERE ticker = :t AND id <> "
        ":id "
        "AND created_at > :created", t=target.ticker, id=target.analysis_id,
        created=target.created_at)
    if next_start is not None and next_start < end:
        end = next_start
    return first_done, end, roles


async def _memories(session: AsyncSession, target: AnalysisTarget) -> None:
    window = await memory_window(session, target)
    if isinstance(window, str):
        target.memory_note = window
        return
    start, end, roles = window
    target.memory_note = (
        f"window {start.isoformat()} .. {end.isoformat()}, "
        f"roles {', '.join(roles)}")
    rows = (
        await session.execute(
            sql_text(
                "SELECT m.id, g.name, m.created_at, m.score, "
                "m.content->>'final_answer' "
                "FROM ai.agent_memories m JOIN ai.agents g ON g.id = "
                "m.agent_id "
                "WHERE m.ticker = :t AND g.name = ANY(:roles) "
                "AND m.created_at >= :start AND m.created_at < :end "
                "ORDER BY m.created_at, m.id"), {
                    "t": target.ticker,
                    "roles": roles,
                    "start": start,
                    "end": end
                })).all()
    target.memories = [
        MemoryInfo(
            memory_id=r[0], role=r[1], created_at=r[2], score=float(r[3]),
            final_answer=r[4] or "") for r in rows
    ]


async def _target(
        session: AsyncSession, analysis_id: UUID,
        selected_by: list[str]) -> AnalysisTarget:
    row = (
        await session.execute(
            sql_text(
                "SELECT ticker, status, failure_reason, created_at FROM "
                "ai.analyses "
                "WHERE id = :id"), {"id": analysis_id})).first()
    target = AnalysisTarget(
        analysis_id=analysis_id, ticker=row[0] if row else None,
        status=row[1] if row else None, failure_reason=row[2] if row else None,
        created_at=row[3] if row else None, selected_by=selected_by)
    if target.status in IN_FLIGHT_STATUSES:
        target.skip_reason = (
            f"status is {target.status} (may still be running)")
    target.decisions = await _decision_lines(session, analysis_id)
    if target.decisions and target.skip_reason is None:
        target.skip_reason = (
            "has user investment decisions -- needs owner review")
    target.counts = await _counts(session, analysis_id)
    target.reports = await _reports(session, analysis_id)
    await _memories(session, target)
    return target


def _orphan_where(child: str, column: str, parent: str) -> str:
    return (
        f"FROM ai.{child} c WHERE c.{column} IS NOT NULL AND NOT EXISTS "
        f"(SELECT 1 FROM ai.{parent} p WHERE p.id = c.{column})")


async def _orphan_children(session: AsyncSession, plan: Plan) -> None:
    for child, column, parent, deletable in ORPHAN_CHILD_CHECKS:
        n = await _count(
            session, "SELECT COUNT(*) " + _orphan_where(child, column, parent))
        key = f"ai.{child}.{column} -> ai.{parent}"
        if deletable:
            plan.orphan_children[key] = n
        else:
            plan.orphan_user_rows[key] = n


async def _qdrant_document_ids() -> dict[str, dict[str, Any]]:
    client = get_qdrant_client()
    found: dict[str, dict[str, Any]] = {}
    offset: Any = None
    while True:
        points, offset = await client.scroll(
            collection_name=settings.QDRANT_COLLECTION_RESEARCH,
            limit=_QDRANT_SCROLL_BATCH, offset=offset, with_payload=True,
            with_vectors=False)
        for point in points:
            payload = point.payload or {}
            doc = str(payload.get("document_id") or "")
            if not doc:
                continue
            entry = found.setdefault(doc, {"points": 0, "payload": payload})
            entry["points"] += 1
        if offset is None:
            return found


def rag_sweep_refusal(orphan_count: int, total_count: int) -> str | None:
    if (total_count
            and orphan_count > MAX_ORPHAN_DOCUMENT_FRACTION * total_count):
        return (
            f"{orphan_count} of {total_count} documents look orphaned -- more "
            f"than "
            f"{MAX_ORPHAN_DOCUMENT_FRACTION:.0%}, which points at a wrong "
            f"ownership "
            "lookup rather than dirt; the RAG orphan sweep is refused")
    return None


async def _orphan_documents(session: AsyncSession, plan: Plan) -> None:
    if not await _table_exists(session, "core", "documents"):
        plan.rag_sweep_allowed = False
        plan.rag_sweep_note = (
            "core.documents is not readable from here; the RAG "
            "orphan sweep is skipped")
        return
    owned = {
        str(r[0])
        for r in (
            await session.execute(
                sql_text(
                    "SELECT id FROM core.documents UNION SELECT id FROM "
                    "ai.agent_reports"))).all()
    }
    try:
        in_qdrant = await _qdrant_document_ids()
    except Exception as exc:
        plan.rag_sweep_allowed = False
        plan.rag_sweep_note = (
            f"Qdrant scroll failed ({type(exc).__name__}: {exc}); "
            "the RAG orphan sweep is skipped")
        return
    chunk_docs = {
        str(r[0]): int(r[1])
        for r in (
            await session.execute(
                sql_text(
                    "SELECT document_id, COUNT(*) FROM ai.document_chunks "
                    "GROUP BY document_id"))).all()
    }
    job_docs = {
        str(r[0]): int(r[1])
        for r in (
            await session.execute(
                sql_text(
                    "SELECT document_id, COUNT(*) FROM ai.embedding_jobs "
                    "GROUP BY document_id"))).all()
    }
    all_docs = set(in_qdrant) | set(chunk_docs) | set(job_docs)
    orphans = sorted(all_docs - owned)
    for doc in orphans:
        payload = in_qdrant.get(doc, {}).get("payload", {})
        plan.orphan_documents.append(
            OrphanDocument(
                document_id=doc,
                qdrant_points=int(in_qdrant.get(doc, {}).get("points", 0)),
                document_chunks=chunk_docs.get(doc, 0),
                embedding_jobs=job_docs.get(doc, 0),
                ticker=payload.get("ticker"), category=payload.get("category"),
                sample=one_line(payload.get("text"), 160)))
    content_docs = set(in_qdrant) | set(chunk_docs)
    refusal = rag_sweep_refusal(
        sum(1 for d in plan.orphan_documents if d.has_content),
        len(content_docs))
    if refusal is not None:
        plan.rag_sweep_allowed = False
        plan.rag_sweep_note = refusal


async def _kg_orphans(session: AsyncSession, plan: Plan) -> None:
    plan.kg_orphans_now = await _count(
        session,
        "SELECT COUNT(*) FROM ai.kg_entities e WHERE NOT EXISTS (SELECT 1 "
        "FROM "
        "ai.kg_relations r WHERE r.source_entity_id = e.id "
        "OR r.target_entity_id = e.id)")
    report_ids = [r.report_id for t in plan.deletable for r in t.reports]
    report_ids += [r.report_id for r in plan.reports]
    if not report_ids:
        return
    rows = (
        await session.execute(
            sql_text(
                "SELECT e.name, e.entity_type FROM ai.kg_entities e WHERE "
                "EXISTS "
                "(SELECT 1 FROM ai.kg_relations r WHERE r.source_report_id = "
                "ANY(:rids) "
                " AND (r.source_entity_id = e.id OR r.target_entity_id = "
                "e.id)) "
                "AND NOT EXISTS (SELECT 1 FROM ai.kg_relations r WHERE "
                " (r.source_report_id IS NULL OR r.source_report_id <> "
                "ALL(:rids)) "
                " AND (r.source_entity_id = e.id OR r.target_entity_id = "
                "e.id)) "
                "ORDER BY e.name"), {"rids": report_ids})).all()
    plan.kg_orphans_after = [f"[{r[1]}] {r[0]!r}" for r in rows]


async def build_plan(
        session: AsyncSession, ids: Sequence[UUID] = (), failed: bool = False,
        since: date | None = None, until: date | None = None,
        report_ids: Sequence[UUID] = ()) -> Plan:
    plan = Plan()
    plan.unhandled_fks = await check_foreign_key_coverage(
        session, FK_ROOTS, HANDLED_FKS)
    selected, plan.not_found = await _select_ids(
        session, ids, failed, since, until)
    for analysis_id, why in selected.items():
        plan.targets.append(await _target(session, analysis_id, why))
    plan.targets.sort(
        key=lambda t:
        (t.created_at or datetime.min.replace(tzinfo=UTC), str(t.analysis_id)))
    await _single_reports(session, report_ids, plan)
    await _orphan_children(session, plan)
    await _orphan_documents(session, plan)
    await _kg_orphans(session, plan)
    return plan


def _print_report(r: ReportInfo) -> None:
    print(
        f"    report {r.report_id} role={r.role} "
        f"category={r.category} "
        f"created_at={r.created_at.isoformat()} -> Qdrant points="
        f"{r.qdrant_points if r.qdrant_points is not None else '?'}, "
        f"document_chunks={r.document_chunks}, "
        f"kg_relations={r.kg_relations}")
    print(f"      content: {one_line(r.content, 160)}")


def print_plan(plan: Plan) -> None:
    deletable = plan.deletable
    skipped = [t for t in plan.targets if t.skip_reason is not None]
    print(f"\n--- 1. Analyses to delete: {len(deletable)} ---")
    totals: dict[str, int] = {}
    for t in deletable:
        when = (
            t.created_at.isoformat()
            if t.created_at else "(analysis row gone)")
        print(
            f"{when} {t.ticker or '-':6} analysis={t.analysis_id} "
            f"status={t.status or '-'} "
            f"failure_reason={t.failure_reason or '-'} "
            f"selected by: {'; '.join(t.selected_by)}")
        rows = ", ".join(f"{k}={v}" for k, v in t.counts.items() if v)
        print(f"    rows: {rows or '(none)'}")
        for table, n in t.counts.items():
            totals[table] = totals.get(table, 0) + n
        for r in t.reports:
            _print_report(r)
        print(
            f"    procedural memory: {t.memory_note}; {len(t.memories)} row(s)"
        )
        for m in t.memories:
            print(
                f"      memory {m.memory_id} role={m.role} "
                f"created_at={m.created_at.isoformat()} score={m.score} "
                f"final_answer={one_line(m.final_answer, 140)}")
    totals["agent_reports (RAG notes)"] = sum(
        len(t.reports) for t in deletable)
    totals["agent_memories"] = sum(len(t.memories) for t in deletable)
    print(f"Totals: {dict(sorted(totals.items()))}")
    if skipped:
        print(f"\nSKIPPED (never deleted): {len(skipped)}")
        for t in skipped:
            print(f"  {t.ticker} analysis={t.analysis_id}: {t.skip_reason}")
            for line in t.decisions:
                print(f"    {line}")
    if plan.not_found:
        print(
            "\nRequested ids not found: " +
            ", ".join(map(str, plan.not_found)))

    if plan.reports or plan.reports_not_found:
        print(
            f"\n--- 1b. Archived notes to delete on their own (--report): "
            f"{len(plan.reports)} ---")
        for r in plan.reports:
            _print_report(r)
        if plan.reports_not_found:
            print(
                "    report ids not found: " +
                ", ".join(map(str, plan.reports_not_found)))

    print("\n--- 2. Orphan sweep ---")
    print("Child rows whose parent row is gone (deleted):")
    for key, n in plan.orphan_children.items():
        print(f"  {key}: {n}")
    print("User-data rows whose parent is gone (listed only, never deleted):")
    for key, n in plan.orphan_user_rows.items():
        print(f"  {key}: {n}")
    with_content = [d for d in plan.orphan_documents if d.has_content]
    jobs_only = [d for d in plan.orphan_documents if not d.has_content]
    print(
        f"RAG documents owned by nothing (no core.documents / agent_reports "
        f"row): "
        f"{len(with_content)} with content, {len(jobs_only)} with only "
        f"leftover "
        f"embedding_jobs rows ({sum(d.embedding_jobs for d in jobs_only)} "
        f"rows)")
    for d in jobs_only[:10]:
        print(
            f"  jobs-only document {d.document_id}: "
            f"embedding_jobs={d.embedding_jobs}")
    if len(jobs_only) > 10:
        print(f"  ... and {len(jobs_only) - 10} more jobs-only documents")
    for d in with_content:
        print(
            f"  document {d.document_id} ticker={d.ticker} "
            f"category={d.category} "
            f"Qdrant points={d.qdrant_points} "
            f"document_chunks={d.document_chunks} "
            f"embedding_jobs={d.embedding_jobs}")
        print(f"    text: {d.sample}")
    if plan.rag_sweep_note:
        print(f"  NOTE: {plan.rag_sweep_note}")
    print(
        f"KG entities with no relation now: {plan.kg_orphans_now}; becoming "
        f"orphans "
        f"when this run deletes their last relation: "
        f"{len(plan.kg_orphans_after)}")
    for line in plan.kg_orphans_after[:40]:
        print(f"  {line}")
    if len(plan.kg_orphans_after) > 40:
        print(f"  ... and {len(plan.kg_orphans_after) - 40} more")


async def _exec(session: AsyncSession, sql: str, **params: Any) -> None:
    await session.execute(sql_text(sql), params)


async def _apply_target(session: AsyncSession, target: AnalysisTarget) -> str:
    aid = target.analysis_id
    if target.status is not None:
        status = await _scalar(
            session,
            "SELECT status FROM ai.analyses WHERE id = :id FOR UPDATE", id=aid)
        if status is None:
            return "already gone"
        if status in IN_FLIGHT_STATUSES:
            return f"status is now {status}"
    if await _decision_lines(session, aid):
        return "has user investment decisions"

    report_ids = [
        r[0] for r in (
            await session.execute(
                sql_text(
                    "SELECT id FROM ai.agent_reports WHERE analysis_id = :id"),
                {"id": aid})).all()
    ]
    for report_id in report_ids:
        await delete_archived_report(session, report_id)
    await _exec(
        session,
        "DELETE FROM ai.recommendation_tiers WHERE recommendation_id IN "
        "(SELECT id FROM ai.recommendations WHERE analysis_id = :id)", id=aid)
    await _exec(
        session,
        "DELETE FROM ai.recommendation_metrics WHERE recommendation_id IN "
        "(SELECT id FROM ai.recommendations WHERE analysis_id = :id)", id=aid)
    await _exec(
        session, "DELETE FROM ai.agent_results WHERE agent_run_id IN "
        "(SELECT id FROM ai.agent_runs WHERE analysis_id = :id)", id=aid)
    await _exec(
        session, "DELETE FROM ai.debate_arguments WHERE debate_session_id IN "
        "(SELECT id FROM ai.debate_sessions WHERE analysis_id = :id)", id=aid)
    memory_ids = [m.memory_id for m in target.memories]
    if memory_ids:
        await _exec(
            session, "DELETE FROM ai.agent_memories WHERE id = ANY(:ids)",
            ids=memory_ids)
    for table in ANALYSIS_KEYED_TABLES:
        await _exec(
            session, f"DELETE FROM ai.{table} WHERE analysis_id = :id", id=aid)
    await _exec(session, "DELETE FROM ai.analyses WHERE id = :id", id=aid)
    return "deleted"


async def _apply_report(session: AsyncSession, report_id: UUID) -> str:
    exists = await _scalar(
        session, "SELECT 1 FROM ai.agent_reports WHERE id = :r FOR UPDATE",
        r=report_id)
    if exists is None:
        return "already gone"
    await delete_archived_report(session, report_id)
    return "deleted"


async def _apply_orphans(
        plan: Plan, session_factory: async_sessionmaker[AsyncSession]) -> None:
    for child, column, parent, deletable in ORPHAN_CHILD_CHECKS:
        if not deletable:
            continue
        async with session_factory() as session:
            deleted = (
                await session.execute(
                    sql_text(
                        f"DELETE FROM ai.{child} WHERE id IN (SELECT c.id " +
                        _orphan_where(child, column, parent) +
                        ") RETURNING id"))).all()
            await session.commit()
        plan.applied_orphan_children += len(deleted)
        if deleted:
            print(f"  orphan ai.{child} rows deleted: {len(deleted)}")
    jobs_only = [
        d.document_id for d in plan.orphan_documents if not d.has_content
    ]
    if jobs_only:
        async with session_factory() as session:
            deleted = (
                await session.execute(
                    sql_text(
                        "DELETE FROM ai.embedding_jobs j "
                        "WHERE j.document_id = ANY(CAST(:ids AS uuid[])) "
                        "AND NOT EXISTS (SELECT 1 FROM core.documents d "
                        "WHERE d.id = j.document_id) "
                        "AND NOT EXISTS (SELECT 1 FROM ai.agent_reports r "
                        "WHERE r.id = j.document_id) "
                        "AND NOT EXISTS (SELECT 1 FROM ai.document_chunks c "
                        "WHERE c.document_id = j.document_id) RETURNING j.id"),
                    {"ids": jobs_only})).all()
            await session.commit()
        plan.applied_documents += len(jobs_only)
        print(
            f"  leftover embedding_jobs rows deleted: {len(deleted)} "
            f"(for {len(jobs_only)} jobs-only documents)")
    if plan.rag_sweep_allowed:
        for doc in plan.orphan_documents:
            if not doc.has_content:
                continue
            async with session_factory() as session:
                owned = await _scalar(
                    session,
                    "SELECT 1 FROM core.documents WHERE id = CAST(:d AS uuid) "
                    "UNION "
                    "SELECT 1 FROM ai.agent_reports WHERE id = CAST(:d AS "
                    "uuid)", d=doc.document_id)
                if owned is not None:
                    print(f"  document {doc.document_id}: now owned, skipped")
                    continue
                result = await delete_document_completely(
                    doc.document_id, session)
                await session.commit()
            plan.applied_documents += 1
            print(
                f"  orphan document {doc.document_id}: Qdrant/BM25 chunks "
                f"removed="
                f"{result.qdrant_bm25_chunks_removed}")
    else:
        print(f"  RAG orphan sweep not applied: {plan.rag_sweep_note}")
    async with session_factory() as session:
        deleted = (
            await session.execute(
                sql_text(
                    "DELETE FROM ai.kg_entities e WHERE NOT EXISTS (SELECT 1 "
                    "FROM "
                    "ai.kg_relations r WHERE r.source_entity_id = e.id "
                    "OR r.target_entity_id = e.id) RETURNING e.id"))).all()
        await session.commit()
    plan.applied_kg_entities = len(deleted)
    print(f"  orphan kg_entities deleted: {len(deleted)}")


async def apply_plan(
        plan: Plan, session_factory: async_sessionmaker[AsyncSession]) -> None:
    for target in plan.deletable:
        async with session_factory() as session:
            outcome = await _apply_target(session, target)
            if outcome == "deleted":
                await session.commit()
            else:
                await session.rollback()
        plan.applied_analyses += int(outcome == "deleted")
        print(
            f"  {target.ticker or '-'} analysis {target.analysis_id}: "
            f"{outcome}")
    for report in plan.reports:
        async with session_factory() as session:
            outcome = await _apply_report(session, report.report_id)
            if outcome == "deleted":
                await session.commit()
            else:
                await session.rollback()
        plan.applied_reports += int(outcome == "deleted")
        print(f"  archived note {report.report_id} ({report.role}): {outcome}")
    await _apply_orphans(plan, session_factory)


async def run(
    confirm: bool, ids: Sequence[UUID] = (), failed: bool = False,
    since: date | None = None, until: date | None = None,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    report_ids: Sequence[UUID] = ()
) -> Plan:
    print(mode_banner(confirm))
    async with session_factory() as session:
        plan = await build_plan(session, ids, failed, since, until, report_ids)
    print_plan(plan)
    if not confirm:
        print("\nPreview only -- re-run with --confirm to apply.")
        return plan
    if plan.unhandled_fks:
        return plan
    print("\n=== Applying ===")
    await apply_plan(plan, session_factory)
    print(
        f"\nDone: {plan.applied_analyses} analysis/analyses deleted, "
        f"{plan.applied_reports} archived note(s) deleted on their own, "
        f"{plan.applied_orphan_children} orphan child row(s), "
        f"{plan.applied_documents} orphan RAG document(s), "
        f"{plan.applied_kg_entities} orphan KG entit(y/ies). Next: restart "
        f"ai-service "
        "(rebuilds its BM25 index from Qdrant), then POST "
        "/internal/review/run.")
    return plan


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.delete_analyses", description=(
            "Delete whole analyses (full cascade) and sweep orphaned data."))
    parser.add_argument(
        "--id", dest="ids", action="append", type=UUID, default=[],
        help="analysis id (repeatable)")
    parser.add_argument(
        "--report", dest="report_ids", action="append", type=UUID, default=[],
        help="archived agent note id to delete on its own "
        "(repeatable); its analysis is kept")
    parser.add_argument(
        "--failed", action="store_true",
        help="every analysis with status 'failed'")
    parser.add_argument(
        "--since", type=date.fromisoformat,
        help="YYYY-MM-DD, UTC, inclusive (needs --until)")
    parser.add_argument(
        "--until", type=date.fromisoformat,
        help="YYYY-MM-DD, UTC, inclusive (needs --since)")
    parser.add_argument(
        "--confirm", action="store_true", help="apply the changes")
    args = parser.parse_args(argv)
    if (args.since is None) != (args.until is None):
        parser.error("--since and --until must be given together")
    if args.since is not None and args.until < args.since:
        parser.error("--until is before --since")
    return args


if __name__ == "__main__":
    _args = parse_args(sys.argv[1:])
    _plan = asyncio.run(
        run(
            confirm=_args.confirm, ids=_args.ids, failed=_args.failed,
            since=_args.since, until=_args.until, report_ids=_args.report_ids))
    sys.exit(1 if _plan.unhandled_fks else 0)
