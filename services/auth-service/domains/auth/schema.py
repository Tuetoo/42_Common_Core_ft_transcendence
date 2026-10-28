from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import security
from config import settings
from pydantic import (AfterValidator, BaseModel, ConfigDict, EmailStr, Field,
                      field_validator, model_validator)
from shared.enums import NotificationChannel
from shared.schemas import BaseSchema, IDMixin

USERNAME_PATTERN = r"^[A-Za-z0-9_.-]{3,32}$"

PROFILE_FIELDS = ("first_name", "last_name", "bio", "timezone", "country")


def normalize_email(value: str) -> str:
    return value.strip().lower()


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone '{value}'") from exc
    return value


Email = Annotated[EmailStr, AfterValidator(normalize_email)]
TimezoneName = Annotated[str,
                         Field(max_length=64),
                         AfterValidator(validate_timezone)]


def validate_new_password(value: str) -> str:
    if len(value) < settings.PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"password must be at least {settings.PASSWORD_MIN_LENGTH} "
            "characters")
    return validate_password_bytes(value)


def validate_password_bytes(value: str) -> str:
    if len(value.encode("utf-8")) > security.PASSWORD_MAX_BYTES:
        raise ValueError(
            f"password must be at most {security.PASSWORD_MAX_BYTES} bytes")
    return value


class UserRegister(BaseModel):
    email: Email
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return validate_new_password(value)


class UserLogin(BaseModel):
    email: Email
    password: str
    totp_code: str | None = Field(default=None, max_length=16)

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return validate_password_bytes(value)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class OAuthTwoFactorChallenge(BaseModel):
    challenge_token: str
    expires_in: int


class OAuthTwoFactorVerify(BaseModel):
    challenge_token: str
    code: str = Field(min_length=1, max_length=16)


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class VerifyEmailRequest(BaseModel):
    token: str


class ResendVerificationRequest(BaseModel):
    email: Email


class PasswordResetRequest(BaseModel):
    email: Email


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return validate_new_password(value)


class TwoFactorSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str


class TwoFactorVerifyRequest(BaseModel):
    code: str = Field(max_length=16)


class TwoFactorDisableRequest(BaseModel):
    code: str = Field(max_length=16)


class UserProfileRead(BaseSchema):
    first_name: str | None = None
    last_name: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    timezone: str | None = None
    country: str | None = None


class UserSettingsRead(BaseSchema):
    notification_preferences: dict[str, Any] | None = None


class MeRead(BaseSchema, IDMixin):
    email: str
    username: str
    system_role: str
    permissions: list[str] = Field(default_factory=list)
    is_active: bool
    is_verified: bool
    last_seen_at: datetime | None = None
    created_at: datetime
    profile: UserProfileRead | None = None
    settings: UserSettingsRead | None = None


class MeUpdate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "PATCH semantics: only fields present in the body change. An "
                "explicit null clears a nullable field; null is rejected "
                "(422) for username.")
        })

    username: str | None = Field(default=None, pattern=USERNAME_PATTERN)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=500)
    timezone: TimezoneName | None = None
    country: str | None = Field(default=None, max_length=100)
    notification_preferences: dict[NotificationChannel, bool] | None = None

    @model_validator(mode="after")
    def _no_null_for_required(self) -> "MeUpdate":
        if "username" in self.model_fields_set and self.username is None:
            raise ValueError("username cannot be null")
        return self


class AvatarUploadRead(BaseModel):
    avatar_url: str
