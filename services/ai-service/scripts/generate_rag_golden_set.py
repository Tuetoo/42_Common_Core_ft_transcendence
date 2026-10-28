import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


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
    "Generates RAG golden-set questions (one per indexed chunk) with the "
    "CHEAP LLM tier.")


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

    from ai.evaluation.rag_recall import (GoldenQAPair, generate_golden_set,
                                          save_golden_set,
                                          select_golden_set_chunks)
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

    kept, candidate_count = await select_golden_set_chunks(
        sample_size=args.limit, categories=categories, skip_chunk_ids=skip)
    planned = dict(sorted(Counter(c["category"] for c in kept).items()))
    print(
        f"Chunks to generate questions for: {len(kept)} {planned} "
        f"({candidate_count - len(kept)} near-duplicate chunk(s) left out); "
        f"one CHEAP-tier call each.")
    if args.dry_run or not kept:
        print(
            "Dry run: nothing generated." if args.dry_run else "Nothing to do."
        )
        return 0

    result = await generate_golden_set(kept)
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
