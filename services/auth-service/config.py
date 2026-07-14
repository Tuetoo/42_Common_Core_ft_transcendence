from functools import lru_cache
from shared.config import BaseServiceSettings


class AuthServiceSettings(BaseServiceSettings):
    SERVICE_NAME: str = "auth-service"
    POSTGRES_SCHEMA: str = "auth"

    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""
    OAUTH_REDIRECT_BASE_URL: str = "http://localhost:8000"

    TOTP_ISSUER: str = "AlphaMind"

    PASSWORD_MIN_LENGTH: int = 10


@lru_cache
def get_settings() -> AuthServiceSettings:
    return AuthServiceSettings()


settings = get_settings()
