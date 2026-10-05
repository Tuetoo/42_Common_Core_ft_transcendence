from dataclasses import dataclass, field
from ai.evaluation.golden_set import GoldenQAPair
from ai.rag.embedding import embed_texts
from ai.rag.retriever import BaseRetriever
from shared.enums import DocumentCategory


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
        category_filter=category_filter)

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
