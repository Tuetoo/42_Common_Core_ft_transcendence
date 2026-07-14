from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels
from config import settings

_client: AsyncQdrantClient | None = None


def get_qdrant_client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(url=settings.QDRANT_URL)
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
