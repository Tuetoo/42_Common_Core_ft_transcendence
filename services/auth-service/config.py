from functools import lru_cache
from cryptography.fernet import Fernet
from pydantic import model_validator
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
    OAUTH_REDIRECT_BASE_URL: str = "https://localhost:8443"

    TOTP_ISSUER: str = "AlphaMind"
    TOTP_ENCRYPTION_KEY: str = ""

    PASSWORD_MIN_LENGTH: int = 10

    EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS: int = 24
    PASSWORD_RESET_TOKEN_EXPIRE_HOURS: int = 1

    AVATAR_STORAGE_DIR: str = "/app/data/avatars"
    AVATAR_MAX_SIZE_MB: int = 5
    AVATAR_ALLOWED_CONTENT_TYPES: tuple[str, ...] = (
        "image/png", "image/jpeg", "image/webp", "image/gif")

    ORGANIZATION_INVITATION_EXPIRE_DAYS: int = 7

    PRESENCE_ONLINE_THRESHOLD_MINUTES: int = 5

    OAUTH_2FA_CHALLENGE_EXPIRE_MINUTES: int = 5
    TWO_FACTOR_RECENT_LOGIN_MINUTES: int = 10

    UNVERIFIED_ACCOUNT_RETENTION_DAYS: int = 7

    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_LOGIN_PER_EMAIL: int = 10
    RATE_LIMIT_LOGIN_PER_IP: int = 30
    RATE_LIMIT_LOGIN_WINDOW_SECONDS: int = 15 * 60
    RATE_LIMIT_2FA_FAILURES: int = 5
    RATE_LIMIT_2FA_WINDOW_SECONDS: int = 5 * 60
    RATE_LIMIT_2FA_LOCK_SECONDS: int = 15 * 60
    RATE_LIMIT_PASSWORD_RESET_PER_EMAIL: int = 3
    RATE_LIMIT_PASSWORD_RESET_PER_IP: int = 10
    RATE_LIMIT_PASSWORD_RESET_WINDOW_SECONDS: int = 60 * 60
    RATE_LIMIT_REGISTER_PER_IP: int = 10
    RATE_LIMIT_REGISTER_WINDOW_SECONDS: int = 60 * 60
    RATE_LIMIT_RESEND_VERIFICATION_PER_EMAIL: int = 3
    RATE_LIMIT_RESEND_VERIFICATION_WINDOW_SECONDS: int = 60 * 60
    RATE_LIMIT_INVITATIONS_PER_ORG: int = 20
    RATE_LIMIT_INVITATIONS_WINDOW_SECONDS: int = 60 * 60
    RATE_LIMIT_FRIEND_REQUESTS_PER_USER: int = 30
    RATE_LIMIT_FRIEND_REQUESTS_WINDOW_SECONDS: int = 60 * 60

    @model_validator(mode="after")
    def _check_totp_key(self) -> "AuthServiceSettings":
        if self.TOTP_ENCRYPTION_KEY:
            try:
                Fernet(self.TOTP_ENCRYPTION_KEY.encode())
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    "TOTP_ENCRYPTION_KEY is not a valid Fernet key (generate "
                    "one with cryptography.fernet.Fernet.generate_key())."
                ) from exc
        elif self.APP_ENV not in ("development", "test"):
            raise ValueError(
                f"TOTP_ENCRYPTION_KEY must be set when APP_ENV={self.APP_ENV}."
            )
        return self


@lru_cache
def get_settings() -> AuthServiceSettings:
    return AuthServiceSettings()


settings = get_settings()
