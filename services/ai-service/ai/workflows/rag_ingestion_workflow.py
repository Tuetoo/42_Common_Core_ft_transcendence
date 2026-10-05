import asyncio
from pathlib import PurePosixPath
from uuid import UUID
import structlog
from ai.llm.base import LLMProviderTimeoutError, LLMRateLimitError
from ai.rag.chunking import chunk_document, parse_document
from ai.rag.embedding import (EmbeddingRateLimitedError, embed_texts,
                              embedding_model_name)
from ai.rag.indexing import (delete_document_chunks, index_chunks,
                             record_document_chunks)
from ai.rag.visibility import ChunkVisibility
from config import settings
from database import SessionLocal
from domains.rag.repository import (DocumentChunkRepository,
                                    EmbeddingJobRepository)
from shared.enums import DocumentCategory, DocumentStatus
from shared.events import publish_cross_service, publish_durable_event
from shared.events_registry import EventName
from shared.storage import download_bytes

logger = structlog.get_logger(__name__)

_TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_STATUS_PUBLISH_RETRY_WAITS_SECONDS = (1.0, 5.0, 15.0)


class TransientIngestionError(Exception):
    pass


def is_transient_ingestion_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, LLMRateLimitError,
                        LLMProviderTimeoutError, EmbeddingRateLimitedError)):
        return True
    raw_response = getattr(exc, "raw_response", None)
    status = getattr(raw_response, "status_code", None)
    if status is None:
        code = getattr(exc, "code", None)
        status = code if isinstance(code, int) else None
    if status in _TRANSIENT_STATUS_CODES:
        return True
    try:
        import httpx
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, httpx.TransportError)


async def _mark_failed_bookkeeping(
        document_id: str, job_id: UUID | None, *,
        points_deleted: bool) -> None:
    if job_id is None and not points_deleted:
        return
    if points_deleted:
        try:
            await delete_document_chunks(document_id)
        except Exception:
            logger.exception(
                "rag_ingestion_failure_chunk_cleanup_failed",
                document_id=document_id)
    try:
        async with SessionLocal() as session:
            if points_deleted:
                await DocumentChunkRepository(session).delete_for_document(
                    UUID(document_id))
            if job_id is not None:
                await EmbeddingJobRepository(session).set_status(
                    job_id, DocumentStatus.FAILED.value)
            await session.commit()
    except Exception:
        logger.exception(
            "rag_ingestion_failure_bookkeeping_failed",
            document_id=document_id)


async def _publish_status(document_id: str, status: DocumentStatus) -> None:
    payload = {"document_id": document_id, "status": status.value}
    logger.debug(
        "document_status_changed_published", document_id=document_id,
        status=status.value)
    for attempt, wait in enumerate(
            (*_STATUS_PUBLISH_RETRY_WAITS_SECONDS, None)):
        try:
            await publish_durable_event(
                settings, EventName.DOCUMENT_STATUS_CHANGED, payload,
                log=False)
            break
        except Exception as exc:
            if wait is None:
                raise
            logger.warning(
                "document_status_publish_retrying", document_id=document_id,
                attempt=attempt + 1, wait_seconds=wait, error=str(exc))
            await asyncio.sleep(wait)
    try:
        await publish_cross_service(
            settings, EventName.DOCUMENT_STATUS_CHANGED, payload, log=False)
    except Exception as exc:
        logger.warning(
            "document_status_live_publish_failed", document_id=document_id,
            error=str(exc))


async def run_rag_ingestion_workflow(
        *, document_id: str, storage_bucket: str, storage_key: str,
        document_type: str, category: str, ticker: str | None,
        content_date: str | None = None, filename: str | None = None,
        owner_id: str | None = None,
        organization_id: str | None = None) -> None:
    visibility = ChunkVisibility.from_upload_event(owner_id, organization_id)
    job_id: UUID | None = None
    points_deleted = False
    try:
        await _publish_status(document_id, DocumentStatus.PARSING)
        content = await download_bytes(
            settings, bucket=storage_bucket, key=storage_key)
        text = parse_document(content, document_type)
        if not text.strip():
            raise ValueError(
                f"document {document_id} parsed to empty text "
                f"(document_type={document_type!r}) -- likely a scanned/"
                "image-only PDF with no embedded text layer; this "
                "codebase has no OCR fallback")

        await _publish_status(document_id, DocumentStatus.CHUNKING)
        doc_category = DocumentCategory(category)
        chunks = chunk_document(
            text, doc_category, metadata={
                "ticker": ticker,
                "content_date": content_date
            })
        if not chunks:
            raise ValueError(f"document {document_id} produced zero chunks")

        async with SessionLocal() as session:
            job_repo = EmbeddingJobRepository(session)
            await job_repo.delete_for_document(UUID(document_id))
            job = await job_repo.create(
                document_id=UUID(document_id),
                embedding_model=embedding_model_name(),
                status=DocumentStatus.EMBEDDING.value)
            created_job_id = job.id
            await session.commit()
        job_id = created_job_id

        await _publish_status(document_id, DocumentStatus.EMBEDDING)
        embeddings = await embed_texts([c.text for c in chunks])

        points_deleted = True
        await delete_document_chunks(document_id)

        await index_chunks(
            document_id=document_id, ticker=ticker, category=doc_category,
            chunks=chunks, embeddings=embeddings,
            source=PurePosixPath(filename).stem if filename else None,
            visibility=visibility)

        async with SessionLocal() as session:
            await record_document_chunks(
                session, document_id=document_id, chunks=chunks,
                replace_existing=True)
            await EmbeddingJobRepository(session).set_status(
                created_job_id, DocumentStatus.READY.value)
            await session.commit()
        job_id = None
        points_deleted = False

        await _publish_status(document_id, DocumentStatus.READY)
        logger.info(
            "rag_ingestion_complete", document_id=document_id,
            chunk_count=len(chunks))

    except Exception as exc:
        logger.exception("rag_ingestion_failed", document_id=document_id)
        await _mark_failed_bookkeeping(
            document_id, job_id, points_deleted=points_deleted)
        await _publish_status(document_id, DocumentStatus.FAILED)
        if is_transient_ingestion_error(exc):
            raise TransientIngestionError(
                f"{type(exc).__name__}: {str(exc)[:200]}") from exc
