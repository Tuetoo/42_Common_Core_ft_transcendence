import asyncio
import json
import re
from pathlib import Path
from typing import Any
import structlog
from ai.llm.base import LLMError, LLMMessage, LLMTier
from ai.llm.mistral_provider import mistral_key_count
from ai.llm.router import LLMRouter
from ai.rag.embedding import cosine_similarity
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

_INTER_CHUNK_DELAY_SECONDS_PER_KEY = 1.5
_RETRIES_PER_CHUNK = 5


def _inter_chunk_delay_seconds() -> float:
    return _INTER_CHUNK_DELAY_SECONDS_PER_KEY / max(1, mistral_key_count())


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
        collection_name=settings.QDRANT_COLLECTION_RESEARCH, limit=sample_size,
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


async def _select_chunks_for_golden_set(
        chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return await _drop_near_duplicate_chunks(chunks)


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


async def generate_golden_set(
    *, sample_size: int | None = None, llm_router: LLMRouter | None = None,
    llm_tier: LLMTier = LLMTier.CHEAP,
    categories: frozenset[str] | None = None,
    skip_chunk_ids: frozenset[str] = frozenset()
) -> GoldenSetGenerationResult:
    router = llm_router or LLMRouter()
    if sample_size is None:
        sample_size = await _real_indexed_chunk_count()
    chunks = await _sample_indexed_chunks(sample_size)
    if categories is not None:
        chunks = [c for c in chunks if c["category"] in categories]
    if skip_chunk_ids:
        chunks = [c for c in chunks if c["chunk_id"] not in skip_chunk_ids]
    if not chunks:
        raise ValueError(
            "No indexed chunks to generate questions for (none in Qdrant, "
            "or none left after the category / already-covered filters) -- "
            "run some documents through "
            "ai/workflows/rag_ingestion_workflow.py "
            "first")
    raw_sample_count = len(chunks)
    chunks = await _select_chunks_for_golden_set(chunks)
    if len(chunks) < raw_sample_count:
        logger.info(
            "golden_set.chunks_filtered_before_generation",
            sampled_count=raw_sample_count, kept_count=len(chunks),
            filtered_count=raw_sample_count - len(chunks))

    pairs: list[GoldenQAPair] = []
    failed_chunk_ids: list[str] = []
    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(_inter_chunk_delay_seconds())
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
