import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
import ai.evaluation.rag_recall as recall_module
import ai.ingestion.sec_edgar_filings as sec_edgar_filings
import ai.rag.embedding as embedding_module
import ai.rag.indexing as indexing_module
import ai.rag.retriever as retriever_module
import ai.workflows.auto_ingestion_workflow as auto_workflow
import ai.workflows.rag_ingestion_workflow as ingestion_module
import numpy as np
import pytest
from ai.evaluation.rag_recall import (GoldenQAPair,
                                      _drop_near_duplicate_chunks,
                                      _fact_signature, compute_recall_at_k)
from ai.ingestion.digests import MacroReport, NewsDigest
from ai.llm.base import (LLMMessage, LLMNonEnglishOutputError,
                         LLMProviderTimeoutError, LLMRateLimitError,
                         LLMRequestError, LLMResponse, LLMTier, LLMUsage)
from ai.rag.chunking import _CATEGORY_CONFIG, chunk_document
from ai.rag.embedding import embed_texts
from ai.rag.indexing import chunk_point_id, index_chunks
from ai.rag.retriever import (BaseRetriever, expand_query,
                              reciprocal_rank_fusion)
from ai.rag.visibility import (SYSTEM_ONLY_SCOPE, ChunkVisibility,
                               LazyVisibilityScope, VisibilityScope,
                               resolve_visibility_scope)
from database import SessionLocal
from fastapi import HTTPException
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels
from sqlalchemy import select
from domains.rag.repository import DocumentChunkRepository
from shared.enums import DocumentCategory
from shared.exceptions import NotFoundError, UpstreamServiceError
from shared.schemas import SYSTEM_DOCUMENT_OWNER_ID
from tests.auth_tokens import bearer_headers as _headers

_VEC = 8
_INDEX_VECTOR_SIZE = 4
_ME, _OTHER, _ORG = str(uuid4()), str(uuid4()), str(uuid4())
_CYRILLIC = "Привет мир " * 4
_SCOPED_TEXT = "Advanced Micro revenue guidance datacenter growth {tag}"
QUESTION = {
    "question": "What is the AMD revenue guidance for datacenter growth?"
}


def _vector(text: str) -> list[float]:
    vec = [0.0] * _VEC
    for word in text.lower().split():
        vec[sum(map(ord, word)) % _VEC] += 1.0
    norm = sum(v * v for v in vec)**0.5
    return [v / norm for v in vec] if norm else vec


class TestChunkDocument:

    def test_long_runs_filtered_paragraphs_fallback_and_targets(self):
        giant_blob = "x" * 20000
        text = (
            f"Normal opening paragraph text here. {giant_blob} "
            "Normal closing text here.")
        chunks = chunk_document(text, DocumentCategory.FILING)
        for chunk in chunks:
            longest_run = max(len(word) for word in chunk.text.split())
            assert longest_run <= 200

        text = (
            "DBS Bank Ltd accepts no responsibility for this. Has not been "
            "reviewed or authorized. Registration number 196800306E.\n\n"
            "DBS Vickers Securities accepts no responsibility either. This "
            "publication has not been reviewed or authorized by any "
            "regulator.\n\n"
            "DBS HK also accepts no responsibility. Registration number "
            "987654321X, has not been reviewed or authorized.")
        assert chunk_document(text, DocumentCategory.ANALYST_REPORT) == []

        text = (
            "us-gaap:EquitySecuritiesMember us-gaap:DisposalGroupMember "
            "intc:AlteraMember our actual revenue grew significantly this "
            "quarter")
        chunks = chunk_document(text, DocumentCategory.FILING)
        combined = " ".join(c.text for c in chunks)
        assert "revenue grew significantly" in combined

        for category, config_ in _CATEGORY_CONFIG.items():
            paragraphs = [
                " ".join(f"p{i}_{j}" for j in range(n)) for i, n in enumerate(
                    [30, config_.target_words * 2 + 7, 90, 10])
            ]
            chunks = chunk_document("\n\n".join(paragraphs), category)
            assert all(
                len(c.text.split()) <= config_.target_words
                for c in chunks), category


class _FakeEmbeddingItem:

    def __init__(self, index: int, embedding: list[float]):
        self.index = index
        self.embedding = embedding


class _FakeEmbeddingResponse:

    def __init__(self, data: list[_FakeEmbeddingItem]):
        self.data = data


class _FakeEmbeddingsAPI:

    def __init__(self):
        self.calls: list[list[str]] = []

    async def create_async(self, model, inputs):
        self.calls.append(list(inputs))
        items = [
            _FakeEmbeddingItem(i,
                               [float(len(text)), float(i)])
            for i, text in enumerate(inputs)
        ]
        return _FakeEmbeddingResponse(list(reversed(items)))


class _FakeEmbeddingClient:

    def __init__(self):
        self.embeddings = _FakeEmbeddingsAPI()


class TestEmbedTexts:

    @pytest.fixture(autouse=True)
    def _mistral_provider(self, monkeypatch):
        monkeypatch.setattr(
            embedding_module.settings, "EMBEDDING_PROVIDER", "mistral")

    @pytest.mark.asyncio
    async def test_empty_input_no_retry_on_401_and_400_truncation(
            self, monkeypatch):
        import httpx
        from mistralai.client.errors.sdkerror import SDKError

        def _should_not_be_called():
            raise AssertionError(
                "should not construct a client for empty input")

        monkeypatch.setattr(
            embedding_module, "next_client_excluding_pooled",
            lambda excluded: _should_not_be_called())
        assert await embed_texts([]) == []

        class _AlwaysFailingEmbeddingsAPI:

            def __init__(self):
                self.call_count = 0

            async def create_async(self, model, inputs):
                self.call_count += 1
                raw_response = httpx.Response(
                    401, headers={},
                    request=httpx.Request("POST", "https://x"))
                raise SDKError("unauthorized", raw_response, "{}")

        fake_client = _FakeEmbeddingClient()
        fake_client.embeddings = _AlwaysFailingEmbeddingsAPI()
        monkeypatch.setattr(
            embedding_module, "next_client_excluding_pooled", lambda excluded:
            ("test", fake_client))

        with pytest.raises(Exception):
            await embed_texts(["hello"])
        assert fake_client.embeddings.call_count == 1

        class _OneOversizedItemAPI:

            def __init__(self):
                self.single_item_calls: list[int] = []

            async def create_async(self, model, inputs):
                if len(inputs) > 1:
                    raw = httpx.Response(
                        400, headers={},
                        request=httpx.Request("POST", "https://x"))
                    raise SDKError("exceeding max tokens", raw, "{}")
                self.single_item_calls.append(len(inputs[0]))
                if len(inputs[0]) > 1000:
                    raw = httpx.Response(
                        400, headers={},
                        request=httpx.Request("POST", "https://x"))
                    raise SDKError("exceeding max tokens", raw, "{}")
                return _FakeEmbeddingResponse([_FakeEmbeddingItem(0, [1.0])])

        fake_client = _FakeEmbeddingClient()
        fake_client.embeddings = _OneOversizedItemAPI()
        monkeypatch.setattr(
            embedding_module, "next_client_excluding_pooled", lambda excluded:
            ("test", fake_client))

        vectors = await embed_texts(["short", "x" * 8000])

        assert len(vectors) == 2
        assert vectors == [[1.0], [1.0]]
        assert fake_client.embeddings.single_item_calls == [
            5, 8000, 4000, 2000, 1000
        ]


@pytest.fixture
async def memory_qdrant(monkeypatch):
    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection(
        collection_name="research_documents",
        vectors_config=qmodels.VectorParams(
            size=_INDEX_VECTOR_SIZE, distance=qmodels.Distance.COSINE))
    monkeypatch.setattr(indexing_module, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(
        indexing_module.settings, "QDRANT_COLLECTION_RESEARCH",
        "research_documents")
    monkeypatch.setattr(indexing_module, "_bm25_index", None)
    yield client


class TestIndexAndDeleteChunks:

    @pytest.mark.asyncio
    async def test_index_validation_noop_and_delete_from_both_stores(
            self, memory_qdrant):
        chunks = chunk_document("Some text.", DocumentCategory.NEWS)
        with pytest.raises(ValueError):
            await index_chunks(
                document_id="d1", ticker="AAPL",
                category=DocumentCategory.NEWS, chunks=chunks,
                embeddings=[[0.1] * _INDEX_VECTOR_SIZE,
                            [0.2] * _INDEX_VECTOR_SIZE],
                visibility=ChunkVisibility.system())

        await index_chunks(
            document_id="d1", ticker="AAPL", category=DocumentCategory.NEWS,
            chunks=[], embeddings=[], visibility=ChunkVisibility.system())
        count = await memory_qdrant.count(collection_name="research_documents")
        assert count.count == 0

        chunks = chunk_document(
            "Apple reported record iPhone revenue this quarter.",
            DocumentCategory.NEWS)
        embeddings = [[0.1, 0.2, 0.3, 0.4] for _ in chunks]
        await index_chunks(
            document_id="d1", ticker="AAPL", category=DocumentCategory.NEWS,
            chunks=chunks, embeddings=embeddings,
            visibility=ChunkVisibility.system())
        count_before = await memory_qdrant.count(
            collection_name="research_documents")
        assert count_before.count == len(chunks)
        point_id = chunk_point_id("d1", 0)
        bm25 = indexing_module.get_bm25_index()
        assert any(
            cid == point_id
            for cid, _ in bm25.search("iPhone revenue", top_k=5))

        removed = await indexing_module.delete_document_chunks("d1")

        assert removed == len(chunks)
        count_after = await memory_qdrant.count(
            collection_name="research_documents")
        assert count_after.count == 0
        bm25 = indexing_module.get_bm25_index()
        assert bm25.search("iPhone revenue", top_k=5) == []

        removed = await indexing_module.delete_document_chunks("never-indexed")
        assert removed == 0


def _bm25_reference_search(
        items: list[Any], query: str, top_k: int,
        mask: list[float] | None) -> list[tuple[str, float]]:
    import bm25s

    ids = [item.chunk_id for item in items]
    model = bm25s.BM25()
    model.index(
        bm25s.tokenize([item.text for item in items], show_progress=False),
        show_progress=False)
    indices, scores = model.retrieve(
        bm25s.tokenize([query], show_progress=False), k=min(top_k, len(ids)),
        show_progress=False,
        weight_mask=None if mask is None else np.array(mask, dtype="f4"))
    return [(ids[i], float(score))
            for i, score in zip(indices[0], scores[0], strict=True)
            if float(score) > 0.0]


class TestBM25Index:
    _WORDS = (
        "revenue margin guidance iphone cloud tariff supply demand chip "
        "data center outlook risk debt cash buyback dividend inflation"
    ).split()

    def _items(self, count: int) -> list[Any]:
        import random

        rng = random.Random(7)
        return [
            indexing_module.BM25Item(
                f"c{i}", " ".join(rng.choice(self._WORDS) for _ in range(30)),
                "news" if i % 2 else "filing", "AAPL" if i % 3 else "MSFT",
                ChunkVisibility.system()) for i in range(count)
        ]

    async def test_search_matches_bm25s_and_rebuild_runs_off_the_loop(
            self, memory_qdrant, monkeypatch):
        items = self._items(60)
        index = indexing_module.BM25Index()
        index.add_many(items[:40])
        index.add_many(items[40:])
        index.remove_many(["c3", "c10"])
        kept = [i for i in items if i.chunk_id not in {"c3", "c10"}]
        for query in ("iphone revenue guidance", "debt cash", "tariff"):
            assert index.search(query, top_k=10) == _bm25_reference_search(
                kept, query, 10, None)
            mask = [
                1.0 if i.category == "news" and i.ticker == "AAPL" else 0.0
                for i in kept
            ]
            assert index.search(
                query, top_k=10, category="news",
                ticker="AAPL") == _bm25_reference_search(
                    kept, query, 10, mask)

        import threading

        threads: list[threading.Thread] = []
        original = indexing_module.BM25Index._rebuild

        def _recording_rebuild(self):
            threads.append(threading.current_thread())
            original(self)

        monkeypatch.setattr(
            indexing_module.BM25Index, "_rebuild", _recording_rebuild)
        chunks = chunk_document(
            "Apple reported record iPhone revenue.", DocumentCategory.NEWS)
        await index_chunks(
            document_id="d1", ticker="AAPL", category=DocumentCategory.NEWS,
            chunks=chunks, embeddings=[[0.1, 0.2, 0.3, 0.4] for _ in chunks],
            visibility=ChunkVisibility.system())
        await indexing_module.delete_document_chunks("d1")
        assert len(threads) == 2
        assert all(t is not threading.main_thread() for t in threads)

        index = indexing_module.BM25Index()
        index.add_many(self._items(10))
        before = index.search("iphone revenue", top_k=5)
        release = threading.Event()

        def _slow_rebuild(self):
            release.wait(5)
            original(self)

        monkeypatch.setattr(
            indexing_module.BM25Index, "_rebuild", _slow_rebuild)
        extra = indexing_module.BM25Item(
            "new", "iphone iphone iphone revenue", "news", "AAPL")
        pending = asyncio.create_task(
            asyncio.to_thread(index.add_many, [extra]))
        await asyncio.sleep(0.05)
        assert not pending.done()
        assert index.search("iphone revenue", top_k=5) == before
        release.set()
        await pending
        assert index.search("iphone revenue", top_k=5)[0][0] == "new"


@pytest.mark.asyncio
async def test_variants_share_one_embedding_and_filing_tie_prefers_system(
        monkeypatch):
    embed_calls: list[tuple[list[str], str]] = []
    searched: list[list[float]] = []

    async def _embed_texts(texts, *, purpose="document"):
        embed_calls.append((list(texts), purpose))
        return [[float(i)] for i in range(len(texts))]

    async def _expand(query, **kwargs):
        return [query, "variant one", "variant two"]

    class _Qdrant:

        async def query_points(self, **kwargs):
            searched.append(kwargs["query"])
            return SimpleNamespace(points=[])

    class _BM25:

        def search(self, query, top_k, **kwargs):
            return []

    monkeypatch.setattr(retriever_module, "embed_texts", _embed_texts)
    monkeypatch.setattr(retriever_module, "expand_query", _expand)
    monkeypatch.setattr(retriever_module, "get_qdrant_client", _Qdrant)
    monkeypatch.setattr(retriever_module, "get_bm25_index", _BM25)
    await BaseRetriever().retrieve("revenue")
    assert embed_calls == [(["revenue", "variant one",
                             "variant two"], "query")]
    assert searched == [[0.0], [1.0], [2.0]]

    def _point(doc: str, created: str, is_system: bool) -> Any:
        return SimpleNamespace(
            id=f"{doc}-0", payload={
                "document_id": doc,
                "content_date": "2026-08-01",
                "created_at": created,
                "is_system": is_system,
                "chunk_index": 0,
                "text": "revenue guidance",
                "ticker": "AMD",
                "category": "filing"
            })

    points = [
        _point("ffff-user", "2026-08-03T00:00:00", False),
        _point("0000-system-old", "2026-08-01T00:00:00", True),
        _point("1111-system-new", "2026-08-02T00:00:00", True)
    ]

    class _ScrollQdrant:

        async def scroll(self, **kwargs):
            return points, None

    monkeypatch.setattr(retriever_module, "get_qdrant_client", _ScrollQdrant)
    newest = await retriever_module.FilingRetriever(use_query_expansion=False
                                                    ).newest_filing_chunks(
                                                        "revenue", "AMD")
    assert [c.document_id for c in newest] == ["1111-system-new"]


class _ScriptedExpansionRouter:

    def __init__(self, content: str):
        self.content = content

    async def complete(self, tier, messages, **kwargs):
        return LLMResponse(
            content=self.content, provider="fake", model="m",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


class TestRetrievalHelpers:

    @pytest.mark.asyncio
    async def test_expansion_fusion_weights_and_fact_signature(self):
        router = _ScriptedExpansionRouter("variant one\nvariant two")
        result = await expand_query("original query", llm_router=router)
        assert result[0] == "original query"
        assert "variant one" in result
        assert "variant two" in result

        with pytest.raises(ValueError):
            reciprocal_rank_fusion([["A", "B"], ["B", "A"]], weights=[1.0])

        sig = _fact_signature(
            "The Fed held rates steady at 5.25% for the third consecutive "
            "quarter in 2026, per the latest release.")
        assert "5.25%" in sig
        assert "2026" in sig
        assert "third" in sig
        assert "Fed" in sig


def _macro_chunk(chunk_id: str, ticker: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "text": (
            f"For {ticker}, the Fed held its policy rate steady at 5.25% "
            "this quarter, consistent with a restrictive stance."),
        "ticker": ticker,
        "category": DocumentCategory.MACRO_RESEARCH.value,
        "vector": [1.0, 0.0, 0.0]
    }


def _news_chunk(chunk_id: str, ticker: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "text": (
            f"{ticker} shares moved after the company held its rate "
            "outlook steady this quarter."),
        "ticker": ticker,
        "category": DocumentCategory.NEWS.value,
        "vector": [1.0, 0.0, 0.0]
    }


def _filing_chunk(chunk_id: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "text": "Same risk factor text.",
        "ticker": "AAPL",
        "category": DocumentCategory.FILING.value,
        "vector": [1.0, 0.0, 0.0]
    }


class TestDropNearDuplicateChunks:

    @pytest.mark.asyncio
    async def test_only_target_categories_are_deduplicated(self):
        cases = {
            "macro-same-facts-different-ticker-merged": ([
                _macro_chunk("m1", "AAPL"),
                _macro_chunk("m2", "MSFT")
            ], ["m1"]),
            "news-same-wording-different-ticker-kept": ([
                _news_chunk("n1", "AAPL"),
                _news_chunk("n2", "MSFT")
            ], ["n1", "n2"]),
            "non-target-category-untouched": ([
                _filing_chunk("f1"), _filing_chunk("f2")
            ], ["f1", "f2"])
        }
        for case_id, (chunks, kept_ids) in cases.items():
            result = await _drop_near_duplicate_chunks(chunks)
            assert [c["chunk_id"] for c in result] == kept_ids, case_id


class _FakeRecallChunk:

    def __init__(self, chunk_id: str):
        self.chunk_id = chunk_id


class _FakeRecallRetriever:

    def __init__(self, *args, **kwargs):
        self.retrieve_calls: list[dict] = []

    async def retrieve(self, question, *, ticker, sources, query_vector=None):
        self.retrieve_calls.append({
            "question": question,
            "sources": sources,
            "query_vector": query_vector
        })
        return [_FakeRecallChunk("expected-chunk")]


def _golden_set(n: int) -> list[GoldenQAPair]:
    return [
        GoldenQAPair(
            question=f"q{i}", chunk_id="expected-chunk", document_id=None,
            ticker=None, category=DocumentCategory.NEWS) for i in range(n)
    ]


class TestComputeRecallAtK:

    @pytest.mark.asyncio
    async def test_one_embedding_call_and_hand_computed_metrics(
            self, monkeypatch):
        embed_calls: list[list[str]] = []
        purposes: list[str] = []

        async def _fake_embed_texts(texts, *, purpose="document"):
            embed_calls.append(list(texts))
            purposes.append(purpose)
            return [[float(i)] for i in range(len(texts))]

        fake_retriever_holder: list[_FakeRecallRetriever] = []

        def _make_retriever(*args, **kwargs):
            r = _FakeRecallRetriever()
            fake_retriever_holder.append(r)
            return r

        monkeypatch.setattr(recall_module, "BaseRetriever", _make_retriever)
        monkeypatch.setattr(recall_module, "embed_texts", _fake_embed_texts)

        golden_set = _golden_set(5)
        await compute_recall_at_k(
            golden_set, sources=frozenset({"vector"}), source_label="vector")

        assert len(embed_calls) == 1 and purposes == ["query"]
        assert embed_calls[0] == [pair.question for pair in golden_set]

        retriever = fake_retriever_holder[0]
        assert [c["query_vector"] for c in retriever.retrieve_calls] == [[0.0],
                                                                         [1.0],
                                                                         [2.0],
                                                                         [3.0],
                                                                         [4.0]]

        scenario = {
            "q_rank_1": ["expected-chunk", "x", "x", "x", "x"],
            "q_rank_2": ["x", "expected-chunk", "x", "x", "x"],
            "q_rank_3": ["x", "x", "expected-chunk", "x", "x"],
            "q_rank_5": ["x", "x", "x", "x", "expected-chunk"],
            "q_miss": ["x", "x", "x", "x", "x"]
        }

        class _ScenarioRetriever:

            def __init__(self, *args, **kwargs):
                pass

            async def retrieve(
                    self, question, *, ticker, sources, query_vector=None):
                return [_FakeRecallChunk(cid) for cid in scenario[question]]

        golden_set = [
            GoldenQAPair(
                question=q, chunk_id="expected-chunk", document_id=None,
                ticker=None, category=DocumentCategory.NEWS) for q in scenario
        ]

        monkeypatch.setattr(recall_module, "BaseRetriever", _ScenarioRetriever)

        result = await compute_recall_at_k(
            golden_set, sources=frozenset({"bm25"}), source_label="bm25",
            top_k=5)

        assert result.hits == 4
        assert result.total == 5
        assert result.recall == pytest.approx(4 / 5)
        assert result.recall_at_1 == pytest.approx(1 / 5)
        assert result.recall_at_3 == pytest.approx(3 / 5)
        assert result.mrr == pytest.approx((1 + 0.5 + (1 / 3) + 0.2 + 0) / 5)


@pytest.fixture
def access_env(monkeypatch):
    import clients.auth_client as auth_client
    import clients.core_client as core_client

    portfolios: dict[str, dict] = {}
    memberships: dict[str, list[UUID] | str] = {}

    async def _owner(portfolio_id):
        return portfolios.get(portfolio_id)

    async def _orgs(user_id):
        value = memberships.get(user_id, [])
        if value == "down":
            raise UpstreamServiceError("auth down")
        return value

    monkeypatch.setattr(core_client, "get_portfolio_owner", _owner)
    monkeypatch.setattr(auth_client, "list_user_organization_ids", _orgs)

    def _portfolio(owner, org=None, visibility="organization"):
        pid = uuid4()
        portfolios[str(pid)] = {
            "portfolio_id": str(pid),
            "owner_id": str(owner),
            "organization_id": str(org) if org else None,
            "visibility": visibility
        }
        return pid

    return SimpleNamespace(portfolio=_portfolio, memberships=memberships)


def _user(user_id):
    from shared.security import CurrentUser

    return CurrentUser(user_id=user_id, role="user")


class TestPortfolioAccess:

    @pytest.mark.asyncio
    async def test_auth_down_is_503(self, access_env):
        from deps import ensure_portfolio_access

        me = uuid4()
        access_env.memberships[str(me)] = "down"
        shared = access_env.portfolio(uuid4(), uuid4(), "organization")
        with pytest.raises(HTTPException) as exc:
            await ensure_portfolio_access(_user(me), shared)
        assert exc.value.status_code == 503


class TestVisibilityModel:

    @pytest.mark.asyncio
    async def test_scope_rules_and_resolve_fails_closed(self, monkeypatch):
        me, other, org = (str(uuid4()) for _ in range(3))
        scope = VisibilityScope.for_user(me, [org])
        assert scope.allows(None)
        assert scope.allows(ChunkVisibility.system())
        assert scope.allows(ChunkVisibility.for_owner(me))
        assert scope.allows(ChunkVisibility.for_owner(other, org))
        assert not scope.allows(ChunkVisibility.for_owner(other))
        assert not scope.allows(ChunkVisibility())
        assert not SYSTEM_ONLY_SCOPE.allows(ChunkVisibility.for_owner(me))
        assert VisibilityScope.for_user("") is SYSTEM_ONLY_SCOPE

        import clients.auth_client as auth_client

        async def _down(user_id):
            raise UpstreamServiceError("down")

        monkeypatch.setattr(auth_client, "list_user_organization_ids", _down)
        me = str(uuid4())
        scope = await resolve_visibility_scope(me)
        assert scope == VisibilityScope(user_id=me)
        assert await resolve_visibility_scope(None) is SYSTEM_ONLY_SCOPE

        org = uuid4()
        calls: list[str] = []

        async def _orgs(user_id):
            calls.append(user_id)
            return [org]

        monkeypatch.setattr(auth_client, "list_user_organization_ids", _orgs)
        lazy = LazyVisibilityScope(me)
        assert (await lazy.resolve()).organization_ids == {str(org)}
        await resolve_visibility_scope(me)
        assert calls == [me, me]


@pytest.fixture
async def scoped_qdrant(monkeypatch):
    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection(
        collection_name="research_documents",
        vectors_config=qmodels.VectorParams(
            size=_VEC, distance=qmodels.Distance.COSINE))

    async def _embed(text):
        return _vector(text)

    monkeypatch.setattr(indexing_module, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(retriever_module, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(
        indexing_module.settings, "QDRANT_COLLECTION_RESEARCH",
        "research_documents")
    monkeypatch.setattr(indexing_module, "_bm25_index", None)
    monkeypatch.setattr(retriever_module, "embed_query", _embed)
    yield client


async def _index_docs() -> None:
    docs = {
        "own": ChunkVisibility.for_owner(_ME),
        "org": ChunkVisibility.for_owner(_OTHER, _ORG),
        "system": ChunkVisibility.system(),
        "legacy": ChunkVisibility.system(),
        "private": ChunkVisibility.for_owner(_OTHER),
        "closed": ChunkVisibility()
    }
    for tag, vis in docs.items():
        doc_id = str(uuid5(NAMESPACE_URL, tag))
        text = _SCOPED_TEXT.format(tag=tag)
        chunks = chunk_document(text, DocumentCategory.FILING)
        await index_chunks(
            document_id=doc_id, ticker="AMD", category=DocumentCategory.FILING,
            chunks=chunks, embeddings=[_vector(c.text) for c in chunks],
            source=tag, visibility=vis)
        if tag == "legacy":
            await indexing_module.get_qdrant_client().delete_payload(
                collection_name="research_documents",
                keys=["owner_id", "organization_id", "is_system"],
                points=[chunk_point_id(doc_id, c.chunk_index) for c in chunks])
    await indexing_module.rebuild_bm25_from_qdrant()


class TestScopedRetrieval:

    @pytest.mark.asyncio
    async def test_scope_in_every_path(self, scoped_qdrant):
        await _index_docs()
        scope = VisibilityScope.for_user(_ME, [_ORG])
        for sources in ({"vector"}, {"bm25"}, {"bm25", "vector"}):
            retriever = BaseRetriever(
                category_filter=DocumentCategory.FILING,
                use_query_expansion=False, top_k=10, visibility=scope)
            results = await retriever.retrieve(
                "revenue guidance datacenter", ticker="AMD",
                sources=frozenset(sources))
            assert {r.source
                    for r in results
                    } == {"own", "org", "system", "legacy"}, sources

            system_only = BaseRetriever(
                category_filter=DocumentCategory.FILING,
                use_query_expansion=False, top_k=10,
                visibility=SYSTEM_ONLY_SCOPE)
            results = await system_only.retrieve(
                "revenue guidance datacenter", ticker="AMD",
                sources=frozenset(sources))
            assert {r.source
                    for r in results} == {"system", "legacy"}, (sources)


class TestAgentsPassScope:

    @pytest.mark.asyncio
    async def test_filing_agent_passes_analysis_user_scope(self, monkeypatch):
        import ai.tools.filing_tool as filing_tool_module
        from ai.agents.filing import FilingAgent
        from ai.graph.state import AnalysisState
        from shared.enums import AgentRole, AnalysisType

        seen: list = []

        class _Stop(Exception):
            pass

        def _retriever(**kwargs):
            seen.append(kwargs.get("visibility"))
            raise _Stop

        monkeypatch.setattr(filing_tool_module, "FilingRetriever", _retriever)
        state = AnalysisState(
            analysis_id="a1", user_id="u-42", ticker="AMD",
            analysis_type=AnalysisType.FULL)
        with pytest.raises(_Stop):
            await FilingAgent(role=AgentRole.FILING).run(state)
        assert isinstance(seen[0], LazyVisibilityScope)
        assert seen[0].user_id == "u-42"


class TestIngestionStoresOwner:

    @pytest.mark.asyncio
    async def test_event_handler_and_workflow_keep_the_owner(
            self, monkeypatch, _schema_ready):
        import event_listener

        captured: dict = {}

        async def _run(**kwargs):
            captured.update(kwargs)

        real_workflow = ingestion_module.run_rag_ingestion_workflow
        monkeypatch.setattr(
            ingestion_module, "run_rag_ingestion_workflow", _run)
        owner, org = str(uuid4()), str(uuid4())
        await event_listener._handle_document_uploaded({
            "document_id": str(uuid4()),
            "owner_id": owner,
            "organization_id": org,
            "storage_bucket": "b",
            "storage_key": "k",
            "document_type": "txt",
            "category": "filing",
            "ticker": "AMD"
        })
        assert captured["owner_id"] == owner
        assert captured["organization_id"] == org

        wf = ingestion_module
        monkeypatch.setattr(wf, "run_rag_ingestion_workflow", real_workflow)
        captured.clear()

        async def _noop(*args, **kwargs):
            return 0

        async def _load(*args, **kwargs):
            return b"Revenue grew strongly this quarter on datacenter demand."

        async def _embed(texts):
            return [[0.1] * _VEC for _ in texts]

        async def _index(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(wf, "_publish_status", _noop)
        monkeypatch.setattr(wf, "download_bytes", _load)
        monkeypatch.setattr(wf, "embed_texts", _embed)
        monkeypatch.setattr(wf, "delete_document_chunks", _noop)
        monkeypatch.setattr(wf, "index_chunks", _index)

        owner, org = str(uuid4()), str(uuid4())
        await wf.run_rag_ingestion_workflow(
            document_id=str(uuid4()), storage_bucket="b", storage_key="k",
            document_type="txt", category="filing", ticker="AMD",
            owner_id=owner, organization_id=org)
        assert captured["visibility"] == ChunkVisibility(owner, org, False)

        await wf.run_rag_ingestion_workflow(
            document_id=str(uuid4()), storage_bucket="b", storage_key="k",
            document_type="txt", category="filing", ticker="AMD",
            owner_id=SYSTEM_DOCUMENT_OWNER_ID, organization_id=None)
        assert captured["visibility"] == ChunkVisibility.system()


class TestInternalAnalysisRoutes:

    @pytest.mark.asyncio
    async def test_ref_defaults_and_latest_recommendation_by_portfolio(
            self, client, db_session):
        from domains.analysis.repository import (AnalysisPersistenceRepository,
                                                 AnalysisRepository)
        from shared.schemas import AnalysisRef

        ref = AnalysisRef(
            id=uuid4(), ticker="AMD", status="pending",
            created_at=datetime.now(UTC))
        assert ref.user_id is None and ref.portfolio_id is None

        repo = AnalysisRepository(db_session)
        persist = AnalysisPersistenceRepository(db_session)
        mine, theirs = uuid4(), uuid4()
        for portfolio, action in ((mine, "buy"), (theirs, "sell")):
            analysis = await repo.create(
                user_id=uuid4(), portfolio_id=portfolio, ticker="TSM")
            await persist.save_recommendation(
                analysis_id=analysis.id, recommendation=action, confidence=0.6,
                reasoning="r", confidence_adjustment_note=None,
                debate_converged=True)
        await db_session.flush()

        base = "/internal/recommendations/latest?ticker=TSM"
        got = await client.get(f"{base}&portfolio_id={mine}")
        assert got.status_code == 200 and got.json()["action"] == "buy"
        got = await client.get(f"{base}&portfolio_id={theirs}")
        assert got.json()["action"] == "sell"
        unknown = await client.get(f"{base}&portfolio_id={uuid4()}")
        assert unknown.status_code == 404
        assert (await client.get(base)).status_code == 422


class TestMarketAndAnalyticsAuth:

    @pytest.mark.asyncio
    async def test_token_required_and_unknown_ticker_is_not_stored(
            self, client, db_session, monkeypatch):
        import domains.market.api as market_api
        from domains.market.repository import InstrumentRepository
        from domains.market.service import MarketService, NoPriceDataError

        denied = await client.get("/api/v1/analytics/agents")
        assert denied.status_code == 401
        ok = await client.get(
            "/api/v1/analytics/agents", headers=_headers(uuid4()))
        assert ok.status_code == 200

        class _Provider:

            async def get_recent_price_history(self, ticker, **kwargs):
                return []

        with pytest.raises(NoPriceDataError):
            await MarketService(db_session).refresh_from_external_provider(
                "ZZQQ", provider=_Provider())  # type: ignore[arg-type]
        with pytest.raises(NotFoundError):
            await InstrumentRepository(db_session).get_by_ticker("ZZQQ")

        async def _bars(ticker, **kwargs):
            raise NoPriceDataError("none")

        monkeypatch.setattr(market_api, "get_bars_cache_first", _bars)
        response = await client.get("/internal/market/instruments/ZZQQ/prices")
        assert response.status_code == 404

        from ai.providers.base import ProviderRequestError

        async def _down(ticker, **kwargs):
            raise ProviderRequestError("provider down")

        monkeypatch.setattr(market_api, "get_bars_cache_first", _down)
        response = await client.get(
            "/api/v1/market/instruments/MSFT/prices",
            headers=_headers(uuid4()))
        assert response.status_code == 502


class TestBacktestScope:

    @pytest.mark.asyncio
    async def test_route_and_repository_filter_to_the_user(
            self, client, monkeypatch, db_session):
        from domains.analysis.model import (Analysis, Recommendation,
                                            RecommendationTier)
        from domains.analysis.repository import RecommendationTierRepository
        from shared.enums import TierStatus

        seen: list[dict] = []
        real_tiers = (
            RecommendationTierRepository.list_triggered_tiers_for_ticker)

        async def _tiers(self, ticker, **kwargs):
            seen.append(kwargs)
            return []

        monkeypatch.setattr(
            RecommendationTierRepository, "list_triggered_tiers_for_ticker",
            _tiers)
        me = uuid4()
        response = await client.post(
            "/api/v1/backtests", headers=_headers(me), json={
                "strategy_name": "s",
                "ticker": "AMD",
                "start_date": "2026-01-01",
                "end_date": "2026-02-01",
                "initial_capital": 1000
            })
        assert response.status_code == 422
        assert seen[0]["user_id"] == me and seen[0]["portfolio_id"] is None
        monkeypatch.setattr(
            RecommendationTierRepository, "list_triggered_tiers_for_ticker",
            real_tiers)

        me, other = uuid4(), uuid4()
        when = datetime(2026, 1, 15, tzinfo=UTC)
        for user in (me, other):
            analysis = Analysis(ticker="QQZ", user_id=user)
            db_session.add(analysis)
            await db_session.flush()
            rec = Recommendation(
                analysis_id=analysis.id, recommendation="buy", confidence=0.5,
                reasoning="r", debate_converged=True)
            db_session.add(rec)
            await db_session.flush()
            db_session.add(
                RecommendationTier(
                    recommendation_id=rec.id, side="buy",
                    target_price=Decimal("10"), quantity=Decimal("1"),
                    status=TierStatus.TRIGGERED.value, triggered_at=when,
                    triggered_price=Decimal("10")))
        await db_session.flush()

        repo = RecommendationTierRepository(db_session)
        window = {"start_date": date(2026, 1, 1), "end_date": date(2026, 2, 1)}
        assert (
            len(await repo.list_triggered_tiers_for_ticker("QQZ",
                                                           **window)) == 2)
        mine = await repo.list_triggered_tiers_for_ticker(
            "QQZ", user_id=me, **window)
        assert len(mine) == 1


def _response(text: str, model: str = "fake-lite") -> LLMResponse:
    return LLMResponse(
        content=text, provider="fake", model=model,
        usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


class FakeRouter:

    def __init__(self, answers=(), chunks=(), stream_error=None, delay=0.0):
        self.answers = list(answers)
        self.chunks = list(chunks)
        self.stream_error = stream_error
        self.delay = delay
        self.calls: list[list[LLMMessage]] = []
        self.stream_calls = 0

    async def complete(self, tier, messages, **kwargs):
        self.calls.append(messages)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _response(answer)

    async def stream(self, tier, messages, **kwargs):
        self.stream_calls += 1
        self.calls.append(messages)
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk
        if self.stream_error is not None:
            raise self.stream_error

    def resolve_model(self, tier, provider=None):
        return "fake-lite"


@pytest.fixture
def generous_rate_limits(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "RAG_ASK_RATE_LIMIT_PER_MINUTE", 10_000)
    monkeypatch.setattr(settings, "RAG_ASK_RATE_LIMIT_PER_DAY", 100_000)


@pytest.fixture
async def ask_qdrant(monkeypatch):
    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection(
        collection_name="research_documents",
        vectors_config=qmodels.VectorParams(
            size=_VEC, distance=qmodels.Distance.COSINE))

    async def _embed(text):
        return _vector(text)

    async def _orgs(user_id):
        return [UUID(_ORG)] if user_id == _ME else []

    import clients.auth_client as auth_client

    monkeypatch.setattr(indexing_module, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(retriever_module, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(
        indexing_module.settings, "QDRANT_COLLECTION_RESEARCH",
        "research_documents")
    monkeypatch.setattr(indexing_module, "_bm25_index", None)
    monkeypatch.setattr(retriever_module, "embed_query", _embed)
    monkeypatch.setattr(auth_client, "list_user_organization_ids", _orgs)
    docs = {
        "own_note": (
            ChunkVisibility.for_owner(_ME), DocumentCategory.ANALYST_REPORT),
        "org_note": (
            ChunkVisibility.for_owner(_OTHER, _ORG), DocumentCategory.NEWS),
        "system_filing": (ChunkVisibility.system(), DocumentCategory.FILING),
        "foreign_private": (
            ChunkVisibility.for_owner(_OTHER), DocumentCategory.FILING)
    }
    for tag, (vis, category) in docs.items():
        doc_id = str(uuid5(NAMESPACE_URL, "ask-" + tag))
        text = (
            f"Advanced Micro Devices revenue guidance datacenter growth {tag}")
        chunks = chunk_document(text, category)
        await index_chunks(
            document_id=doc_id, ticker="AMD", category=category, chunks=chunks,
            embeddings=[_vector(c.text) for c in chunks], source=tag,
            content_date="2026-08-01", visibility=vis)
    yield client


@pytest.fixture
def fake_router(monkeypatch):
    import domains.rag.qa as qa

    holder: dict[str, FakeRouter] = {}

    def _install(router: FakeRouter) -> FakeRouter:
        holder["router"] = router
        monkeypatch.setattr(qa, "LLMRouter", lambda: router)
        return router

    return _install


def _sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


@pytest.mark.usefixtures("generous_rate_limits")
class TestAsk:

    @pytest.mark.asyncio
    async def test_answers_drafts_no_sources_english_and_input_validation(
            self, client, ask_qdrant, fake_router):
        router = fake_router(
            FakeRouter(answers=["Revenue guidance rose [1][2]."]))
        response = await client.post(
            "/api/v1/rag/ask", headers=_headers(_ME), json={
                **QUESTION, "top_k": 10
            })
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["answer"] == "Revenue guidance rose [1][2]."
        assert data["grounded"] is True
        assert data["model"] == "fake-lite"
        assert data["mode"] == "ask"
        titles = {c["title"] for c in data["citations"]}
        assert titles == {"own_note", "org_note", "system_filing"}
        assert [c["index"] for c in data["citations"]] == [1, 2, 3]
        first = data["citations"][0]
        assert set(first) == {
            "index", "document_id", "title", "category", "ticker",
            "content_date", "snippet"
        }
        assert first["ticker"] == "AMD"
        assert first["content_date"] == "2026-08-01"
        assert "revenue guidance" in first["snippet"]
        prompt = router.calls[0][1].content
        for citation in data["citations"]:
            assert f"[{citation['index']}] {citation['title']}" in prompt

        router = fake_router(FakeRouter(answers=["A draft paragraph [1]."]))
        response = await client.post(
            "/api/v1/rag/ask", headers=_headers(_ME), json={
                **QUESTION, "mode": "draft",
                "categories": ["news", "filing"],
                "ticker": " amd "
            })
        data = response.json()["data"]
        assert data["mode"] == "draft"
        assert {c["category"] for c in data["citations"]} == {"news", "filing"}
        assert "draft paragraph" in router.calls[0][0].content

        router = fake_router(FakeRouter(answers=[]))
        response = await client.post(
            "/api/v1/rag/ask", headers=_headers(_ME), json={
                **QUESTION, "ticker": "ZZZZ"
            })
        data = response.json()["data"]
        assert response.status_code == 200
        assert data["grounded"] is False
        assert data["citations"] == []
        assert data["model"] is None
        assert "could not find any relevant documents" in data["answer"]
        assert router.calls == []

        error = LLMNonEnglishOutputError("not English", reason="cyrillic")
        router = fake_router(
            FakeRouter(answers=[error, "English answer [1]."]))
        ok = await client.post(
            "/api/v1/rag/ask", headers=_headers(_ME), json=QUESTION)
        assert ok.json()["data"]["answer"] == "English answer [1]."
        assert "English only" in router.calls[1][-1].content

        fake_router(FakeRouter(answers=[error, error]))
        rejected = await client.post(
            "/api/v1/rag/ask", headers=_headers(_ME), json=QUESTION)
        assert rejected.status_code == 503
        assert _CYRILLIC.strip() not in rejected.text

        bodies = [{
            "question": ""
        }, {
            "question": "   "
        }, {
            "question": "x" * 2001
        }, {
            "question": "ok",
            "mode": "essay"
        }, {
            "question": "ok",
            "top_k": 11
        }, {
            "question": "ok",
            "top_k": 0
        }, {
            "question": "ok",
            "ticker": "NOT A TICKER"
        }, {
            "question": "ok",
            "categories": ["poetry"]
        }, {
            "question": "ok",
            "unknown": 1
        }]
        for body in bodies:
            response = await client.post(
                "/api/v1/rag/ask", headers=_headers(str(uuid4())), json=body)
            assert response.status_code == 422, body

    @pytest.mark.asyncio
    async def test_minute_and_daily_rate_limits(
            self, client, ask_qdrant, fake_router, monkeypatch, _schema_ready):
        from config import settings
        from domains.rag.qa import check_rate_limit
        from rate_limit import UserRateLimitedError

        monkeypatch.setattr(settings, "RAG_ASK_RATE_LIMIT_PER_MINUTE", 2)
        import domains.rag.api as rag_api

        real_check = rag_api.check_rate_limit
        fixed = datetime(2026, 9, 29, 12, 0, 30, tzinfo=UTC)

        async def _at_fixed_moment(user_id):
            await real_check(user_id, now=fixed)

        monkeypatch.setattr(rag_api, "check_rate_limit", _at_fixed_moment)
        fake_router(FakeRouter(answers=["a [1].", "b [1].", "c [1]."]))
        user = str(uuid4())
        statuses = []
        for _ in range(3):
            response = await client.post(
                "/api/v1/rag/ask", headers=_headers(user), json=QUESTION)
            statuses.append(response.status_code)
        assert statuses == [200, 200, 429]
        assert 1 <= int(response.headers["Retry-After"]) <= 60
        streamed = await client.post(
            "/api/v1/rag/ask/stream", headers=_headers(user), json=QUESTION)
        assert streamed.status_code == 429

        monkeypatch.setattr(settings, "RAG_ASK_RATE_LIMIT_PER_DAY", 1)
        user = str(uuid4())
        now = datetime(2026, 9, 29, 23, 0, 0, tzinfo=UTC)
        await check_rate_limit(user, now=now)
        with pytest.raises(UserRateLimitedError) as exc:
            await check_rate_limit(user, now=now + timedelta(minutes=1))
        assert exc.value.retry_after == 59 * 60


@pytest.mark.usefixtures("generous_rate_limits")
class TestAskStream:

    @pytest.mark.asyncio
    async def test_event_order_no_sources_failures_and_fallback(
            self, client, ask_qdrant, fake_router):
        fake_router(FakeRouter(chunks=["Revenue ", "rose ", "[1]."]))
        response = await client.post(
            "/api/v1/rag/ask/stream", headers=_headers(_ME), json=QUESTION)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _sse(response.text)
        names = [name for name, _ in events]
        assert names == ["sources", "token", "token", "token", "done"]
        sources = events[0][1]["citations"]
        done = events[-1][1]
        assert done["answer"] == "Revenue rose [1]."
        assert done["citations"] == sources
        assert done["model"] == "fake-lite"
        assert isinstance(done["latency_ms"], int)
        assert "".join(e[1]["text"] for e in events
                       if e[0] == "token") == ("Revenue rose [1].")

        router = fake_router(FakeRouter())
        response = await client.post(
            "/api/v1/rag/ask/stream", headers=_headers(_ME), json={
                **QUESTION, "ticker": "ZZZZ"
            })
        events = _sse(response.text)
        assert [n for n, _ in events] == ["sources", "token", "done"]
        assert events[0][1]["citations"] == []
        assert events[-1][1]["grounded"] is False
        assert router.stream_calls == 0 and router.calls == []

        failures = {
            "mid-stream-llm-failure": (["Partial "],
                                       LLMRequestError("connection reset"),
                                       "llm_unavailable"),
            "unexpected-error": (["x"], KeyError("bug"), "internal_error")
        }
        for case_id, (chunks, stream_error, code) in failures.items():
            fake_router(FakeRouter(chunks=chunks, stream_error=stream_error))
            response = await client.post(
                "/api/v1/rag/ask/stream", headers=_headers(_ME), json=QUESTION)
            events = _sse(response.text)
            names = [n for n, _ in events]
            assert names == ["sources", "token", "error"], case_id
            assert events[-1][1]["code"] == code, case_id
            assert set(events[-1][1]) == {"code", "message"}, case_id

        fake_router(
            FakeRouter(
                answers=["Recovered answer [1]."],
                stream_error=LLMRateLimitError("429 on open")))
        response = await client.post(
            "/api/v1/rag/ask/stream", headers=_headers(_ME), json=QUESTION)
        events = _sse(response.text)
        assert [n for n, _ in events] == ["sources", "token", "done"]
        assert events[-1][1]["answer"] == "Recovered answer [1]."

        fake_router(
            FakeRouter(
                answers=[LLMRateLimitError("still down")],
                stream_error=LLMRateLimitError("429 on open")))
        response = await client.post(
            "/api/v1/rag/ask/stream", headers=_headers(_ME), json=QUESTION)
        events = _sse(response.text)
        assert [n for n, _ in events] == ["sources", "error"]

    @pytest.mark.asyncio
    async def test_timeout_is_an_error_event_and_disconnect_stops(
            self, ask_qdrant, monkeypatch):
        from config import settings
        from domains.rag.qa import retrieve_sources, stream_answer
        from domains.rag.schema import RagAskRequest

        monkeypatch.setattr(settings, "RAG_ASK_STREAM_TIMEOUT_SECONDS", 0.05)
        router = FakeRouter(chunks=["slow", "slower"], delay=0.2)
        request = RagAskRequest(**QUESTION)
        chunks = await retrieve_sources(request, VisibilityScope.for_user(_ME))
        events = [
            e async for e in stream_answer(
                request, chunks, started=0.0, llm_router=router)
        ]
        assert [e.event for e in events] == ["sources", "error"]
        assert events[-1].data["code"] == "timeout"

        consumed: list[str] = []

        class _Router(FakeRouter):

            async def stream(self, tier, messages, **kwargs):
                try:
                    for chunk in ["a", "b", "c", "d"]:
                        consumed.append(chunk)
                        yield chunk
                finally:
                    consumed.append("closed")

        disconnected = {"value": False}

        async def _is_disconnected():
            return disconnected["value"]

        events = []
        async for event in stream_answer(request, chunks, started=0.0,
                                         llm_router=_Router(),
                                         is_disconnected=_is_disconnected):
            events.append(event.event)
            if event.event == "token":
                disconnected["value"] = True
        assert events == ["sources", "token"]
        assert consumed[-1] == "closed"
        assert "d" not in consumed

    @pytest.mark.asyncio
    async def test_one_deadline_covers_tokens_and_fallback_not_the_client(
            self, ask_qdrant, monkeypatch):
        from config import settings
        from domains.rag.qa import retrieve_sources, stream_answer
        from domains.rag.schema import RagAskRequest

        request = RagAskRequest(**QUESTION)
        chunks = await retrieve_sources(request, VisibilityScope.for_user(_ME))

        async def _collect(router, client_delay=0.0):
            events = []
            async for event in stream_answer(
                    request, chunks, started=0.0, llm_router=router):
                events.append(event)
                await asyncio.sleep(client_delay)
            return events

        monkeypatch.setattr(settings, "RAG_ASK_STREAM_TIMEOUT_SECONDS", 0.1)
        events = await _collect(
            FakeRouter(chunks=["a", "b", "c"]), client_delay=0.06)
        assert [e.event for e in events] == [
            "sources", "token", "token", "token", "done"
        ]

        monkeypatch.setattr(settings, "RAG_ASK_STREAM_TIMEOUT_SECONDS", 0.3)
        events = await _collect(FakeRouter(chunks=["a", "b"], delay=0.2))
        assert [e.event for e in events] == ["sources", "token", "error"]
        assert events[-1].data["code"] == "timeout"

        class _SlowFallback(FakeRouter):

            async def complete(self, tier, messages, **kwargs):
                await asyncio.sleep(5)
                return await super().complete(tier, messages, **kwargs)

        monkeypatch.setattr(settings, "RAG_ASK_STREAM_TIMEOUT_SECONDS", 0.1)
        events = await _collect(
            _SlowFallback(
                answers=["never"], stream_error=LLMRateLimitError("429")))
        assert [e.event for e in events] == ["sources", "error"]
        assert events[-1].data["code"] == "timeout"


class _GoogleChunk:

    def __init__(self, text):
        self.text = text


class _GoogleReservation:
    key = ("google", "m", "k1")
    wait = 0.0
    gate_wait = 0.0
    reserved = True


class _SlowAfterFirstChunkModels:

    async def generate_content_stream(self, **kwargs):

        async def _slow_stream():
            yield _GoogleChunk("first")
            await asyncio.sleep(5)
            yield _GoogleChunk("never")

        return _slow_stream()


class _SlowToStartModels:

    async def generate_content_stream(self, **kwargs):
        await asyncio.sleep(5)


class TestStreamPlumbing:

    @pytest.mark.asyncio
    async def test_router_wraps_errors_and_google_stream_times_out(
            self, monkeypatch):
        import ai.llm.google_provider as gp
        from ai.llm.router import LLMRouter

        class _Adapter:
            name = "fake"

            async def stream(self, messages, **kwargs):
                yield "x"
                raise KeyError("sdk bug")

        router = LLMRouter(providers={"google": _Adapter()})
        out = []
        with pytest.raises(LLMRequestError):
            async for chunk in router.stream(
                    LLMTier.CHEAP, [LLMMessage(role="user", content="q")]):
                out.append(chunk)
        assert out == ["x"]
        assert router.resolve_model(LLMTier.CHEAP)

        async def _reserve(model, candidates, **kw):
            return _GoogleReservation()

        monkeypatch.setattr(
            gp, "_default_request_timeout", lambda model, **kw: 0.05)
        monkeypatch.setattr(gp, "_reserve_dispatch_slot", _reserve)
        monkeypatch.setattr(gp._pool, "key_count", lambda: 1)
        monkeypatch.setattr(
            gp._pool, "available_labels_excluding", lambda tried: []
            if tried else ["k1"])
        cases = {
            "chunk-timeout": (_SlowAfterFirstChunkModels(), ["first"]),
            "start-timeout": (_SlowToStartModels(), [])
        }
        for case_id, (models, expected_out) in cases.items():
            client = SimpleNamespace(aio=SimpleNamespace(models=models))
            monkeypatch.setattr(
                gp._pool, "client_for", lambda label, c=client: c)
            adapter = gp.GoogleAdapter()
            out = []
            with pytest.raises(LLMProviderTimeoutError):
                async for text in adapter.stream(
                    [LLMMessage(role="user", content="q")],
                        model="gemini-test-lite"):
                    out.append(text)
            assert out == expected_out, case_id


class TestNewestFilingReserved:

    @pytest.fixture
    async def filings(self, ask_qdrant):

        async def _index(tag, text, date, vis):
            doc_id = str(uuid5(NAMESPACE_URL, "newest-" + tag))
            chunks = chunk_document(text, DocumentCategory.FILING)
            await index_chunks(
                document_id=doc_id, ticker="AMD",
                category=DocumentCategory.FILING, chunks=chunks,
                embeddings=[_vector(c.text) for c in chunks], source=tag,
                content_date=date, visibility=vis)
            return doc_id

        strong = (
            "AMD 10-Q quarterly report revenue gross margin operating income")
        old_ids = [
            await _index(
                f"old_{i}", f"{strong} older quarter {i}", f"2025-0{i}-01",
                ChunkVisibility.system()) for i in range(1, 5)
        ]
        newest = await _index(
            "newest", "Second quarter results summary", "2026-08-05",
            ChunkVisibility.system())
        hidden = await _index(
            "foreign_newer", f"{strong} private", "2026-09-01",
            ChunkVisibility.for_owner(_OTHER))
        return {"old": old_ids, "newest": newest, "foreign": hidden}

    @pytest.mark.asyncio
    async def test_no_reservation_without_ticker_or_filings_in_scope(
            self, filings):
        from domains.rag.qa import (_ASK_SYSTEM_PROMPT, _DRAFT_SYSTEM_PROMPT,
                                    retrieve_sources)
        from domains.rag.schema import RagAskRequest

        scope = VisibilityScope.for_user(_ME)
        news_only = RagAskRequest(
            question="AMD 10-Q quarterly report revenue", ticker="AMD",
            categories=[DocumentCategory.NEWS], top_k=3)
        chunks = await retrieve_sources(news_only, scope)
        assert all(c.category == DocumentCategory.NEWS for c in chunks)
        no_ticker = RagAskRequest(
            question="AMD 10-Q quarterly report revenue gross margin", top_k=2)
        chunks = await retrieve_sources(no_ticker, scope)
        assert filings["newest"] not in [c.document_id for c in chunks]

        for prompt in (_ASK_SYSTEM_PROMPT, _DRAFT_SYSTEM_PROMPT):
            assert "most recently filed source" in prompt
            assert "NOT the end of the period" in prompt


class TestAutoIngestionContentDate:

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ingestion_sources")
    async def test_news_digest_and_macro_report_pass_content_date(
            self, monkeypatch):
        created: list[dict[str, Any]] = []

        async def _create(**kwargs):
            created.append(kwargs)
            return "id"

        async def _tickers():
            return ["AMD"]

        async def _existing(ticker, category):
            return []

        async def _no_filings(ticker, *, already_ingested_filenames):
            return []

        async def _digest(ticker, *, already_ingested_filenames):
            return NewsDigest(
                filename="AMD_news_2026-09-28.txt", text="t", article_count=3,
                content_date="2026-09-28")

        async def _macro(*, already_ingested_filenames):
            return MacroReport(
                filename="macro_auto_2026-09.txt", text="m",
                content_date="2026-09-28")

        for module in (auto_workflow, sec_edgar_filings):
            monkeypatch.setattr(module, "create_system_document", _create)
            monkeypatch.setattr(
                module, "get_existing_document_filenames", _existing)
        monkeypatch.setattr(auto_workflow, "get_tracked_tickers", _tickers)
        monkeypatch.setattr(sec_edgar_filings, "list_new_filings", _no_filings)
        monkeypatch.setattr(auto_workflow, "generate_news_digest", _digest)
        monkeypatch.setattr(auto_workflow, "generate_macro_report", _macro)

        await auto_workflow.run_auto_ingestion_workflow()
        await auto_workflow.run_macro_auto_ingestion_workflow()
        assert [c["content_date"]
                for c in created] == ["2026-09-28", "2026-09-28"]


class _FakeIngestionSession:

    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.log.append("commit")


def _patch_ingestion(monkeypatch, log: list[str], *, index_error: Exception):
    job_id = uuid4()

    class _JobRepo:

        def __init__(self, session):
            pass

        async def delete_for_document(self, document_id):
            log.append("job_rows_deleted")
            return 0

        async def create(self, **kwargs):
            return SimpleNamespace(id=job_id)

        async def set_status(self, jid, status):
            log.append(f"job_status:{status}")
            return True

    class _ChunkRepo:

        def __init__(self, session):
            pass

        async def delete_for_document(self, document_id):
            log.append("chunk_rows_deleted")
            return 1

    async def _noop(*args, **kwargs):
        return None

    async def _load(*args, **kwargs):
        return b"hello world"

    async def _embed(texts):
        return [[0.1] for _ in texts]

    async def _delete_points(document_id):
        log.append("points_deleted")
        return 1

    async def _index(**kwargs):
        raise index_error

    monkeypatch.setattr(ingestion_module, "_publish_status", _noop)
    monkeypatch.setattr(ingestion_module, "download_bytes", _load)
    monkeypatch.setattr(
        ingestion_module, "parse_document", lambda c, t: "Some text.")
    monkeypatch.setattr(ingestion_module, "embed_texts", _embed)
    monkeypatch.setattr(
        ingestion_module, "delete_document_chunks", _delete_points)
    monkeypatch.setattr(ingestion_module, "index_chunks", _index)
    monkeypatch.setattr(
        ingestion_module, "SessionLocal", lambda: _FakeIngestionSession(log))
    monkeypatch.setattr(ingestion_module, "EmbeddingJobRepository", _JobRepo)
    monkeypatch.setattr(
        ingestion_module, "DocumentChunkRepository", _ChunkRepo)


_INGESTION_KWARGS: dict[str, Any] = {
    "document_id": str(uuid4()),
    "storage_bucket": "b",
    "storage_key": "k",
    "document_type": "txt",
    "category": "news",
    "ticker": "AMD"
}


class TestIngestionFailureBookkeeping:

    @pytest.mark.asyncio
    async def test_permanent_and_transient_failures(self, monkeypatch):
        log: list[str] = []
        _patch_ingestion(
            monkeypatch, log, index_error=ValueError("bad payload"))
        await ingestion_module.run_rag_ingestion_workflow(**_INGESTION_KWARGS)
        assert log.index("points_deleted") < log.index("chunk_rows_deleted")
        assert "job_status:failed" in log
        assert log.count("job_rows_deleted") == 1

        log = []
        _patch_ingestion(
            monkeypatch, log, index_error=LLMRateLimitError("429"))
        with pytest.raises(ingestion_module.TransientIngestionError):
            await ingestion_module.run_rag_ingestion_workflow(
                **_INGESTION_KWARGS)
        assert "job_status:failed" in log

        is_transient = ingestion_module.is_transient_ingestion_error
        sdk_503 = Exception("x")
        sdk_503.raw_response = SimpleNamespace(  # type: ignore[attr-defined]
            status_code=503
        )
        sdk_400 = Exception("x")
        sdk_400.raw_response = SimpleNamespace(  # type: ignore[attr-defined]
            status_code=400
        )
        assert is_transient(TimeoutError())
        assert is_transient(sdk_503)
        assert not is_transient(sdk_400)
        assert not is_transient(ValueError("zero chunks"))


class TestDocumentDeletedEvent:

    @pytest.mark.usefixtures("_schema_ready")
    @pytest.mark.asyncio
    async def test_event_clears_every_store_and_needs_an_id(self, monkeypatch):
        import event_listener
        from domains.rag.model import DocumentChunk, EmbeddingJob
        from domains.rag.repository import EmbeddingJobRepository

        gone, kept = uuid4(), uuid4()
        async with SessionLocal() as session:
            for document_id in (gone, kept):
                await DocumentChunkRepository(session).bulk_create(
                    document_id=document_id,
                    rows=[(0, 10, f"{document_id}-chunk-0")])
                await EmbeddingJobRepository(session).create(
                    document_id=document_id, embedding_model="m",
                    status="completed")
            await session.commit()

        vector_deletes = []

        async def _fake_delete_chunks(document_id):
            vector_deletes.append(document_id)
            return 1

        monkeypatch.setattr(
            indexing_module, "delete_document_chunks", _fake_delete_chunks)
        await event_listener._handle_document_deleted(
            {"document_id": str(gone)})

        assert vector_deletes == [str(gone)]
        async with SessionLocal() as session:
            for table in (DocumentChunk, EmbeddingJob):
                left = set((
                    await session.scalars(
                        select(table.document_id).where(
                            table.document_id.in_([gone, kept])))).all())
                assert left == {kept}, table
            await DocumentChunkRepository(session).delete_for_document(kept)
            await EmbeddingJobRepository(session).delete_for_document(kept)
            await session.commit()

        async def _must_not_run(document_id):
            raise AssertionError("no delete without a document id")

        monkeypatch.setattr(
            indexing_module, "delete_document_chunks", _must_not_run)
        await event_listener._handle_document_deleted({})
