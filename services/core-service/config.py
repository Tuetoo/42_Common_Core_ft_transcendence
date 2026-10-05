from functools import lru_cache
from typing import ClassVar
from shared.config import BaseServiceSettings
from shared.enums import DocumentCategory


class CoreServiceSettings(BaseServiceSettings):
    SERVICE_NAME: str = "core-service"
    POSTGRES_SCHEMA: str = "core"

    PUBLIC_API_RATE_LIMIT_PER_MINUTE: int = 60
    MAX_ACTIVE_API_KEYS_PER_USER: int = 10

    NOTIFICATION_READ_RETENTION_DAYS: int = 90
    NOTIFICATION_PURGE_BATCH_SIZE: int = 1000

    MAX_UPLOAD_SIZE_BYTES: int = 20 * 1024 * 1024
    YJS_MAX_UPDATE_BYTES: int = 1024 * 1024
    ALLOWED_DOCUMENT_EXTENSIONS: ClassVar[set[str]] = {".pdf", ".txt", ".md"}
    ALLOWED_DOCUMENT_CATEGORIES: ClassVar[set[str]] = {
        c.value
        for c in DocumentCategory
    }


@lru_cache
def get_settings() -> CoreServiceSettings:
    return CoreServiceSettings()


settings = get_settings()
