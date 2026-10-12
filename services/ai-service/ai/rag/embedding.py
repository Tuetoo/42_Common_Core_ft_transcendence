import asyncio
import itertools
import math
from typing import Literal, cast
import structlog
from ai.llm.base import rate_limit_backoff_seconds
from ai.llm.mistral_provider import (REQUEST_TIMEOUT_SECONDS,
                                     mark_pooled_client_rate_limited,
                                     next_client_excluding_pooled)
from config import settings
from google.genai import errors as genai_errors
from google.genai import types
from mistralai.client import Mistral
from mistralai.client.errors.sdkerror import SDKError

logger = structlog.get_logger(__name__)

_EMBEDDING_MODEL = "mistral-embed"
_MISTRAL_VECTOR_SIZE = 1024
_google_key_cursor = itertools.count()
_BATCH_SIZE = 32
_RATE_LIMIT_MAX_RETRIES = 5


class EmbeddingError(Exception):
    pass


class EmbeddingRateLimitedError(EmbeddingError):
    pass


async def _create_embeddings_with_retry(batch: list[str]) -> list[list[float]]:
    tried_402_labels: set[str] = set()
    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        selection = next_client_excluding_pooled(tried_402_labels)
        if selection is None:
            raise EmbeddingError(
                f"Every real pooled Mistral key ({len(tried_402_labels)}) "
                f"returned 402 (check subscriptions at "
                f"https://admin.mistral.ai/subscription) while embedding "
                f"a batch of {len(batch)}.")
        key_label, client = selection
        try:
            response = await asyncio.wait_for(
                client.embeddings.create_async(
                    model=_EMBEDDING_MODEL, inputs=batch),
                timeout=REQUEST_TIMEOUT_SECONDS)
        except TimeoutError:
            if attempt >= _RATE_LIMIT_MAX_RETRIES:
                raise
            logger.warning(
                "embedding.request_timed_out_retrying", attempt=attempt + 1,
                batch_size=len(batch), key=key_label,
                timeout_seconds=REQUEST_TIMEOUT_SECONDS)
            await asyncio.sleep(2.0)
            continue
        except SDKError as exc:
            status = exc.raw_response.status_code
            if status == 429:
                mark_pooled_client_rate_limited(key_label)
                if attempt >= _RATE_LIMIT_MAX_RETRIES:
                    raise
                retry_after = exc.raw_response.headers.get("retry-after")
                delay = (
                    float(retry_after)
                    if retry_after else rate_limit_backoff_seconds(attempt))
                logger.warning(
                    "embedding.rate_limited_retrying", attempt=attempt + 1,
                    delay_seconds=delay, batch_size=len(batch), key=key_label)
                await asyncio.sleep(delay)
                continue
            if status == 402:
                tried_402_labels.add(key_label)
                logger.warning(
                    "embedding.subscription_error_failing_over",
                    attempt=attempt + 1, batch_size=len(batch), key=key_label)
                continue
            if status == 400 and len(batch) > 1:
                return await _embed_individually_with_truncation_fallback(
                    client, batch)
            if status == 503:
                if attempt >= _RATE_LIMIT_MAX_RETRIES:
                    raise
                delay = rate_limit_backoff_seconds(attempt)
                logger.warning(
                    "embedding.service_unavailable_retrying",
                    attempt=attempt + 1, delay_seconds=delay,
                    batch_size=len(batch), key=key_label)
                await asyncio.sleep(delay)
                continue
            raise
        else:
            if any(item.index is None for item in response.data):
                raise EmbeddingError(
                    "Mistral returned an embedding item with no index -- "
                    "can't safely reorder the batch without it.")
            ordered = sorted(response.data, key=lambda item: item.index or 0)
            vectors = [item.embedding for item in ordered]
            if any(v is None for v in vectors):
                raise EmbeddingError(
                    "Mistral returned a null embedding for at least one "
                    "item in the batch -- unexpected on a successful "
                    "response, treating as a hard failure rather than "
                    "silently propagating a None vector.")
            logger.info(
                "embedding.succeeded", key=key_label, batch_size=len(batch))
            return cast(list[list[float]], vectors)
    raise AssertionError("unreachable")  # pragma: no cover


async def _embed_individually_with_truncation_fallback(
        client: Mistral, batch: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in batch:
        candidate = text
        while True:
            try:
                response = await client.embeddings.create_async(
                    model=_EMBEDDING_MODEL, inputs=[candidate])
            except SDKError as exc:
                if exc.raw_response.status_code != 400 or len(candidate) < 100:
                    raise
                candidate = candidate[:len(candidate) // 2]
                continue
            if candidate is not text:
                logger.warning(
                    "embedding.truncated_oversized_input",
                    original_chars=len(text), truncated_chars=len(candidate))
            embedding = response.data[0].embedding
            if embedding is None:
                raise EmbeddingError(
                    "Mistral returned a null embedding for a successful "
                    "response -- unexpected, treating as a hard failure.")
            vectors.append(embedding)
            break
    return vectors


async def embed_texts(
        texts: list[str], *,
        purpose: "EmbeddingPurpose" = "document") -> list[list[float]]:
    if not texts:
        return []
    if embedding_provider() == "google":
        return await _google_embed_texts(texts, purpose)

    vectors: list[list[float]] = []
    for start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[start:start + _BATCH_SIZE]
        vectors.extend(await _create_embeddings_with_retry(batch))
    return vectors


async def embed_query(text: str) -> list[float]:
    vectors = await embed_texts([text], purpose="query")
    return vectors[0]


EmbeddingPurpose = Literal["document", "query"]

_GOOGLE_BATCH_MAX_INPUTS = 20
_GOOGLE_BATCH_TOKEN_BUDGET = 15_000
_GOOGLE_TOKENS_PER_WORD = 1.7
_GOOGLE_RETRY_WAITS_SECONDS = (5.0, 10.0, 20.0, 40.0)


def embedding_provider() -> str:
    return settings.EMBEDDING_PROVIDER.strip().lower()


def embedding_model_name() -> str:
    if embedding_provider() == "google":
        return settings.GOOGLE_EMBEDDING_MODEL
    return _EMBEDDING_MODEL


def expected_vector_size() -> int:
    if embedding_provider() == "google":
        return settings.GOOGLE_EMBEDDING_DIMENSIONS
    return _MISTRAL_VECTOR_SIZE


def check_embedding_configuration() -> None:
    provider = embedding_provider()
    if provider not in ("mistral", "google"):
        raise EmbeddingError(
            f"EMBEDDING_PROVIDER must be 'mistral' or 'google', got "
            f"{provider!r}")
    if expected_vector_size() != settings.QDRANT_VECTOR_SIZE:
        raise EmbeddingError(
            f"QDRANT_VECTOR_SIZE={settings.QDRANT_VECTOR_SIZE} does not match "
            f"EMBEDDING_PROVIDER={provider!r} ({expected_vector_size()} "
            f"dims); "
            "set both together, with a QDRANT_COLLECTION_RESEARCH built for "
            "that provider (scripts/reembed_collection.py)")


def _is_embedding_2(model: str) -> bool:
    return "embedding-2" in model


def _google_payload_text(
        text: str, purpose: EmbeddingPurpose, model: str) -> str:
    if not _is_embedding_2(model):
        return text
    if purpose == "query":
        return f"task: search result | query: {text}"
    return f"title: none | text: {text}"


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a)**0.5
    norm_b = sum(y * y for y in b)**0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def l2_normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise EmbeddingError("Gemini returned an all-zero embedding")
    return [x / norm for x in vector]


async def google_embed_batch(
        batch: list[str], purpose: EmbeddingPurpose, *, model: str,
        dimensions: int) -> list[list[float]]:
    from ai.llm.google_provider import google_client_for, google_pool_labels

    labels = google_pool_labels()
    config = types.EmbedContentConfig(
        output_dimensionality=dimensions, task_type=(
            None if _is_embedding_2(model) else (
                "RETRIEVAL_QUERY"
                if purpose == "query" else "RETRIEVAL_DOCUMENT")))
    contents = [
        types.Content(
            parts=[types.Part(text=_google_payload_text(t, purpose, model))])
        for t in batch
    ]
    start = next(_google_key_cursor)
    attempt = 0
    tried_this_round: set[str] = set()
    while True:
        label = next((
            labels[(start + i) % len(labels)] for i in range(len(labels))
            if labels[(start + i) % len(labels)] not in tried_this_round),
                     None)
        if label is None:
            if attempt >= len(_GOOGLE_RETRY_WAITS_SECONDS):
                raise EmbeddingRateLimitedError(
                    f"Gemini embedding ({model}) rate-limited on every pooled "
                    f"project for {attempt} rounds")
            wait = _GOOGLE_RETRY_WAITS_SECONDS[attempt]
            attempt += 1
            logger.warning(
                "embedding.google_pool_rate_limited_waiting", model=model,
                wait_seconds=wait, round=attempt)
            await asyncio.sleep(wait)
            tried_this_round.clear()
            continue
        try:
            response = await asyncio.wait_for(
                google_client_for(label).aio.models.embed_content(
                    model=model, contents=contents, config=config),
                timeout=settings.EMBEDDING_REQUEST_TIMEOUT_SECONDS)
        except TimeoutError:
            if attempt >= len(_GOOGLE_RETRY_WAITS_SECONDS):
                raise TimeoutError(
                    f"Gemini embedding ({model}) timed out after "
                    f"{settings.EMBEDDING_REQUEST_TIMEOUT_SECONDS:g} s, "
                    f"{attempt} retries") from None
            wait = _GOOGLE_RETRY_WAITS_SECONDS[attempt]
            attempt += 1
            tried_this_round.add(label)
            logger.warning(
                "embedding.google_timeout_retrying", model=model, key=label,
                wait_seconds=wait, round=attempt,
                timeout_seconds=settings.EMBEDDING_REQUEST_TIMEOUT_SECONDS)
            await asyncio.sleep(wait)
            continue
        except genai_errors.ClientError as exc:
            if exc.code == 429:
                tried_this_round.add(label)
                logger.warning(
                    "embedding.google_rate_limited_rotating", model=model,
                    key=label, batch_size=len(batch))
                continue
            raise
        except genai_errors.ServerError as exc:
            if attempt >= len(_GOOGLE_RETRY_WAITS_SECONDS):
                raise
            wait = _GOOGLE_RETRY_WAITS_SECONDS[attempt]
            attempt += 1
            tried_this_round.add(label)
            logger.warning(
                "embedding.google_server_error_retrying", model=model,
                key=label, code=exc.code, wait_seconds=wait, round=attempt)
            await asyncio.sleep(wait)
            continue
        embeddings = response.embeddings or []
        if len(embeddings) != len(batch):
            raise EmbeddingError(
                f"Gemini returned {len(embeddings)} embeddings for "
                f"{len(batch)} inputs")
        vectors: list[list[float]] = []
        for item in embeddings:
            values = list(item.values or [])
            if len(values) != dimensions:
                raise EmbeddingError(
                    f"Gemini returned a {len(values)}-dim vector, expected "
                    f"{dimensions}")
            vectors.append(l2_normalise([float(v) for v in values]))
        logger.info(
            "embedding.google_succeeded", model=model, key=label,
            batch_size=len(batch), purpose=purpose)
        return vectors


def estimated_tokens(text: str) -> int:
    return int(len(text.split()) * _GOOGLE_TOKENS_PER_WORD) + 8


def google_batches(texts: list[str]) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for text in texts:
        tokens = estimated_tokens(text)
        if current and (len(current) >= _GOOGLE_BATCH_MAX_INPUTS or
                        current_tokens + tokens > _GOOGLE_BATCH_TOKEN_BUDGET):
            batches.append(current)
            current, current_tokens = [], 0
        current.append(text)
        current_tokens += tokens
    if current:
        batches.append(current)
    return batches


async def _google_embed_texts(texts: list[str],
                              purpose: EmbeddingPurpose) -> list[list[float]]:
    vectors: list[list[float]] = []
    for batch in google_batches(texts):
        vectors.extend(
            await google_embed_batch(
                batch, purpose, model=settings.GOOGLE_EMBEDDING_MODEL,
                dimensions=settings.GOOGLE_EMBEDDING_DIMENSIONS))
    return vectors
