import asyncio
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import NAMESPACE_URL, UUID, uuid5
import bm25s  # type: ignore[import-untyped]
import numpy as np
import structlog
from ai.rag.chunking import Chunk
from ai.rag.visibility import ChunkVisibility, VisibilityScope
from config import settings
from qdrant import get_qdrant_client
from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import ResponseHandlingException
from sqlalchemy.ext.asyncio import AsyncSession
from shared.enums import DocumentCategory

logger = structlog.get_logger(__name__)

_UPSERT_RETRY_DELAY_SECONDS = 1.0


class BM25Item(NamedTuple):
    chunk_id: str
    text: str
    category: str | None = None
    ticker: str | None = None
    visibility: ChunkVisibility | None = None


def chunk_point_id(document_id: str, chunk_index: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"{document_id}:{chunk_index}"))


async def index_chunks(
        *, document_id: str, ticker: str | None, category: DocumentCategory,
        chunks: list[Chunk], embeddings: list[list[float]],
        visibility: ChunkVisibility, source: str | None = None,
        created_at: datetime | None = None,
        content_date: str | None = None) -> None:
    if len(chunks) != len(embeddings):
        raise ValueError(
            f"chunks ({len(chunks)}) and embeddings ({len(embeddings)}) "
            f"length mismatch")
    if not chunks:
        return

    indexed_at = (created_at or datetime.now(UTC)).isoformat()
    extra_payload = {"content_date": content_date} if content_date else {}
    visibility_payload = visibility.payload()

    client = get_qdrant_client()
    points = [
        qmodels.PointStruct(
            id=chunk_point_id(document_id, chunk.chunk_index),
            vector=embedding, payload={
                "document_id": document_id,
                "created_at": indexed_at,
                "chunk_id": chunk_point_id(document_id, chunk.chunk_index),
                "ticker": ticker,
                "category": category.value,
                "chunk_index": chunk.chunk_index,
                "text": chunk.text,
                "source": source,
                **extra_payload,
                **{
                    k: v
                    for k, v in chunk.metadata.items()
                    if v is not None or k not in extra_payload
                },
                **visibility_payload
            }) for chunk, embedding in zip(chunks, embeddings, strict=True)
    ]
    try:
        await client.upsert(
            collection_name=settings.QDRANT_COLLECTION_RESEARCH, points=points)
    except ResponseHandlingException as exc:
        logger.warning(
            "rag.qdrant_upsert_retrying", document_id=document_id,
            error=str(exc))
        await asyncio.sleep(_UPSERT_RETRY_DELAY_SECONDS)
        await client.upsert(
            collection_name=settings.QDRANT_COLLECTION_RESEARCH, points=points)

    bm25_index = get_bm25_index()
    await asyncio.to_thread(
        bm25_index.add_many, [
            BM25Item(
                chunk_point_id(document_id, c.chunk_index), c.text,
                category.value, ticker, visibility) for c in chunks
        ])

    logger.info(
        "rag.indexed_document", document_id=document_id, ticker=ticker,
        category=category.value, chunk_count=len(chunks))


async def delete_document_chunks(document_id: str) -> int:
    client = get_qdrant_client()
    doc_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="document_id", match=qmodels.MatchValue(value=document_id))
        ])

    chunk_ids: list[str] = []
    next_offset = None
    while True:
        points, next_offset = await client.scroll(
            collection_name=settings.QDRANT_COLLECTION_RESEARCH,
            scroll_filter=doc_filter, limit=256, offset=next_offset,
            with_payload=True, with_vectors=False)
        chunk_ids.extend(
            p.payload["chunk_id"] for p in points
            if p.payload and p.payload.get("chunk_id"))
        if next_offset is None:
            break

    if not chunk_ids:
        logger.info(
            "rag.deleted_document_chunks", document_id=document_id,
            chunk_count=0)
        return 0

    await client.delete(
        collection_name=settings.QDRANT_COLLECTION_RESEARCH,
        points_selector=qmodels.FilterSelector(filter=doc_filter))
    await asyncio.to_thread(get_bm25_index().remove_many, chunk_ids)

    logger.info(
        "rag.deleted_document_chunks", document_id=document_id,
        chunk_count=len(chunk_ids))
    return len(chunk_ids)


WORDS_TO_TOKENS = 1.3


async def record_document_chunks(
        session: AsyncSession, *, document_id: str, chunks: list[Chunk],
        replace_existing: bool = False) -> None:
    from domains.rag.repository import DocumentChunkRepository

    repo = DocumentChunkRepository(session)
    document_uuid = UUID(document_id)
    if replace_existing:
        await repo.delete_for_document(document_uuid)
    await repo.bulk_create(
        document_id=document_uuid, rows=[(
            c.chunk_index, round(len(c.text.split()) * WORDS_TO_TOKENS),
            chunk_point_id(document_id, c.chunk_index)) for c in chunks])


@dataclass
class DeletionResult:
    document_id: str
    qdrant_bm25_chunks_removed: int


async def delete_document_completely(
        document_id: str, session: AsyncSession) -> DeletionResult:
    chunk_count = await delete_document_chunks(document_id)

    document_uuid = UUID(document_id)
    from domains.rag.repository import (DocumentChunkRepository,
                                        EmbeddingJobRepository)

    chunk_rows = await DocumentChunkRepository(session).delete_for_document(
        document_uuid)
    job_rows = await EmbeddingJobRepository(session).delete_for_document(
        document_uuid)

    logger.info(
        "rag.deleted_document_completely", document_id=document_id,
        qdrant_bm25_chunks_removed=chunk_count,
        document_chunks_rows_removed=chunk_rows,
        embedding_jobs_rows_removed=job_rows)
    return DeletionResult(
        document_id=document_id, qdrant_bm25_chunks_removed=chunk_count)


async def rebuild_bm25_from_qdrant() -> int:
    client = get_qdrant_client()
    items: list[BM25Item] = []
    next_offset = None
    while True:
        points, next_offset = await client.scroll(
            collection_name=settings.QDRANT_COLLECTION_RESEARCH, limit=256,
            offset=next_offset, with_payload=True, with_vectors=False)
        for point in points:
            payload = point.payload or {}
            chunk_id = payload.get("chunk_id")
            text = payload.get("text")
            if chunk_id and text:
                items.append(
                    BM25Item(
                        chunk_id, text, payload.get("category"),
                        payload.get("ticker"),
                        ChunkVisibility.from_payload(payload)))
        if next_offset is None:
            break

    if items:
        await asyncio.to_thread(get_bm25_index().add_many, items)
    logger.info("rag.bm25_rebuilt_from_qdrant", chunk_count=len(items))
    return len(items)


@dataclass(frozen=True)
class _BM25Snapshot:
    retriever: bm25s.BM25
    corpus_ids: list[str]
    scopes: dict[str, tuple[str | None, str | None]]
    visibility: dict[str, ChunkVisibility]


class BM25Index:

    def __init__(self) -> None:
        self._write_lock = threading.Lock()
        self._snapshot: _BM25Snapshot | None = None
        self._corpus_texts: dict[str, str] = {}
        self._scopes: dict[str, tuple[str | None, str | None]] = {}
        self._visibility: dict[str, ChunkVisibility] = {}

    def add_many(self, items: Sequence[BM25Item]) -> None:
        with self._write_lock:
            for item in items:
                self._corpus_texts[item.chunk_id] = item.text
                self._scopes[item.chunk_id] = (item.category, item.ticker)
                if item.visibility is not None:
                    self._visibility[item.chunk_id] = item.visibility
                else:
                    self._visibility.pop(item.chunk_id, None)
            self._rebuild()

    def remove_many(self, chunk_ids: Sequence[str]) -> None:
        with self._write_lock:
            removed_any = False
            for chunk_id in chunk_ids:
                if chunk_id in self._corpus_texts:
                    del self._corpus_texts[chunk_id]
                    self._scopes.pop(chunk_id, None)
                    self._visibility.pop(chunk_id, None)
                    removed_any = True
            if removed_any:
                self._rebuild()

    def _rebuild(self) -> None:
        if not self._corpus_texts:
            self._snapshot = None
            return
        ids = list(self._corpus_texts.keys())
        texts = [self._corpus_texts[cid] for cid in ids]
        tokens = bm25s.tokenize(texts, show_progress=False)
        retriever = bm25s.BM25()
        retriever.index(tokens, show_progress=False)
        retriever.corpus_ids = ids
        self._snapshot = _BM25Snapshot(
            retriever=retriever, corpus_ids=ids, scopes=dict(self._scopes),
            visibility=dict(self._visibility))

    @staticmethod
    def _scope_mask(
            snapshot: _BM25Snapshot, category: str | None, ticker: str | None,
            visibility_scope: VisibilityScope | None = None) -> np.ndarray:
        ids = snapshot.corpus_ids
        mask = np.ones(len(ids), dtype=np.float32)
        for i, chunk_id in enumerate(ids):
            if visibility_scope is not None and not visibility_scope.allows(
                    snapshot.visibility.get(chunk_id)):
                mask[i] = 0.0
                continue
            scope = snapshot.scopes.get(chunk_id)
            if scope is None:
                continue
            if (category is not None
                    and scope[0] != category) or (ticker is not None
                                                  and scope[1] != ticker):
                mask[i] = 0.0
        return mask

    def search(
            self, query: str, top_k: int, *, category: str | None = None,
            ticker: str | None = None,
            scope: VisibilityScope | None = None) -> list[tuple[str, float]]:
        snapshot = self._snapshot
        if snapshot is None:
            return []
        corpus_ids = snapshot.corpus_ids
        k = min(top_k, len(corpus_ids))
        if k == 0:
            return []
        weight_mask = (
            self._scope_mask(snapshot, category, ticker, scope) if category
            is not None or ticker is not None or scope is not None else None)
        query_tokens = bm25s.tokenize([query], show_progress=False)
        indices, scores = snapshot.retriever.retrieve(
            query_tokens, k=k, show_progress=False, weight_mask=weight_mask)
        return [(corpus_ids[idx], float(score))
                for idx, score in zip(indices[0], scores[0], strict=True)
                if float(score) > 0.0]


_bm25_index: BM25Index | None = None


def get_bm25_index() -> BM25Index:
    global _bm25_index
    if _bm25_index is None:
        _bm25_index = BM25Index()
    return _bm25_index
