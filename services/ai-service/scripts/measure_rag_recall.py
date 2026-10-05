import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_AI_SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(_AI_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_AI_SERVICE_DIR))

from ai.evaluation.recall import (Score, embed_golden_questions,  # noqa: E402
                                  rank_of)

BASELINE_2026_08_27 = (
    "2026-08-27 baseline (mistral-embed, older corpus): BM25 R@5 ~91-94%, "
    "vector ~83%, fused ~94%, fused MRR ~0.75")
WEIGHT_GRID: tuple[tuple[float, float],
                   ...] = ((1.0, 0.0), (0.9, 0.1), (0.8, 0.2), (0.7, 0.3),
                           (0.6, 0.4), (0.5, 0.5))
SWEEP_MIN_QUESTIONS = 10
THIN_MARGIN_HITS = 2
CATEGORY_WEIGHTS_PATH = (
    _AI_SERVICE_DIR / "ai" / "rag" / "category_weights.json")

CURVES: dict[str, frozenset[str] | None] = {
    "bm25": frozenset({"bm25"}),
    "vector": frozenset({"vector"}),
    "fused": frozenset({"bm25", "vector"}),
    "production": None
}


def format_row(label: str, score: Score, top_k: int) -> str:
    return (
        f"  {label:<26} n={score.total:<4} R@1={score.recall(1):6.1%}  "
        f"R@5={score.recall(5):6.1%}  R@{top_k}={score.recall(top_k):6.1%}  "
        f"MRR@{top_k}={score.mrr():.3f}")


async def existing_chunk_ids() -> set[str]:
    from config import settings
    from qdrant import get_qdrant_client

    client = get_qdrant_client()
    ids: set[str] = set()
    offset: Any = None
    while True:
        points, offset = await client.scroll(
            collection_name=settings.QDRANT_COLLECTION_RESEARCH, limit=512,
            offset=offset, with_payload=False, with_vectors=False)
        ids.update(str(p.id) for p in points)
        if offset is None:
            return ids


@dataclass
class SweepResult:
    category: str
    questions: int
    hits_by_ratio: dict[tuple[float, float], int]
    live_ratio: tuple[float, float]

    @property
    def best(self) -> tuple[tuple[float, float], int]:
        ratio = max(self.hits_by_ratio, key=lambda r: self.hits_by_ratio[r])
        return ratio, self.hits_by_ratio[ratio]

    @property
    def margin(self) -> int:
        return self.best[1] - self.hits_by_ratio.get(self.live_ratio, 0)


def live_ratio(weights: dict[str, float] | None) -> tuple[float, float]:
    if not weights:
        return (0.5, 0.5)
    ratio = (
        round(weights.get("bm25", 1.0),
              1), round(weights.get("vector", 1.0), 1))
    return (0.5, 0.5) if ratio == (1.0, 1.0) else ratio


async def sweep_category_weights(
        usable: list[Any], vectors: list[list[float]]) -> list[SweepResult]:
    import ai.rag.retriever as retriever_module
    from ai.rag.retriever import BaseRetriever

    by_category: dict[Any, list[tuple[Any, list[float]]]] = defaultdict(list)
    for pair, vector in zip(usable, vectors, strict=True):
        by_category[pair.category].append((pair, vector))
    live = dict(retriever_module._CATEGORY_SOURCE_WEIGHTS)
    results: list[SweepResult] = []
    try:
        for category in sorted(by_category, key=lambda c: c.value):
            items = by_category[category]
            if len(items) < SWEEP_MIN_QUESTIONS:
                print(
                    f"{category.value}: only {len(items)} question(s), skipped"
                )
                continue
            hits_by_ratio: dict[tuple[float, float], int] = {}
            for bm25_w, vector_w in WEIGHT_GRID:
                retriever_module._CATEGORY_SOURCE_WEIGHTS[category] = {
                    "bm25": bm25_w,
                    "vector": vector_w
                }
                retriever = BaseRetriever(
                    top_k=5, use_query_expansion=False,
                    category_filter=category)
                hits = 0
                for pair, vector in items:
                    found = await retriever.retrieve(
                        pair.question, ticker=pair.ticker,
                        sources=frozenset({"bm25",
                                           "vector"}), query_vector=vector)
                    if rank_of([r.chunk_id for r in found], pair.chunk_id):
                        hits += 1
                hits_by_ratio[(bm25_w, vector_w)] = hits
            results.append(
                SweepResult(
                    category=category.value, questions=len(items),
                    hits_by_ratio=hits_by_ratio,
                    live_ratio=live_ratio(live.get(category))))
    finally:
        retriever_module._CATEGORY_SOURCE_WEIGHTS.clear()
        retriever_module._CATEGORY_SOURCE_WEIGHTS.update(live)
    return results


def print_sweep(results: list[SweepResult]) -> None:
    for result in results:
        print(f"\n=== {result.category} ({result.questions} questions) ===")
        for (bm25_w, vector_w), hits in result.hits_by_ratio.items():
            print(
                f"  bm25={bm25_w:.1f} vector={vector_w:.1f}  "
                f"R@5={hits / result.questions:.3f} "
                f"({hits}/{result.questions})")
        (bm25_w, vector_w), hits = result.best
        live_bm25, live_vector = result.live_ratio
        print(
            f"  -> best bm25={bm25_w:.1f} vector={vector_w:.1f} ({hits} "
            f"hits); "
            f"live bm25={live_bm25:.1f} vector={live_vector:.1f}; "
            f"gain {result.margin} hit(s)")
        if 0 < result.margin <= THIN_MARGIN_HITS:
            print(
                f"  !! THIN MARGIN: {result.margin} hit(s) in "
                f"{result.questions} questions -- likely noise")


def apply_sweep(
        results: list[SweepResult],
        path: Path = CATEGORY_WEIGHTS_PATH) -> list[str]:
    current = json.loads(path.read_text()) if path.exists() else {}
    written: list[str] = []
    for result in results:
        if result.margin <= 0:
            continue
        (bm25_w, vector_w), hits = result.best
        evidence = (
            f"scripts/measure_rag_recall.py --sweep: "
            f"{hits}/{result.questions} "
            f"Recall@5 hits at this ratio, {result.margin} more than the "
            "previous config.")
        if result.margin <= THIN_MARGIN_HITS:
            evidence += (
                " THIN MARGIN: applied anyway (a stable plateau across grid "
                "points is a real signal), re-confirm on the next sweeps.")
        current[result.category] = {
            "bm25": bm25_w,
            "vector": vector_w,
            "_evidence": evidence
        }
        written.append(result.category)
    if written:
        path.write_text(json.dumps(current, indent=2) + "\n")
    return written


DESCRIPTION = "Owner-run RAG measurement: Recall@1/@5/@10 and MRR@10 of"


async def main() -> None:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--sweep", action="store_true",
        help="tune category_weights.json instead of measuring (dry run)")
    parser.add_argument(
        "--apply", action="store_true",
        help="with --sweep: write the winners to category_weights.json")
    args = parser.parse_args()
    if args.apply and not args.sweep:
        parser.error("--apply only works with --sweep")

    from ai.evaluation.golden_set import GoldenQAPair
    from ai.rag.embedding import embedding_model_name
    from ai.rag.indexing import rebuild_bm25_from_qdrant
    from ai.rag.retriever import BaseRetriever

    raw = json.load(sys.stdin)
    pairs = [GoldenQAPair.model_validate(item) for item in raw]
    if args.limit:
        pairs = pairs[:args.limit]
    print(
        f"Golden set: {len(pairs)} question(s). Embedding model: "
        f"{embedding_model_name()}.")

    present = await existing_chunk_ids()
    indexed = await rebuild_bm25_from_qdrant()
    print(f"Collection: {len(present)} chunk(s); BM25 rebuilt with {indexed}.")
    missing: dict[str, int] = defaultdict(int)
    usable = []
    for pair in pairs:
        if pair.chunk_id in present:
            usable.append(pair)
        else:
            missing[pair.category.value] += 1
    print(
        f"Questions whose chunk still exists: {len(usable)}; missing "
        f"{sum(missing.values())} {dict(sorted(missing.items()))}.\n")
    if not usable:
        return

    vectors = await embed_golden_questions(usable)
    if args.sweep:
        sweep = await sweep_category_weights(usable, vectors)
        print_sweep(sweep)
        if not args.apply:
            print(
                "\nDry run: nothing written (add --apply to write the "
                "winners).")
            return
        written = apply_sweep(sweep)
        print(
            f"\nWrote {len(written)} "
            f"categor{'y' if len(written) == 1 else 'ies'} "
            f"to {CATEGORY_WEIGHTS_PATH}: {', '.join(written) or '(none)'}. "
            "Restart ai-service to load them.")
        return
    overall: dict[str, Score] = {name: Score() for name in CURVES}
    by_category: dict[str, dict[str, Score]] = defaultdict(
        lambda: {name: Score()
                 for name in CURVES})
    for index, (pair, vector) in enumerate(zip(usable, vectors, strict=True),
                                           1):
        for name, sources in CURVES.items():
            retriever = BaseRetriever(
                top_k=args.top_k, use_query_expansion=False,
                category_filter=pair.category if sources is None else None)
            results = await retriever.retrieve(
                pair.question, ticker=pair.ticker, sources=sources,
                query_vector=vector)
            rank = rank_of([r.chunk_id for r in results], pair.chunk_id)
            overall[name].add(rank)
            by_category[pair.category.value][name].add(rank)
        if index % 100 == 0:
            print(f"  ... {index}/{len(usable)} questions scored", flush=True)

    print(f"\nOverall (top {args.top_k}):")
    for name in CURVES:
        print(format_row(name, overall[name], args.top_k))
    print("\nBy category:")
    for category in sorted(by_category):
        print(f" {category}")
        for name in CURVES:
            print(format_row(name, by_category[category][name], args.top_k))
    print(f"\n{BASELINE_2026_08_27}")
    print(
        "'production' = what an agent's search tool gets (category filter on, "
        "category source weights applied). Send this output to Claude.")


if __name__ == "__main__":
    asyncio.run(main())
