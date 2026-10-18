import logging
from functools import lru_cache
from typing import Literal
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_JWT_SECRET_MIN_LENGTH = 32

_internal_token_warned = False


class BaseServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")

    SERVICE_NAME: str = "unnamed-service"
    APP_ENV: Literal["development", "staging", "production",
                     "test"] = ("development")
    API_V1_PREFIX: str = "/api/v1"

    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "alphamind"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_SCHEMA: str = "public"

    DATABASE_URL: str | None = None
    SYNC_DATABASE_URL: str | None = None

    DB_POOL_SIZE: int = 20
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

    INTERNAL_API_TOKEN: str = ""

    USER_STATE_CHECK_ENABLED: bool = True
    USER_STATE_CACHE_TTL_SECONDS: int = 300

    AUTH_SERVICE_URL: str = "http://auth-service:8000"
    CORE_SERVICE_URL: str = "http://core-service:8000"
    AI_SERVICE_URL: str = "http://ai-service:8000"

    FRONTEND_BASE_URL: str = "http://localhost:3000"

    SMTP_HOST: str = "mailhog"
    SMTP_PORT: int = 1025
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "noreply@alphamind.local"
    SMTP_USE_TLS: bool = False
    SMTP_TIMEOUT_SECONDS: float = 10.0

    BACKEND_CORS_ORIGINS: str = "http://localhost:3000"

    def cors_origins(self) -> list[str]:
        return [
            origin.strip() for origin in self.BACKEND_CORS_ORIGINS.split(",")
            if origin.strip()
        ]

    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    S3_ENDPOINT_URL: str = "http://minio:9000"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_REGION: str = "us-east-1"
    S3_USE_SSL: bool = False
    S3_BUCKET_DOCUMENTS: str = "research-documents"

    def assemble_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:"
            f"{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:"
            f"{self.POSTGRES_PORT}/{self.POSTGRES_DB}")

    def assemble_sync_database_url(self) -> str:
        if self.SYNC_DATABASE_URL:
            return self.SYNC_DATABASE_URL
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:"
            f"{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:"
            f"{self.POSTGRES_PORT}/{self.POSTGRES_DB}")

    def assemble_redis_url(self) -> str:
        if self.REDIS_URL:
            return self.REDIS_URL
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}"

    @model_validator(mode="after")
    def _check_jwt_secret(self) -> "BaseServiceSettings":
        weak = (
            not self.JWT_SECRET or self.JWT_SECRET == "CHANGE_ME"
            or len(self.JWT_SECRET) < _JWT_SECRET_MIN_LENGTH)
        if weak:
            if self.APP_ENV in ("development", "test"):
                logging.getLogger(__name__).warning(
                    "JWT_SECRET is weak (placeholder, empty or shorter than "
                    "%d chars); acceptable only in %s.",
                    _JWT_SECRET_MIN_LENGTH, self.APP_ENV)
            else:
                raise ValueError(
                    f"JWT_SECRET must be set to a random value of at least "
                    f"{_JWT_SECRET_MIN_LENGTH} characters when APP_ENV="
                    f"{self.APP_ENV}.")
        return self

    @model_validator(mode="after")
    def _check_internal_token(self) -> "BaseServiceSettings":
        global _internal_token_warned
        if self.INTERNAL_API_TOKEN:
            return self
        if self.APP_ENV in ("development", "test"):
            if not _internal_token_warned:
                _internal_token_warned = True
                logging.getLogger(__name__).warning(
                    "INTERNAL_API_TOKEN is empty: /internal/* routes accept "
                    "requests without a token; acceptable only in %s.",
                    self.APP_ENV)
            return self
        raise ValueError(
            "INTERNAL_API_TOKEN must be set to a random value (e.g. "
            "`openssl rand -hex 32`) when APP_ENV="
            f"{self.APP_ENV}; every service and the scheduler share it.")


@lru_cache
def get_settings() -> BaseServiceSettings:
    return BaseServiceSettings()
