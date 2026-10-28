import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import structlog
from ai.llm.base import LLMError, LLMMessage, LLMTier
from ai.llm.router import LLMRouter
from ai.rag.embedding import cosine_similarity, embed_texts
from ai.rag.retriever import BaseRetriever
from ai.rag.visibility import SYSTEM_ONLY_SCOPE
from config import settings
from pydantic import BaseModel
from qdrant import get_qdrant_client
from shared.enums import DocumentCategory

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "You will be given one passage from a financial research document. "
    "Write exactly one specific factual question whose answer is stated "
    "in the passage -- not a generic question that could apply to any "
    "similar document. Return only the question, no preamble, no quotes.")

_INTER_CHUNK_DELAY_SECONDS = 1.5
_RETRIES_PER_CHUNK = 5


class GoldenQAPair(BaseModel):
    question: str
    chunk_id: str
    document_id: str | None = None
    ticker: str | None = None
    category: DocumentCategory


class GoldenSetGenerationResult(BaseModel):
    pairs: list[GoldenQAPair]
    sampled_count: int
    failed_chunk_ids: list[str]
    duplicate_question_chunk_ids: list[str] = []


async def _sample_indexed_chunks(sample_size: int) -> list[dict[str, Any]]:
    client = get_qdrant_client()
    points, _ = await client.scroll(
        collection_name=settings.QDRANT_COLLECTION_RESEARCH,
        scroll_filter=SYSTEM_ONLY_SCOPE.qdrant_condition(), limit=sample_size,
        with_payload=True, with_vectors=True)
    return [{
        "chunk_id": str(p.id),
        "text": p.payload.get("text", ""),
        "document_id": p.payload.get("document_id"),
        "ticker": p.payload.get("ticker"),
        "category": p.payload.get("category"),
        "vector": p.vector
    } for p in points if p.payload and p.payload.get("text")]


_NEAR_DUPLICATE_CATEGORIES = frozenset(
    {DocumentCategory.MACRO_RESEARCH.value, DocumentCategory.NEWS.value})
_NEAR_DUPLICATE_SIMILARITY_THRESHOLD = 0.97

_FACT_SIGNATURE_PERIOD_WORDS = frozenset({
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "first", "second", "third", "fourth"
})


def _fact_signature(text: str, *,
                    ignore_proper_noun: str | None = None) -> frozenset[str]:
    numbers = set(re.findall(r"\d+\.?\d*%?|\b(?:19|20)\d{2}\b", text.lower()))
    period_words = {
        w
        for w in re.findall(r"\b[a-z]+\b", text.lower())
        if w in _FACT_SIGNATURE_PERIOD_WORDS
    }
    proper_nouns = set(re.findall(r"\b[A-Z][a-zA-Z]+\b", text))
    if ignore_proper_noun is not None:
        proper_nouns.discard(ignore_proper_noun)
    return frozenset(numbers | period_words | proper_nouns)


async def _drop_near_duplicate_chunks(
        chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    target_idxs = [
        i for i, c in enumerate(chunks)
        if c["category"] in _NEAR_DUPLICATE_CATEGORIES
    ]
    if len(target_idxs) < 2:
        return chunks

    texts = [chunks[i]["text"] for i in target_idxs]
    vectors = [chunks[i]["vector"] for i in target_idxs]
    signatures = [
        _fact_signature(
            t, ignore_proper_noun=(
                chunks[idx].get("ticker") if chunks[idx]["category"]
                == DocumentCategory.MACRO_RESEARCH.value else None))
        for t, idx in zip(texts, target_idxs, strict=True)
    ]

    dropped: set[int] = set()
    kept_in_target: list[int] = []
    for pos, idx in enumerate(target_idxs):
        is_dup = False
        for kept_pos in kept_in_target:
            if signatures[pos] != signatures[kept_pos]:
                continue
            similarity = cosine_similarity(vectors[pos], vectors[kept_pos])
            if similarity >= _NEAR_DUPLICATE_SIMILARITY_THRESHOLD:
                is_dup = True
                break
        if is_dup:
            dropped.add(idx)
        else:
            kept_in_target.append(pos)

    return [c for i, c in enumerate(chunks) if i not in dropped]


def _normalize_question(question: str) -> str:
    return re.sub(r"\s+", " ", question.strip().lower()).rstrip("?.! ")


def _drop_exact_duplicate_questions(
        pairs: list[GoldenQAPair]) -> tuple[list[GoldenQAPair], list[str]]:
    seen: set[str] = set()
    kept: list[GoldenQAPair] = []
    dropped_chunk_ids: list[str] = []
    for pair in pairs:
        key = _normalize_question(pair.question)
        if key in seen:
            dropped_chunk_ids.append(pair.chunk_id)
            continue
        seen.add(key)
        kept.append(pair)
    return kept, dropped_chunk_ids


async def _real_indexed_chunk_count() -> int:
    client = get_qdrant_client()
    result = await client.count(
        collection_name=settings.QDRANT_COLLECTION_RESEARCH, exact=True)
    return result.count


async def select_golden_set_chunks(
    *, sample_size: int | None = None,
    categories: frozenset[str] | None = None,
    skip_chunk_ids: frozenset[str] = frozenset()
) -> tuple[list[dict[str, Any]], int]:
    if sample_size is None:
        sample_size = await _real_indexed_chunk_count()
    chunks = await _sample_indexed_chunks(sample_size)
    if categories is not None:
        chunks = [c for c in chunks if c["category"] in categories]
    if skip_chunk_ids:
        chunks = [c for c in chunks if c["chunk_id"] not in skip_chunk_ids]
    kept = await _drop_near_duplicate_chunks(chunks)
    if len(kept) < len(chunks):
        logger.info(
            "golden_set.chunks_filtered_before_generation",
            sampled_count=len(chunks), kept_count=len(kept),
            filtered_count=len(chunks) - len(kept))
    return kept, len(chunks)


async def generate_golden_set(
        chunks: list[dict[str, Any]], *, llm_router: LLMRouter | None = None,
        llm_tier: LLMTier = LLMTier.CHEAP) -> GoldenSetGenerationResult:
    router = llm_router or LLMRouter()
    pairs: list[GoldenQAPair] = []
    failed_chunk_ids: list[str] = []
    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(_INTER_CHUNK_DELAY_SECONDS)
        try:
            response = await router.complete(
                llm_tier, [
                    LLMMessage(role="system", content=_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=chunk["text"])
                ], temperature=0.0, retries=_RETRIES_PER_CHUNK)
        except LLMError as exc:
            logger.warning(
                "golden_set.question_generation_failed",
                chunk_id=chunk["chunk_id"], error=str(exc))
            failed_chunk_ids.append(chunk["chunk_id"])
            continue
        pairs.append(
            GoldenQAPair(
                question=response.content.strip(), chunk_id=chunk["chunk_id"],
                document_id=chunk["document_id"], ticker=chunk["ticker"],
                category=DocumentCategory(chunk["category"])))
        if (i + 1) % 25 == 0:
            logger.info(
                "golden_set.progress", processed=i + 1, total=len(chunks),
                generated_so_far=len(pairs))

    if failed_chunk_ids:
        logger.warning(
            "golden_set.chunks_skipped", skipped_count=len(failed_chunk_ids),
            sampled_count=len(chunks), generated_count=len(pairs),
            skipped_chunk_ids=failed_chunk_ids)

    pairs, duplicate_question_chunk_ids = _drop_exact_duplicate_questions(
        pairs)
    if duplicate_question_chunk_ids:
        logger.warning(
            "golden_set.duplicate_questions_dropped",
            dropped_count=len(duplicate_question_chunk_ids),
            remaining_count=len(pairs),
            dropped_chunk_ids=duplicate_question_chunk_ids)

    return GoldenSetGenerationResult(
        pairs=pairs, sampled_count=len(chunks),
        failed_chunk_ids=failed_chunk_ids,
        duplicate_question_chunk_ids=duplicate_question_chunk_ids)


def save_golden_set(pairs: list[GoldenQAPair], path: Path) -> None:
    path.write_text(
        json.dumps([p.model_dump(mode="json") for p in pairs], indent=2))


def load_golden_set(path: Path) -> list[GoldenQAPair]:
    if not path.exists():
        raise FileNotFoundError(
            f"No golden set at {path} -- generate one first via "
            "scripts/generate_rag_golden_set.py")
    return [
        GoldenQAPair.model_validate(item)
        for item in json.loads(path.read_text())
    ]


def rank_of(chunk_ids: list[str], expected: str) -> int | None:
    for position, chunk_id in enumerate(chunk_ids, start=1):
        if chunk_id == expected:
            return position
    return None


@dataclass
class Score:
    total: int = 0
    ranks: list[int] = field(default_factory=list)

    def add(self, rank: int | None) -> None:
        self.total += 1
        if rank is not None:
            self.ranks.append(rank)

    def recall(self, k: int) -> float:
        return (
            sum(1 for r in self.ranks if r <= k) /
            self.total if self.total else 0.0)

    def mrr(self) -> float:
        return (
            sum(1.0 / r
                for r in self.ranks) / self.total if self.total else 0.0)


@dataclass
class RecallResult:
    source_label: str
    top_k: int
    score: Score

    @property
    def hits(self) -> int:
        return len(self.score.ranks)

    @property
    def total(self) -> int:
        return self.score.total

    def _when_scored(self, value: float) -> float | None:
        return value if self.score.total else None

    @property
    def recall(self) -> float | None:
        return self._when_scored(self.score.recall(self.top_k))

    @property
    def recall_at_1(self) -> float | None:
        return self._when_scored(self.score.recall(1))

    @property
    def recall_at_3(self) -> float | None:
        return self._when_scored(self.score.recall(3))

    @property
    def mrr(self) -> float | None:
        return self._when_scored(self.score.mrr())


async def embed_golden_questions(
        golden_set: list[GoldenQAPair]) -> list[list[float]]:
    return await embed_texts([pair.question for pair in golden_set],
                             purpose="query")


async def compute_recall_at_k(
        golden_set: list[GoldenQAPair], *, sources: frozenset[str] | None,
        source_label: str, top_k: int = 5,
        category_filter: DocumentCategory | None = None) -> RecallResult:
    retriever = BaseRetriever(
        top_k=top_k, use_query_expansion=False,
        category_filter=category_filter, visibility=None)

    query_vectors: list[list[float] | None]
    if sources is None or "vector" in sources:
        query_vectors = list(await embed_golden_questions(golden_set))
    else:
        query_vectors = [None] * len(golden_set)

    score = Score()
    for pair, query_vector in zip(golden_set, query_vectors, strict=True):
        results = await retriever.retrieve(
            pair.question, ticker=pair.ticker, sources=sources,
            query_vector=query_vector)
        score.add(rank_of([r.chunk_id for r in results], pair.chunk_id))
    return RecallResult(source_label=source_label, top_k=top_k, score=score)


async def compute_all_recall_curves(
        golden_set: list[GoldenQAPair], *,
        top_k: int = 5) -> dict[str, RecallResult]:
    return {
        "bm25": await compute_recall_at_k(
            golden_set, sources=frozenset({"bm25"}), source_label="bm25",
            top_k=top_k),
        "vector": await compute_recall_at_k(
            golden_set, sources=frozenset({"vector"}), source_label="vector",
            top_k=top_k),
        "fused": await compute_recall_at_k(
            golden_set, sources=frozenset({"bm25", "vector"}),
            source_label="fused (BM25+vector+RRF)", top_k=top_k)
    }
