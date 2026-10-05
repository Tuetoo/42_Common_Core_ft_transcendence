import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_AI_SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(_AI_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_AI_SERVICE_DIR))


def merge_pairs(existing: list[Any], new: list[Any]) -> list[Any]:
    seen = {p.chunk_id for p in existing}
    merged = list(existing)
    for pair in new:
        if pair.chunk_id not in seen:
            merged.append(pair)
            seen.add(pair.chunk_id)
    return merged


def category_counts(pairs: list[Any]) -> dict[str, int]:
    return dict(sorted(Counter(p.category.value for p in pairs).items()))


DESCRIPTION = (
    "Generates RAG golden-set questions (one per indexed chunk) with the")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--category", action="append", dest="categories",
        help="only this DocumentCategory value; repeatable "
        "(default: every category)")
    parser.add_argument(
        "--merge", action="store_true",
        help="read the existing set from stdin, skip its "
        "chunks, and output existing + new")
    parser.add_argument(
        "--output", required=True,
        help="path inside the container, e.g. /tmp/x.json")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="scroll at most N chunks (quick test)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would be generated, no LLM call")
    return parser


async def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    from ai.evaluation.golden_set import (GoldenQAPair,
                                          _drop_near_duplicate_chunks,
                                          _real_indexed_chunk_count,
                                          _sample_indexed_chunks,
                                          generate_golden_set, save_golden_set)
    from shared.enums import DocumentCategory

    categories: frozenset[str] | None = None
    if args.categories:
        valid = {c.value for c in DocumentCategory}
        unknown = sorted(set(args.categories) - valid)
        if unknown:
            print(f"Unknown category {unknown}; valid: {sorted(valid)}")
            return 2
        categories = frozenset(args.categories)

    existing: list[GoldenQAPair] = []
    if args.merge:
        existing = [
            GoldenQAPair.model_validate(i) for i in json.load(sys.stdin)
        ]
        print(
            f"Existing set: {len(existing)} question(s) "
            f"{category_counts(existing)}")
    skip = frozenset(p.chunk_id for p in existing)

    sample_size = args.limit or await _real_indexed_chunk_count()
    chunks = await _sample_indexed_chunks(sample_size)
    if categories is not None:
        chunks = [c for c in chunks if c["category"] in categories]
    chunks = [c for c in chunks if c["chunk_id"] not in skip]
    kept = await _drop_near_duplicate_chunks(chunks)
    planned = dict(sorted(Counter(c["category"] for c in kept).items()))
    print(
        f"Chunks to generate questions for: {len(kept)} {planned} "
        f"({len(chunks) - len(kept)} near-duplicate chunk(s) left out); "
        f"one CHEAP-tier call each.")
    if args.dry_run or not kept:
        print(
            "Dry run: nothing generated." if args.dry_run else "Nothing to do."
        )
        return 0

    result = await generate_golden_set(
        sample_size=sample_size, categories=categories, skip_chunk_ids=skip)
    print(
        f"Generated {len(result.pairs)} question(s) from "
        f"{result.sampled_count} chunk(s) {category_counts(result.pairs)}.")
    if result.failed_chunk_ids:
        print(
            f"WARNING: {len(result.failed_chunk_ids)} chunk(s) failed and "
            f"were "
            f"skipped: {result.failed_chunk_ids}")
    if result.duplicate_question_chunk_ids:
        print(
            f"NOTE: {len(result.duplicate_question_chunk_ids)} "
            f"exact-duplicate "
            f"question(s) dropped: {result.duplicate_question_chunk_ids}")

    pairs = merge_pairs(existing, result.pairs) if args.merge else result.pairs
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    save_golden_set(pairs, output)
    print(
        f"Saved {len(pairs)} question(s) {category_counts(pairs)} to {output}."
    )
    print("Category weights were NOT changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
