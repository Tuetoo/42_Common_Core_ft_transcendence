from functools import lru_cache
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8",
        case_sensitive=True, extra="ignore")

    SERVICE_NAME: str = "unnamed-service"
    APP_ENV: Literal["development", "staging", "production", "test"
                     ] = "development"
    APP_DEBUG: bool = True
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_V1_PREFIX: str = "/api/v1"

    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "alphamind"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_SCHEMA: str = "public"

    # async URL used by SQLAlchemy
    DATABASE_URL: str | None = None
    # used by Alembic (sync driver)
    SYNC_DATABASE_URL: str | None = None

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    DB_ECHO: bool = False

    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_URL: str | None = None
    REDIS_DB_CACHE: int = 0
    REDIS_DB_RATE_LIMIT: int = 1
    REDIS_DB_EVENTS: int = 2
    REDIS_DB_QUEUE: int = 3

    JWT_SECRET: str = "CHANGE_ME"
    JWT_ALGORITHM: str = "HS256"

    AUTH_SERVICE_URL: str = "http://auth-service:8000"
    CORE_SERVICE_URL: str = "http://core-service:8000"
    AI_SERVICE_URL: str = "http://ai-service:8000"

    # LANGCHAIN_TRACING_V2: bool = False
    # LANGCHAIN_API_KEY: str = ""
    # LANGCHAIN_PROJECT: str = "alphamind"

    # BACKEND_CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    def assemble_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:"
            f"{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:"
            f"{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    def assemble_sync_database_url(self) -> str:
        if self.SYNC_DATABASE_URL:
            return self.SYNC_DATABASE_URL
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:"
            f"{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:"
            f"{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    def assemble_redis_url(self) -> str:
        if self.REDIS_URL:
            return self.REDIS_URL
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}"

    # @property
    # def is_production(self) -> bool:
    #     return self.APP_ENV == "production"


@lru_cache
def get_settings() -> BaseServiceSettings:
    return BaseServiceSettings()
