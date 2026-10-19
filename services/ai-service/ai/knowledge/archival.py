import json
import math
from datetime import UTC, date, datetime
from uuid import UUID, uuid4
import structlog
from ai.agents.evidence_grounding import non_english_reason
from ai.knowledge.kg_context import core_company_name, resolve_ticker_name
from ai.llm.base import (AGENT_LLM_CALL_RETRIES, LLMError, LLMMessage, LLMTier,
                         log_unparseable, recover_wrapped_json,
                         strip_json_code_fence)
from ai.llm.router import LLMRouter
from ai.rag.chunking import Chunk, chunk_document
from ai.rag.embedding import (cosine_similarity, embed_texts,
                              embedding_model_name)
from ai.rag.indexing import (delete_document_chunks,
                             delete_document_completely, index_chunks,
                             record_document_chunks)
from ai.rag.visibility import ChunkVisibility
from config import settings
from database import SessionLocal
from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
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
        owner: object) -> AgentReport | None:
    async with SessionLocal() as session:
        recent = await AgentReportRepository(session).list_recent_by_category(
            category.value, ticker=ticker,
            limit=_NEAR_DUPLICATE_DEDUP_LOOKBACK)
    if recent:
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
        if similarity >= settings.ARCHIVAL_NEAR_DUPLICATE_THRESHOLD:
            logger.info(
                "archival.found_near_duplicate", category=category.value,
                similarity=round(similarity,
                                 3), matched_report_id=str(report.id))
            return report
    return None


async def delete_archived_report(
        session: AsyncSession, report_id: UUID) -> tuple[int, int]:
    deletion = await delete_document_completely(str(report_id), session)
    relations_removed = await KnowledgeGraphRepository(
        session).delete_relations_for_report(report_id)
    await AgentReportRepository(session).delete(report_id)
    return deletion.qdrant_bm25_chunks_removed, relations_removed


async def _supersede_report(old_report: AgentReport) -> None:
    async with SessionLocal() as session:
        chunks_removed, relations_removed = await delete_archived_report(
            session, old_report.id)
        await session.commit()

    logger.info(
        "archival.superseded_near_duplicate", old_report_id=str(old_report.id),
        category=old_report.category, rag_chunks_removed=chunks_removed,
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


_ENTITY_TYPES = (
    "company", "person", "institution", "economic_indicator", "event",
    "concept", "product", "position", "country")

_MAX_TRIPLES = 15
KG_KINDS = ("stable", "event")
_EXTRACTION_MAX_TOKENS = 2048

_SYSTEM_PROMPT = (
    "Extract a small knowledge graph from the report below: a list of "
    "(source entity, relation, target entity) triples that capture its key "
    'factual claims. Use short, canonical entity names (e.g. "AMD", not '
    '"the company" or "Advanced Micro Devices Inc." if the report itself '
    'consistently says "AMD") -- consistent naming matters more than '
    "completeness, since two different names for the same real-world thing "
    "become two different graph nodes. "
    f"entity_type must be one of: {', '.join(_ENTITY_TYPES)}. "
    'relation should be a short verb phrase (e.g. "raised_rates_by", '
    '"affects", "reported_revenue_of"), not a full sentence. '
    'kind is "stable" for a lasting relationship (a competitor, a supplier, '
    'a customer, a product, a role) or "event" for a fact whose status or '
    "value can change (a lawsuit, investigation or regulatory action and its "
    "status, a deal, guidance, a metric reading). as_of is the date the "
    'report states the fact as of, as "YYYY-MM-DD", or null when it gives '
    "none. "
    f"Return at most {_MAX_TRIPLES} triples -- prioritize the most important "
    "claims, not every mentioned fact. Return ONLY a JSON array, no "
    "commentary, in this exact shape: "
    '[{"source": "...", "source_type": "...", "relation": "...", '
    '"target": "...", "target_type": "...", "confidence": 0.0-1.0, '
    '"kind": "stable" or "event", "as_of": "YYYY-MM-DD" or null}]. '
    "If the report contains nothing worth extracting, return [].")


class KgTriple(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=200)
    source_type: str
    relation: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=200)
    target_type: str
    confidence: float = 0.5
    kind: str | None = None
    as_of: date | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _known_kind(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in KG_KINDS:
            return value.strip().lower()
        return None

    @field_validator("as_of", mode="before")
    @classmethod
    def _iso_date(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError:
                return None
        return value if isinstance(value, date) else None

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: object) -> object:
        if value is None:
            return 0.5
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return value
        try:
            number = float(value)
        except ValueError:
            return value
        if not math.isfinite(number):
            raise ValueError("confidence must be a finite number")
        return min(1.0, max(0.0, number))


def _parse_response(raw: str) -> list[KgTriple]:
    text = strip_json_code_fence(raw)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = recover_wrapped_json(raw, list)
        if data is None:
            log_unparseable("kg_extraction.unparseable_response", raw)
            return []

    if not isinstance(data, list):
        logger.warning("kg_extraction.response_not_a_list", raw=raw[:500])
        return []

    triples: list[KgTriple] = []
    for item in data[:_MAX_TRIPLES]:
        if isinstance(item, dict):
            recovered_fields = []
            if "source_type" not in item:
                item = {**item, "source_type": "concept"}
                recovered_fields.append("source_type")
            if "target_type" not in item:
                item = {**item, "target_type": "concept"}
                recovered_fields.append("target_type")
            if recovered_fields:
                logger.warning(
                    "kg_extraction.recovered_missing_type_field",
                    fields=recovered_fields, item=item)

        try:
            triple = KgTriple.model_validate(item)
        except ValidationError:
            logger.warning("kg_extraction.malformed_triple", item=item)
            continue
        if (triple.source_type not in _ENTITY_TYPES
                or triple.target_type not in _ENTITY_TYPES):
            logger.warning("kg_extraction.unknown_entity_type", item=item)
            continue
        triples.append(triple)
    return triples


async def extract_entities_relations(
        report_text: str, *, llm_router: LLMRouter | None = None,
        llm_tier: LLMTier = LLMTier.CHEAP, ticker: str | None = None,
        canonical_name: str | None = None) -> list[KgTriple]:
    router = llm_router or LLMRouter()
    user_content = report_text
    if ticker is not None and canonical_name is not None:
        user_content = (
            f"This report is specifically about ticker {ticker}" + (
                f", official name {canonical_name}"
                if canonical_name.upper() != ticker.upper() else "") +
            f'. Use "{ticker}" as the entity name for this company '
            "specifically, regardless of which name form the report text "
            "below actually uses.\n\n"
            f"{report_text}")
    try:
        response = await router.complete(
            llm_tier, [
                LLMMessage(role="system", content=_SYSTEM_PROMPT),
                LLMMessage(role="user", content=user_content)
            ], temperature=0.0, retries=AGENT_LLM_CALL_RETRIES,
            max_tokens=_EXTRACTION_MAX_TOKENS)
    except LLMError as exc:
        logger.info("kg_extraction.unavailable", error=str(exc))
        return []

    return _parse_response(response.content)
