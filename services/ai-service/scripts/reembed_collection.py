import argparse
import asyncio
import sys
from typing import Any
from ai.rag.embedding import google_batches, google_embed_batch
from config import settings
from qdrant import get_qdrant_client
from qdrant_client.http import models as qmodels


async def _all_points(collection: str,
                      with_vectors: bool = False) -> list[Any]:
    client = get_qdrant_client()
    points: list[Any] = []
    offset: Any = None
    while True:
        batch, offset = await client.scroll(
            collection_name=collection, limit=256, offset=offset,
            with_payload=True, with_vectors=with_vectors)
        points.extend(batch)
        if offset is None:
            return points


async def _vector_size(collection: str) -> int | None:
    client = get_qdrant_client()
    if not await client.collection_exists(collection_name=collection):
        return None
    info = await client.get_collection(collection_name=collection)
    size = getattr(info.config.params.vectors, "size", None)
    return int(size) if size is not None else None


async def run(
        *, source: str, target: str, model: str, dims: int, confirm: bool,
        copy_all: bool = False) -> int:
    print("=== CONFIRM ===" if confirm else "=== PREVIEW (read-only) ===")
    if source == target:
        print("ABORT: --source and --target are the same collection")
        return 0
    source_size = await _vector_size(source)
    if source_size is None:
        print(f"ABORT: source collection {source!r} does not exist")
        return 0
    target_size = await _vector_size(target)
    if target_size is not None and target_size != dims:
        print(
            f"ABORT: target {target!r} exists with {target_size}-dim vectors, "
            f"not {dims}")
        return 0
    points = await _all_points(source)
    existing_ids = ({str(p.id)
                     for p in await _all_points(target)}
                    if target_size is not None and not copy_all else set())
    todo = [p for p in points if str(p.id) not in existing_ids]
    no_text = [
        p for p in todo
        if not str((p.payload or {}).get("text") or "").strip()
    ]
    todo = [p for p in todo if p not in no_text]
    print(f"source {source!r}: {len(points)} points ({source_size}-dim)")
    print(
        f"target {target!r}: " + (
            f"exists, {len(existing_ids)} points already present"
            if target_size is not None else
            f"will be created ({dims}-dim, cosine)"))
    print(
        f"model {model} @ {dims} dims; points to embed and write: {len(todo)}"
        +
        (f"; skipped without payload text: {len(no_text)}" if no_text else ""))
    if not confirm:
        print("\nPreview only -- re-run with --confirm to write.")
        return 0

    client = get_qdrant_client()
    if target_size is None:
        await client.create_collection(
            collection_name=target, vectors_config=qmodels.VectorParams(
                size=dims, distance=qmodels.Distance.COSINE))
    written = 0
    start = 0
    for text_batch in google_batches([str(p.payload["text"]) for p in todo]):
        batch = todo[start:start + len(text_batch)]
        start += len(text_batch)
        vectors = await google_embed_batch(
            text_batch, "document", model=model, dimensions=dims)
        await client.upsert(
            collection_name=target, points=[
                qmodels.PointStruct(id=p.id, vector=v, payload=p.payload)
                for p, v in zip(batch, vectors, strict=True)
            ])
        written += len(batch)
        print(f"  {written}/{len(todo)} written")
    final = len(await _all_points(target))
    print(
        f"\nDone: {written} points written; target now holds {final} "
        f"(source {len(points)}).")
    return written


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.reembed_collection")
    parser.add_argument(
        "--source", required=True,
        help="collection to copy from (the live one before a switch)")
    parser.add_argument(
        "--target", default=settings.QDRANT_COLLECTION_RESEARCH)
    parser.add_argument("--model", default=settings.GOOGLE_EMBEDDING_MODEL)
    parser.add_argument(
        "--dims", type=int, default=settings.GOOGLE_EMBEDDING_DIMENSIONS)
    parser.add_argument(
        "--all", dest="copy_all", action="store_true",
        help="re-embed points already present in the target too")
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    _a = parse_args(sys.argv[1:])
    asyncio.run(
        run(
            source=_a.source, target=_a.target, model=_a.model, dims=_a.dims,
            confirm=_a.confirm, copy_all=_a.copy_all))
