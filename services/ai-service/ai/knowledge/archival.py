from datetime import UTC, datetime
from uuid import UUID, uuid4
import structlog
from ai.agents.evidence_grounding import non_english_reason
from ai.knowledge.extraction import extract_entities_relations
from ai.knowledge.ticker_names import core_company_name, resolve_ticker_name
from ai.llm.base import LLMTier
from ai.rag.chunking import Chunk, chunk_document
from ai.rag.embedding import (cosine_similarity, embed_texts,
                              embedding_model_name)
from ai.rag.indexing import (delete_document_chunks,
                             delete_document_completely, index_chunks,
                             record_document_chunks)
from ai.rag.visibility import ChunkVisibility
from config import settings
from database import SessionLocal
from sqlalchemy import select
from domains.analysis.model import Analysis
from domains.knowledge.model import AgentReport
from domains.knowledge.repository import (AgentReportRepository,
                                          KnowledgeGraphRepository)
from domains.rag.repository import EmbeddingJobRepository
from shared.enums import AgentRole, DocumentCategory, DocumentStatus

logger = structlog.get_logger(__name__)

_CATEGORY_BY_ROLE: dict[AgentRole, DocumentCategory] = {
    AgentRole.MACRO: DocumentCategory.MACRO_RESEARCH,
    AgentRole.NEWS: DocumentCategory.NEWS,
    AgentRole.FUNDAMENTAL: DocumentCategory.ANALYST_REPORT,
    AgentRole.RISK: DocumentCategory.RISK_ANALYSIS
}

_MIN_WORDS_TO_ARCHIVE = 40

_NEAR_DUPLICATE_DEDUP_LOOKBACK = 3
_NEAR_DUPLICATE_SEMANTIC_THRESHOLD = 0.90


def near_duplicate_threshold() -> float:
    configured = settings.ARCHIVAL_NEAR_DUPLICATE_THRESHOLD
    return (
        configured
        if configured is not None else _NEAR_DUPLICATE_SEMANTIC_THRESHOLD)


def is_archivable_role(role: AgentRole) -> bool:
    return role in _CATEGORY_BY_ROLE


_UNKNOWN_OWNER = object()


async def _analysis_owners(
        analysis_ids: list[UUID]) -> dict[UUID, UUID | None]:
    if not analysis_ids:
        return {}
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Analysis.id,
                       Analysis.user_id).where(Analysis.id.in_(analysis_ids))
            )).all()
    return {row[0]: row[1] for row in rows}


async def _archive_owner(analysis_id: UUID) -> object:
    try:
        owners = await _analysis_owners([analysis_id])
    except Exception:
        logger.exception(
            "archival.owner_lookup_failed", analysis_id=str(analysis_id))
        return _UNKNOWN_OWNER
    if analysis_id not in owners:
        logger.warning(
            "archival.owner_analysis_not_found", analysis_id=str(analysis_id))
        return _UNKNOWN_OWNER
    return owners[analysis_id]


def _archive_visibility(owner: object) -> ChunkVisibility:
    if owner is _UNKNOWN_OWNER:
        return ChunkVisibility()
    if owner is None:
        return ChunkVisibility.system()
    return ChunkVisibility.for_owner(owner)


async def _find_near_duplicate_of_recent_report(
        *, category: DocumentCategory, content: str, ticker: str | None,
        owner: object = _UNKNOWN_OWNER) -> AgentReport | None:
    async with SessionLocal() as session:
        recent = await AgentReportRepository(session).list_recent_by_category(
            category.value, ticker=ticker,
            limit=_NEAR_DUPLICATE_DEDUP_LOOKBACK)
    if recent and owner is not _UNKNOWN_OWNER:
        report_owners = await _analysis_owners(
            [report.analysis_id for report in recent])
        recent = [
            report for report in recent if report.analysis_id in report_owners
            and report_owners[report.analysis_id] == owner
        ]
    if not recent:
        return None

    vectors = await embed_texts([content] +
                                [report.content for report in recent])
    new_vector, recent_vectors = vectors[0], vectors[1:]

    for report, recent_vector in zip(recent, recent_vectors, strict=True):
        similarity = cosine_similarity(new_vector, recent_vector)
        if similarity >= near_duplicate_threshold():
            logger.info(
                "archival.found_near_duplicate", category=category.value,
                similarity=round(similarity,
                                 3), matched_report_id=str(report.id))
            return report
    return None


async def _supersede_report(old_report: AgentReport) -> None:
    async with SessionLocal() as session:
        deletion = await delete_document_completely(
            str(old_report.id), session)
        relations_removed = await KnowledgeGraphRepository(
            session).delete_relations_for_report(old_report.id)
        await AgentReportRepository(session).delete(old_report.id)
        await session.commit()

    logger.info(
        "archival.superseded_near_duplicate", old_report_id=str(old_report.id),
        category=old_report.category,
        rag_chunks_removed=deletion.qdrant_bm25_chunks_removed,
        kg_relations_removed=relations_removed)


async def _persist_report_rows(
        *, report_id: UUID, analysis_id: UUID, role: AgentRole,
        ticker: str | None, category: DocumentCategory, content: str,
        chunks: list[Chunk]) -> None:
    async with SessionLocal() as session:
        await AgentReportRepository(session).create(
            report_id=report_id, analysis_id=analysis_id,
            agent_role=role.value, ticker=ticker, category=category.value,
            content=content)
        job = await EmbeddingJobRepository(session).create(
            document_id=report_id, embedding_model=embedding_model_name(),
            status=DocumentStatus.EMBEDDING.value)
        await record_document_chunks(
            session, document_id=str(report_id), chunks=chunks)
        await EmbeddingJobRepository(session).set_status(
            job.id, DocumentStatus.READY.value)
        await session.commit()


async def archive_agent_report(
        *, analysis_id: UUID, role: AgentRole, ticker: str | None,
        content: str, hit_iteration_cap: bool = False,
        verification_warnings: list[str] | None = None,
        is_well_supported: bool = True, recycled_share: float = 0.0) -> None:
    if role not in _CATEGORY_BY_ROLE:
        return
    if not is_well_supported:
        logger.info(
            "archival.skipped_not_well_supported", role=role.value,
            analysis_id=str(analysis_id))
        return
    if recycled_share >= settings.ARCHIVAL_RECYCLED_SHARE_MAX:
        logger.info(
            "archival.skipped_recycled_prior_notes", role=role.value,
            analysis_id=str(analysis_id),
            recycled_share=round(recycled_share, 2))
        return
    if verification_warnings:
        logger.info(
            "archival.skipped_verification_warning", role=role.value,
            analysis_id=str(analysis_id), warnings=verification_warnings)
        return
    if hit_iteration_cap:
        logger.info(
            "archival.skipped_hit_iteration_cap", role=role.value,
            analysis_id=str(analysis_id))
        return
    non_english = non_english_reason(content)
    if non_english is not None:
        logger.info(
            "archival.skipped_non_english", role=role.value,
            analysis_id=str(analysis_id), reason=non_english)
        return
    if len(content.split()) < _MIN_WORDS_TO_ARCHIVE:
        logger.info(
            "archival.skipped_too_short", role=role.value,
            word_count=len(content.split()))
        return

    category = _CATEGORY_BY_ROLE[role]
    owner = await _archive_owner(analysis_id)
    visibility = _archive_visibility(owner)

    duplicate = (
        await _find_near_duplicate_of_recent_report(
            category=category, content=content, ticker=ticker, owner=owner)
        if owner is not _UNKNOWN_OWNER else None)

    report_id = uuid4()

    report_date = datetime.now(UTC).date().isoformat()
    chunks = chunk_document(
        content, category, metadata={"agent_role": role.value})
    if not chunks:
        logger.info("archival.skipped_zero_chunks", role=role.value)
        return

    embeddings = await embed_texts([c.text for c in chunks])

    await index_chunks(
        document_id=str(report_id), ticker=ticker, category=category,
        chunks=chunks, embeddings=embeddings, source=f"agent:{role.value}",
        content_date=report_date, visibility=visibility)

    try:
        await _persist_report_rows(
            report_id=report_id, analysis_id=analysis_id, role=role,
            ticker=ticker, category=category, content=content, chunks=chunks)
    except Exception:
        try:
            await delete_document_chunks(str(report_id))
        except Exception:
            logger.exception(
                "archival.orphan_chunk_cleanup_failed",
                report_id=str(report_id))
        raise

    logger.info(
        "archival.report_archived", role=role.value, report_id=str(report_id),
        chunk_count=len(chunks))

    if duplicate is not None:
        try:
            await _supersede_report(duplicate)
        except Exception:
            logger.exception(
                "archival.supersede_failed", old_report_id=str(duplicate.id),
                new_report_id=str(report_id))

    try:
        await _extract_and_store_kg(
            report_id=report_id, content=content, ticker=ticker)
    except Exception:
        logger.exception(
            "archival.kg_extraction_failed", report_id=str(report_id))


async def _extract_and_store_kg(
        *, report_id: UUID, content: str, ticker: str | None) -> None:
    canonical_name: str | None = None
    if ticker is not None:
        try:
            async with SessionLocal() as name_session:
                canonical_name = await resolve_ticker_name(
                    name_session, ticker)
                await name_session.commit()
        except Exception:
            logger.warning(
                "kg_extraction.ticker_name_resolution_failed", ticker=ticker)

    triples = await extract_entities_relations(
        content, llm_tier=LLMTier(settings.KG_EXTRACTION_LLM_TIER),
        ticker=ticker, canonical_name=canonical_name)
    if not triples:
        return
    triple_text = " ".join(
        f"{t.source} {t.relation} {t.target}" for t in triples)
    non_english = non_english_reason(triple_text)
    if non_english is not None:
        logger.info(
            "archival.kg_skipped_non_english", report_id=str(report_id),
            reason=non_english)
        return

    if ticker is not None and canonical_name is not None:
        canonical_core = core_company_name(canonical_name)
        for triple in triples:
            if core_company_name(triple.source) == canonical_core:
                triple.source = ticker
            if core_company_name(triple.target) == canonical_core:
                triple.target = ticker

    async with SessionLocal() as session:
        kg = KnowledgeGraphRepository(session)
        for triple in triples:
            source_entity = await kg.get_or_create_entity(
                name=triple.source, entity_type=triple.source_type)
            target_entity = await kg.get_or_create_entity(
                name=triple.target, entity_type=triple.target_type)
            await kg.create_relation(
                source_entity_id=source_entity.id,
                target_entity_id=target_entity.id,
                relation_type=triple.relation, source_report_id=report_id,
                confidence=triple.confidence, kind=triple.kind,
                as_of=triple.as_of)
        await session.commit()

    logger.info(
        "archival.kg_extracted", report_id=str(report_id),
        triple_count=len(triples),
        event_count=sum(1 for t in triples
                        if t.kind == "event"), event_as_of_after_today=sum(
                            1 for t in triples
                            if t.kind == "event" and t.as_of is not None
                            and t.as_of > datetime.now(UTC).date()))
