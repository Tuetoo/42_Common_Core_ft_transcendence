import sys
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AI_SERVICE_DIR = _REPO_ROOT / "services" / "ai-service"

sys.path.insert(0, str(_AI_SERVICE_DIR))
sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(scope="session", autouse=True)
async def _bm25_index_hydrated():
    from ai.rag.indexing import rebuild_bm25_from_qdrant

    count = await rebuild_bm25_from_qdrant()
    print(f"\n[conftest] Rehydrated BM25 index from Qdrant: {count} chunks")
