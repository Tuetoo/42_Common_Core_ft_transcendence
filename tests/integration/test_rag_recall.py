from pathlib import Path
import pytest

_GOLDEN_SET_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" /
    "rag_golden_set.json")

_MIN_RECALL_BM25 = 0.87
_MIN_RECALL_VECTOR = 0.72
_MIN_RECALL_FUSED = 0.86


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_at_5_meets_baseline():
    if not _GOLDEN_SET_PATH.exists():
        pytest.skip(
            f"No golden set at {_GOLDEN_SET_PATH} -- run "
            "scripts/generate_rag_golden_set.py first (requires real "
            "indexed Qdrant data and an LLM key)")

    from ai.evaluation.golden_set import load_golden_set
    from ai.evaluation.recall import compute_all_recall_curves

    golden_set = load_golden_set(_GOLDEN_SET_PATH)
    curves = await compute_all_recall_curves(golden_set, top_k=5)

    for label, result in curves.items():
        print(
            f"Recall@5 [{label}]: {result.recall} "
            f"({result.hits}/{result.total}) | "
            f"Recall@1={result.recall_at_1:.3f} "
            f"Recall@3={result.recall_at_3:.3f} "
            f"MRR={result.mrr:.3f}")

    assert (
        curves["bm25"].recall is not None
        and curves["bm25"].recall >= _MIN_RECALL_BM25)
    assert (
        curves["vector"].recall is not None
        and curves["vector"].recall >= _MIN_RECALL_VECTOR)
    assert (
        curves["fused"].recall is not None
        and curves["fused"].recall >= _MIN_RECALL_FUSED)
