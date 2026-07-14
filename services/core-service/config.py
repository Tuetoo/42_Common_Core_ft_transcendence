from functools import lru_cache
from shared.config import BaseServiceSettings


class CoreServiceSettings(BaseServiceSettings):
    SERVICE_NAME: str = "core-service"
    POSTGRES_SCHEMA: str = "core"

    PUBLIC_API_RATE_LIMIT_PER_MINUTE: int = 60

    MAX_UPLOAD_SIZE_BYTES: int = 20 * 1024 * 1024
    ALLOWED_DOCUMENT_EXTENSIONS: set[str] = {".pdf", ".txt", ".md"}


@lru_cache
def get_settings() -> CoreServiceSettings:
    return CoreServiceSettings()


settings = get_settings()
