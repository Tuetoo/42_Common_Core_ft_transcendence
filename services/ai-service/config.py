from functools import lru_cache
from shared.config import BaseServiceSettings


class AIServiceSettings(BaseServiceSettings):
    SERVICE_NAME: str = "ai-service"
    POSTGRES_SCHEMA: str = "ai"

    QDRANT_HOST: str = "qdrant"
    QDRANT_PORT: int = 6333
    QDRANT_URL: str | None = None
    QDRANT_COLLECTION_RESEARCH: str = "research_documents"
    QDRANT_VECTOR_SIZE: int = 1024

    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    MISTRAL_API_KEY: str = ""
    MISTRAL_API_KEY_2: str = ""
    MISTRAL_API_KEY_3: str = ""
    MISTRAL_API_KEY_4: str = ""
    MISTRAL_API_KEY_5: str = ""

    LLM_DEFAULT_PROVIDER: str = "mistral"
    LLM_OPENAI_MODEL_FAST: str = "gpt-5.6-luna"
    LLM_OPENAI_MODEL_CHEAP: str = "gpt-5.6-luna"
    LLM_OPENAI_MODEL_HIGHEST_ACCURACY: str = "gpt-5.6-sol"
    LLM_ANTHROPIC_MODEL_FAST: str = "claude-haiku-4-5-20251001"
    LLM_ANTHROPIC_MODEL_CHEAP: str = "claude-haiku-4-5-20251001"
    LLM_ANTHROPIC_MODEL_HIGHEST_ACCURACY: str = "claude-opus-4-8"
    LLM_GOOGLE_MODEL_FAST: str = "gemini-3.5-flash-lite"
    LLM_GOOGLE_MODEL_CHEAP: str = "gemini-3.5-flash-lite"
    LLM_GOOGLE_MODEL_HIGHEST_ACCURACY: str = "gemini-3.1-pro"
    LLM_MISTRAL_MODEL_FAST: str = "mistral-small-latest"
    LLM_MISTRAL_MODEL_CHEAP: str = "mistral-small-latest"
    LLM_MISTRAL_MODEL_HIGHEST_ACCURACY: str = "mistral-medium-latest"
    LLM_MISTRAL_MODEL_HIGHEST_ACCURACY_FALLBACK: str = "mistral-large-latest"
    LLM_MISTRAL_MODEL_CHEAP_FALLBACK: str = "ministral-8b-2512"
    LLM_MISTRAL_CHEAP_MIN_INTERVAL_SECONDS: float = 1.3
    LLM_MISTRAL_HIGHEST_ACCURACY_MIN_INTERVAL_SECONDS: float = 1.3

    SEC_EDGAR_USER_AGENT: str = ""
    FRED_API_KEY: str = ""
    NEWSAPI_API_KEY: str = ""
    REDDIT_CLIENT_ID: str = ""
    REDDIT_CLIENT_SECRET: str = ""
    REDDIT_USER_AGENT: str = "AlphaMind/1.0 (research platform)"
    TAVILY_API_KEY: str = ""
    KG_EXTRACTION_LLM_TIER: str = "cheap"

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
