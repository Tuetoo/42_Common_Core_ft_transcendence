from functools import lru_cache
from shared.config import BaseServiceSettings


class AIServiceSettings(BaseServiceSettings):
    SERVICE_NAME: str = "ai-service"
    POSTGRES_SCHEMA: str = "ai"

    QDRANT_HOST: str = "qdrant"
    QDRANT_PORT: int = 6333
    QDRANT_URL: str | None = None
    QDRANT_COLLECTION_RESEARCH: str = "research_documents"
    QDRANT_VECTOR_SIZE: int = 1536

    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    MISTRAL_API_KEY: str = ""

    RAG_CHUNK_SIZE_TOKEN: int = 512
    RAG_CHUNK_OVERLAP_TOKEN: int = 64
    RAG_TOP_K_RESULTS: int = 5

    def assemble_qdrant_url(self) -> str:
        if self.QDRANT_URL:
            return self.QDRANT_URL
        return f"http://{self.QDRANT_HOST}:{self.QDRANT_PORT}"


@lru_cache
def get_settings() -> AIServiceSettings:
    return AIServiceSettings()


settings = get_settings()
