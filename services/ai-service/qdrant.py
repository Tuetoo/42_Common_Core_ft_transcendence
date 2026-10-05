from config import settings
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

_client: AsyncQdrantClient | None = None


def get_qdrant_client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(
            url=settings.assemble_qdrant_url(), api_key=settings.QDRANT_API_KEY
            or None)
    return _client


async def ensure_collection(
        collection_name: str = settings.QDRANT_COLLECTION_RESEARCH,
        vector_size: int = settings.QDRANT_VECTOR_SIZE) -> None:
    client = get_qdrant_client()
    if not await client.collection_exists(collection_name=collection_name):
        await client.create_collection(
            collection_name=collection_name,
            vectors_config=qmodels.VectorParams(
                size=vector_size, distance=qmodels.Distance.COSINE))
        return
    info = await client.get_collection(collection_name=collection_name)
    vectors = info.config.params.vectors
    existing_size = getattr(vectors, "size", None)
    if existing_size is not None and existing_size != vector_size:
        raise RuntimeError(
            f"Qdrant collection {collection_name!r} holds {existing_size}-dim "
            f"vectors but the configured embedding model produces "
            f"{vector_size}; point QDRANT_COLLECTION_RESEARCH at a collection "
            "built for this model (scripts/reembed_collection.py)")


async def check_qdrant_connection() -> bool:
    try:
        await get_qdrant_client().get_collections()
        return True
    except Exception:
        return False


async def close_qdrant_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
