import json
from pathlib import Path
from typing import Any
import bm25s  # type: ignore[import-untyped]
import structlog
from ai.knowledge.kg_context import (drop_non_english_relations,
                                     fetch_kg_relations)
from ai.llm.base import AGENT_LLM_CALL_RETRIES, LLMError, LLMMessage, LLMTier
from ai.llm.router import LLMRouter
from ai.rag.embedding import embed_query, embed_texts
from ai.rag.indexing import get_bm25_index
from ai.rag.visibility import RetrievalVisibility, VisibilityScope
from config import settings
from database import SessionLocal
from pydantic import BaseModel
from qdrant import get_qdrant_client
from qdrant_client.http import models as qmodels
from shared.enums import DocumentCategory

logger = structlog.get_logger(__name__)

_PER_RANKING_OVERFETCH = 3

_DEFAULT_SOURCE_WEIGHT = 1.0

_CATEGORY_WEIGHTS_PATH = Path(__file__).parent / "category_weights.json"

_DEFAULT_K = 60

_SYSTEM_PROMPT = (
    "Generate alternative phrasings of the user's search query to improve "
    "retrieval recall (RAG-Fusion). Return exactly {n} lines, one alternative "
    "query per line, no numbering, no bullets, no extra commentary.")

_SYSTEM_PROMPT_WITH_KG = (
    "Generate alternative phrasings of the user's search query to improve "
    "retrieval recall (RAG-Fusion). The query concerns {ticker}; entities "
    "already known to relate to it (from prior real analysis) include: "
    "{related_entities}. If one of these is genuinely relevant to the "
    "query's own real topic, a variant naming it explicitly is a real, "
    "useful candidate -- but do not force one in in if it doesn't actually "
    "fit; a query about a specific product recall shouldn't get a variant "
    "about an unrelated competitor just because the two are known to be "
    "related in general. Return exactly {n} lines, one alternative query "
    "per line, no numbering, no bullets, no extra commentary.")


async def _related_entity_names(
        ticker: str, viewer_user_id: str | None = None) -> list[str]:
    try:
        async with SessionLocal() as session:
            relations = await fetch_kg_relations(
                ticker, ticker, session, viewer_user_id=viewer_user_id)
    except Exception as exc:
        logger.info(
            "query_expansion.kg_lookup_failed", ticker=ticker, error=str(exc))
        return []
    relations = drop_non_english_relations(ticker, relations)

    names: list[str] = []
    seen = {ticker.strip().upper()}
    for other_name, *_rest in relations:
        key = other_name.strip().upper()
        if key not in seen:
            seen.add(key)
            names.append(other_name)
    return names


async def expand_query(
        query: str, *, ticker: str | None = None,
        viewer_user_id: str | None = None, num_variants: int = 3,
        llm_router: LLMRouter | None = None,
        llm_tier: LLMTier = LLMTier.CHEAP) -> list[str]:
    router = llm_router or LLMRouter()

    if ticker:
        related = await _related_entity_names(ticker, viewer_user_id)
    else:
        related = []

    if related:
        system_prompt = _SYSTEM_PROMPT_WITH_KG.format(
            n=num_variants, ticker=ticker,
            related_entities=", ".join(related[:10]))
    else:
        system_prompt = _SYSTEM_PROMPT.format(n=num_variants)

    try:
        response = await router.complete(
            llm_tier, [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=query)
            ], retries=AGENT_LLM_CALL_RETRIES)
    except LLMError as exc:
        logger.info("query_expansion.unavailable", error=str(exc))
        return [query]

    variants = [
        line.strip() for line in response.content.splitlines() if line.strip()
    ]
    return [query, *variants[:num_variants]]


def reciprocal_rank_fusion(
        rankings: list[list[str]], *, k: int = _DEFAULT_K,
        weights: list[float] | None = None) -> list[tuple[str, float]]:
    if weights is not None and len(weights) != len(rankings):
        raise ValueError(
            f"weights (len={len(weights)}) must match rankings "
            f"(len={len(rankings)})")

    scores: dict[str, float] = {}
    for i, ranking in enumerate(rankings):
        weight = weights[i] if weights is not None else 1.0
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


def _load_category_weights() -> dict[DocumentCategory, dict[str, float]]:
    if not _CATEGORY_WEIGHTS_PATH.exists():
        return {}
    with open(_CATEGORY_WEIGHTS_PATH) as f:
        raw = json.load(f)
    weights: dict[DocumentCategory, dict[str, float]] = {}
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        try:
            category = DocumentCategory(key)
        except ValueError:
            continue
        weights[category] = {
            "bm25": value.get("bm25", _DEFAULT_SOURCE_WEIGHT),
            "vector": value.get("vector", _DEFAULT_SOURCE_WEIGHT)
        }
    return weights


_CATEGORY_SOURCE_WEIGHTS: dict[DocumentCategory,
                               dict[str, float]] = (_load_category_weights())


def _payload_content_date(payload: dict[str, Any]) -> str | None:
    content_date = payload.get("content_date")
    if content_date:
        return str(content_date)
    source = payload.get("source")
    if isinstance(source, str) and source.startswith("agent:"):
        created_at = payload.get("created_at")
        return str(created_at) if created_at else None
    return None


class RetrievedChunk(BaseModel):
    chunk_id: str
    text: str
    score: float
    category: DocumentCategory
    ticker: str | None = None
    document_id: str | None = None
    source: str | None = None
    content_date: str | None = None


class BaseRetriever:

    def __init__(
            self, *, category_filter: DocumentCategory | None = None,
            top_k: int | None = None, use_query_expansion: bool = True,
            visibility: RetrievalVisibility | None = None) -> None:
        self.category_filter = category_filter
        self.top_k = top_k or settings.RAG_TOP_K_RESULTS
        self.use_query_expansion = use_query_expansion
        self.visibility = visibility

    async def _visibility_scope(self) -> VisibilityScope | None:
        if self.visibility is None:
            return None
        return await self.visibility.resolve()

    async def retrieve(
            self, query: str, *, ticker: str | None = None,
            sources: frozenset[str] | None = None,
            query_vector: list[float] | None = None) -> list[RetrievedChunk]:
        may_degrade_to_bm25 = sources is None
        scope = await self._visibility_scope()
        if sources is None:
            sources = self._resolve_default_sources()
        queries = (
            await expand_query(
                query, ticker=ticker,
                viewer_user_id=scope.user_id if scope is not None else None)
            if self.use_query_expansion else [query])
        overfetch = self.top_k * _PER_RANKING_OVERFETCH

        rankings: list[list[str]] = []
        ranking_weights: list[float] = []
        payloads: dict[str, dict[str, Any] | None] = {}

        qdrant_filter = self._build_filter(ticker, scope)
        client = get_qdrant_client()
        bm25_index = get_bm25_index()

        category_weights = self._category_weights()

        query_vectors = (
            await self._query_vectors(
                queries, query_vector, may_degrade=may_degrade_to_bm25
                and "bm25" in sources) if "vector" in sources else None)

        for variant, q in enumerate(queries):
            if "bm25" in sources:
                bm25_results = bm25_index.search(
                    q, top_k=overfetch, category=(
                        self.category_filter.value if self.category_filter
                        is not None else None), ticker=ticker, scope=scope)
                rankings.append([
                    chunk_id for chunk_id, score in bm25_results if score > 0
                ])
                ranking_weights.append(
                    category_weights.get("bm25", _DEFAULT_SOURCE_WEIGHT))

            if query_vectors is not None:
                vector_response = await client.query_points(
                    collection_name=settings.QDRANT_COLLECTION_RESEARCH,
                    query=query_vectors[variant], limit=overfetch,
                    query_filter=qdrant_filter)
                rankings.append(
                    [str(point.id) for point in vector_response.points])
                ranking_weights.append(
                    category_weights.get("vector", _DEFAULT_SOURCE_WEIGHT))
                for point in vector_response.points:
                    payloads[str(point.id)] = point.payload

        fused = reciprocal_rank_fusion(rankings, weights=ranking_weights)

        missing_ids = [
            chunk_id for chunk_id, _ in fused if chunk_id not in payloads
        ]
        if missing_ids:
            records = await client.retrieve(
                collection_name=settings.QDRANT_COLLECTION_RESEARCH,
                ids=missing_ids, with_payload=True)
            for record in records:
                payloads[str(record.id)] = record.payload

        results: list[RetrievedChunk] = []
        for chunk_id, score in fused:
            payload = payloads.get(chunk_id)
            if payload is None:
                continue
            if (self.category_filter
                    and payload.get("category") != self.category_filter.value):
                continue
            if ticker and payload.get("ticker") != ticker:
                continue
            if scope is not None and not scope.allows_payload(payload):
                continue
            results.append(
                RetrievedChunk(
                    chunk_id=chunk_id, text=payload.get("text",
                                                        ""), score=score,
                    category=DocumentCategory(payload["category"]),
                    ticker=payload.get("ticker"),
                    document_id=payload.get("document_id"),
                    source=payload.get("source"),
                    content_date=_payload_content_date(payload)))
            if len(results) >= self.top_k:
                break

        return results

    async def _query_vectors(
            self, queries: list[str], query_vector: list[float] | None, *,
            may_degrade: bool) -> list[list[float]] | None:
        if query_vector is not None and not self.use_query_expansion:
            return [query_vector for _ in queries]
        try:
            if len(queries) == 1:
                return [await embed_query(queries[0])]
            return await embed_texts(queries, purpose="query")
        except Exception as exc:
            if not may_degrade:
                raise
            logger.warning(
                "retriever.vector_search_skipped_embedding_failed", category=(
                    self.category_filter.value
                    if self.category_filter is not None else None),
                error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return None

    def _category_weights(self) -> dict[str, float]:
        if self.category_filter is None:
            return {}
        return _CATEGORY_SOURCE_WEIGHTS.get(self.category_filter, {})

    def _resolve_default_sources(self) -> frozenset[str]:
        weights = self._category_weights()
        if weights:
            nonzero_sources = {
                source
                for source in ("bm25", "vector")
                if weights.get(source, _DEFAULT_SOURCE_WEIGHT) != 0.0
            }
            if nonzero_sources:
                return frozenset(nonzero_sources)
        return frozenset({"bm25", "vector"})

    def _build_filter(
            self, ticker: str | None,
            scope: VisibilityScope | None = None) -> qmodels.Filter | None:
        conditions: list[qmodels.Condition] = []
        if self.category_filter is not None:
            conditions.append(
                qmodels.FieldCondition(
                    key="category", match=qmodels.MatchValue(
                        value=self.category_filter.value)))
        if ticker is not None:
            conditions.append(
                qmodels.FieldCondition(
                    key="ticker", match=qmodels.MatchValue(value=ticker)))
        if scope is not None:
            conditions.append(scope.qdrant_condition())
        return qmodels.Filter(must=conditions) if conditions else None


class FilingRetriever(BaseRetriever):
    NEWEST_FILING_SLOTS = 2

    def __init__(
            self, *, top_k: int | None = None,
            use_query_expansion: bool = True,
            visibility: RetrievalVisibility | None = None) -> None:
        super().__init__(
            category_filter=DocumentCategory.FILING, top_k=top_k
            or (settings.RAG_TOP_K_RESULTS + 3),
            use_query_expansion=use_query_expansion, visibility=visibility)

    async def retrieve(
            self, query: str, *, ticker: str | None = None,
            sources: frozenset[str] | None = None,
            query_vector: list[float] | None = None) -> list[RetrievedChunk]:
        results = await super().retrieve(
            query, ticker=ticker, sources=sources, query_vector=query_vector)
        if ticker is None or sources is not None:
            return results
        try:
            newest = await self.newest_filing_chunks(query, ticker)
        except Exception:
            logger.warning(
                "filing_retriever.newest_filing_lookup_failed", ticker=ticker)
            return results
        return reserve_newest_filing_slots(
            results, newest, top_k=self.top_k, slots=self.NEWEST_FILING_SLOTS)

    async def newest_filing_chunks(self, query: str,
                                   ticker: str) -> list[RetrievedChunk]:
        client = get_qdrant_client()
        scope = await self._visibility_scope()
        points: list[Any] = []
        next_offset = None
        while True:
            batch, next_offset = await client.scroll(
                collection_name=settings.QDRANT_COLLECTION_RESEARCH,
                scroll_filter=self._build_filter(ticker, scope), limit=256,
                offset=next_offset, with_payload=True, with_vectors=False)
            points.extend(batch)
            if next_offset is None:
                break
        dated = [
            p for p in points if p.payload and p.payload.get("content_date")
            and p.payload.get("document_id") and (
                scope is None or scope.allows_payload(p.payload))
        ]
        if not dated:
            return []
        newest_date = max(str(p.payload["content_date"])[:10] for p in dated)
        newest_point = max((
            p for p in dated
            if str(p.payload["content_date"])[:10] == newest_date),
                           key=_newest_document_rank)
        newest_doc = str(newest_point.payload["document_id"])
        doc_points = sorted(
            (p for p in dated if str(p.payload["document_id"]) == newest_doc),
            key=lambda p: int(p.payload.get("chunk_index") or 0))
        ranked = _rank_lexically(
            query, [str(p.payload.get("text", "")) for p in doc_points])
        return [
            RetrievedChunk(
                chunk_id=str(doc_points[i].id),
                text=str(doc_points[i].payload.get("text", "")), score=0.0,
                category=DocumentCategory.FILING,
                ticker=doc_points[i].payload.get("ticker"),
                document_id=newest_doc,
                source=doc_points[i].payload.get("source"),
                content_date=str(doc_points[i].payload["content_date"]))
            for i in ranked[:self.NEWEST_FILING_SLOTS]
        ]


def _newest_document_rank(point: Any) -> tuple[bool, str, str]:
    payload = point.payload
    return (
        payload.get("is_system")
        is not False, str(payload.get("created_at")
                          or ""), str(payload["document_id"]))


def _rank_lexically(query: str, texts: list[str]) -> list[int]:
    if not texts:
        return []
    tokens = bm25s.tokenize(texts, show_progress=False)
    query_words = [
        word for word in bm25s.tokenize(
            query, show_progress=False, return_ids=False)[0]
        if word in tokens.vocab
    ]
    if not query_words:
        return list(range(len(texts)))
    model = bm25s.BM25()
    model.index(tokens, show_progress=False)
    scores = model.get_scores(query_words)
    return sorted(range(len(texts)), key=lambda i: (-float(scores[i]), i))


def reserve_newest_filing_slots(
        results: list[RetrievedChunk], newest: list[RetrievedChunk], *,
        top_k: int, slots: int) -> list[RetrievedChunk]:
    if not newest:
        return results
    newest_doc = newest[0].document_id
    already = sum(1 for r in results if r.document_id == newest_doc)
    present = {r.chunk_id for r in results}
    to_add = [c for c in newest
              if c.chunk_id not in present][:max(0, slots - already)]
    if not to_add:
        return results
    keep = max(0, top_k - len(to_add))
    kept: list[RetrievedChunk] = []
    dropped_other = 0
    budget_to_drop = max(0, len(results) - keep)
    for r in reversed(results):
        if dropped_other < budget_to_drop and r.document_id != newest_doc:
            dropped_other += 1
            continue
        kept.append(r)
    kept.reverse()
    return kept + to_add
