from functools import lru_cache
from typing import Literal
from pydantic import ValidationInfo, field_validator
from shared.config import BaseServiceSettings

LLMProviderName = Literal["google", "mistral", "anthropic", "openai"]
LLMTierName = Literal["cheap", "highest_accuracy"]
AnthropicThinking = Literal["adaptive", "between_tools"]

# Literal settings where an empty value (VAR= copied from .env.example)
# means "use the default", as before they were validated.
_BLANK_IS_DEFAULT = (
    "LLM_DEFAULT_PROVIDER", "LLM_ANTHROPIC_THINKING_CHEAP",
    "LLM_ANTHROPIC_THINKING_HIGHEST_ACCURACY",
    "FILING_RECORDS_BACKFILL_LLM_TIER", "FILING_LLM_TIER",
    "KG_EXTRACTION_LLM_TIER", "RAG_ASK_LLM_TIER")


class AIServiceSettings(BaseServiceSettings):
    SERVICE_NAME: str = "ai-service"
    POSTGRES_SCHEMA: str = "ai"

    QDRANT_HOST: str = "qdrant"
    QDRANT_PORT: int = 6333
    QDRANT_URL: str | None = None
    QDRANT_API_KEY: str = ""
    QDRANT_COLLECTION_RESEARCH: str = "research_documents_gemini"
    QDRANT_VECTOR_SIZE: int = 1536

    EMBEDDING_PROVIDER: str = "google"
    GOOGLE_EMBEDDING_MODEL: str = "gemini-embedding-2"
    GOOGLE_EMBEDDING_DIMENSIONS: int = 1536
    EMBEDDING_REQUEST_TIMEOUT_SECONDS: float = 30.0
    ARCHIVAL_NEAR_DUPLICATE_THRESHOLD: float = 0.85
    ARCHIVAL_RECYCLED_SHARE_MAX: float = 0.5
    PRIOR_NOTE_MAX_SIMILARITY: float = 0.9

    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    GOOGLE_API_KEY_2: str = ""
    GOOGLE_API_KEY_3: str = ""
    GOOGLE_API_KEY_4: str = ""
    GOOGLE_API_KEY_5: str = ""
    GOOGLE_API_KEY_6: str = ""
    GOOGLE_API_KEY_7: str = ""
    GOOGLE_API_KEY_8: str = ""
    GOOGLE_API_KEY_9: str = ""
    GOOGLE_API_KEY_10: str = ""
    MISTRAL_API_KEY: str = ""
    MISTRAL_API_KEY_2: str = ""
    MISTRAL_API_KEY_3: str = ""
    MISTRAL_API_KEY_4: str = ""
    MISTRAL_API_KEY_5: str = ""
    MISTRAL_API_KEY_6: str = ""
    MISTRAL_API_KEY_7: str = ""

    LLM_DEFAULT_PROVIDER: LLMProviderName = "google"
    LLM_OPENAI_MODEL_CHEAP: str = "gpt-5.6-luna"
    LLM_OPENAI_MODEL_HIGHEST_ACCURACY: str = "gpt-5.6-sol"
    LLM_ANTHROPIC_MODEL_CHEAP: str = "claude-haiku-5-5"
    LLM_ANTHROPIC_MODEL_HIGHEST_ACCURACY: str = "claude-sonnet-5-5"
    LLM_GOOGLE_MODEL_CHEAP: str = "gemini-3.5-flash-lite"
    LLM_GOOGLE_MODEL_CHEAP_FALLBACK: str = "gemini-3.1-flash-lite"
    LLM_GOOGLE_MODEL_HIGHEST_ACCURACY: str = "gemini-3.8-flash"
    LLM_GOOGLE_MODEL_HIGHEST_ACCURACY_FALLBACK: str = "gemini-3.6-flash"
    LLM_MISTRAL_MODEL_CHEAP: str = "mistral-small-2603"
    LLM_MISTRAL_MODEL_HIGHEST_ACCURACY: str = "mistral-medium-latest"
    LLM_MISTRAL_MODEL_HIGHEST_ACCURACY_FALLBACK: str = "mistral-large-2512"
    LLM_MISTRAL_MODEL_CHEAP_FALLBACK: str = "ministral-8b-2512"

    LLM_MISTRAL_CHEAP_MIN_INTERVAL_SECONDS: float = 15.0
    LLM_MISTRAL_HIGHEST_ACCURACY_MIN_INTERVAL_SECONDS: float = 22.0

    LLM_GOOGLE_CHEAP_MIN_INTERVAL_SECONDS: float = 5.0
    LLM_GOOGLE_HIGHEST_ACCURACY_MIN_INTERVAL_SECONDS: float = 13.5

    LLM_GOOGLE_LITE_MAX_CONCURRENT_REQUESTS: int = 2

    LLM_GOOGLE_LITE_POOL_MIN_DISPATCH_INTERVAL_SECONDS: float = 3.0

    LLM_GOOGLE_LITE_POOL_MAX_WAIT_SECONDS: float = 30.0

    LLM_GOOGLE_LITE_RUNAWAY_RETRY_THINKING_BUDGET: int = 1024

    LLM_GOOGLE_SERVER_ERROR_COOLDOWN_SECONDS: float = 20.0
    LLM_GOOGLE_HIGHEST_REQUEST_TIMEOUT_SECONDS: float = 30.0
    LLM_GOOGLE_HIGHEST_FALLBACK_REQUEST_TIMEOUT_SECONDS: float = 60.0
    LLM_GOOGLE_TIMEOUT_COOLDOWN_SECONDS: float = 300.0
    LLM_GOOGLE_SERVER_ERROR_COOLDOWN_MAX_SECONDS: float = 300.0

    LLM_ANTHROPIC_REQUEST_TIMEOUT_SECONDS: float = 120.0
    LLM_ANTHROPIC_EFFORT: Literal["", "low", "medium", "high", "xhigh",
                                  "max"] = "low"
    LLM_ANTHROPIC_THINKING_CHEAP: AnthropicThinking = "adaptive"
    LLM_ANTHROPIC_THINKING_HIGHEST_ACCURACY: AnthropicThinking = (
        "between_tools")
    LLM_ANTHROPIC_THINKING_HEADROOM_TOKENS: int = 2048
    LLM_ANTHROPIC_SEND_TEMPERATURE: bool = False
    LLM_ANTHROPIC_PROMPT_CACHING: bool = True
    LLM_ANTHROPIC_CACHE_SCRATCHPAD: bool = True
    LLM_ANTHROPIC_SHARED_EVIDENCE_FIRST: bool = False

    SEC_EDGAR_USER_AGENT: str = ""
    SEC_EDGAR_MAX_REQUESTS_PER_SECOND: float = 8.0
    FILING_RECORDS_MAX_FILINGS: int = 5
    FILING_RECORDS_BACKFILL_LLM_TIER: LLMTierName = "cheap"
    FILING_LLM_TIER: LLMTierName = "highest_accuracy"
    FRED_API_KEY: str = ""
    NEWSAPI_API_KEY: str = ""
    NEWSAPI_SEARCH_IN: str = "title,description"
    NEWSAPI_SORT_BY: str = "relevancy"
    NEWSAPI_RECENT_EXTRA_LIMIT: int = 10
    NEWSAPI_RATE_LIMIT_COOLDOWN_SECONDS: float = 3600.0
    NEWS_RELEVANCE_BATCH_SIZE: int = 8
    NEWS_CONSUMER_MAX_ITEMS: int = 2
    NEWS_WEB_FALLBACK_DAYS: int = 7
    TAVILY_API_KEY: str = ""

    KG_EXTRACTION_LLM_TIER: LLMTierName = "cheap"

    RAG_TOP_K_RESULTS: int = 5

    ANALYSIS_TIMEOUT_SECONDS: float = 2700.0
    ANALYSIS_MAX_ACTIVE_PER_USER: int = 3
    ANALYSIS_QUEUE_MAX_PENDING: int = 50

    LLM_GOOGLE_LITE_COOLDOWN_RETRY_MAX_SECONDS: float = 70.0

    JUDGE_RETRY_BUDGET_SECONDS: float = 240.0
    JUDGE_RETRY_MAX_ATTEMPTS: int = 4
    SENTIMENT_THINKING_BUDGET: int = 1024
    SENTIMENT_TIMEOUT_SECONDS: float = 240.0
    DEBATE_SIDE_REPAIR_DELAY_SECONDS: float = 60.0
    AGENT_PREFETCH_ENABLED: bool = True
    REFLECTION_REVIEW_DELAYED_RETRY_SECONDS: float = 60.0
    JUDGE_STATE_QUARTER_DECLINE_PCT: float = 15.0
    FALLBACK_ANCHOR_MIN_WORD_SHARE: float = 0.3
    TIER_MIN_NOTIONAL_USD: float = 100.0
    TIER_MIN_NOTIONAL_PORTFOLIO_FRACTION: float = 0.01
    MAX_POSITION_WEIGHT_PCT: float = 50.0
    MIN_BUY_TIER_CONFIDENCE: float = 0.5
    TIER_WINDOW_DAYS_MAX: int = 30
    SYSTEM_TIER_ACCEPTANCE_HOURS: int = 72
    MIN_USABLE_AGENTS_FOR_DEBATE: int = 4
    EPISODIC_CONTEXT_LIMIT: int = 3
    ANALYSIS_EXPERIMENT_ISOLATION: bool = False
    AI_ACCURACY_EVALUATION_DAYS: int = 30
    REVIEW_GIVE_UP_AFTER_DAYS: int = 30

    RAG_ASK_RATE_LIMIT_PER_MINUTE: int = 10
    RAG_ASK_RATE_LIMIT_PER_DAY: int = 200
    DECISION_REVIEW_RATE_LIMIT_PER_MINUTE: int = 3
    DECISION_REVIEW_RATE_LIMIT_PER_DAY: int = 20
    RAG_ASK_LLM_TIER: LLMTierName = "cheap"
    RAG_ASK_MAX_OUTPUT_TOKENS: int = 1024
    RAG_ASK_SNIPPET_CHARS: int = 300
    RAG_ASK_STREAM_TIMEOUT_SECONDS: float = 120.0
    SENTIMENT_MAX_INPUT_CHARS: int = 6000

    @field_validator(*_BLANK_IS_DEFAULT, mode="before")
    @classmethod
    def _blank_is_default(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str) and not value.strip() and info.field_name:
            return cls.model_fields[info.field_name].default
        return value.strip() if isinstance(value, str) else value

    def assemble_qdrant_url(self) -> str:
        if self.QDRANT_URL:
            return self.QDRANT_URL
        return f"http://{self.QDRANT_HOST}:{self.QDRANT_PORT}"


@lru_cache
def get_settings() -> AIServiceSettings:
    return AIServiceSettings()


settings = get_settings()
