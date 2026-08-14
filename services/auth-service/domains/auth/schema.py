from datetime import datetime
from typing import Any
from pydantic import BaseModel, EmailStr, Field, field_validator
from config import settings
from shared.schemas.base import BaseSchema, IDMixin


# Registration/login
class UserRegister(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=32)
    password: str

    @field_validator("password")
    @classmethod
    def _password_min_length(cls, value: str) -> str:
        if len(value) < settings.PASSWORD_MIN_LENGTH:
            raise ValueError("password must be at least "
                             f"{settings.PASSWORD_MIN_LENGTH} characters")
        return value


class UserLogin(BaseModel):
    email: EmailStr
    password: str
    totp_code: str | None = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


# Email verification/password reset
class VerifyEmailRequest(BaseModel):
    token: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _password_min_length(cls, value: str) -> str:
        if len(value) < settings.PASSWORD_MIN_LENGTH:
            raise ValueError("password must be at least "
                             f"{settings.PASSWORD_MIN_LENGTH} characters")
        return value


# 2FA
class TwoFactorSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str


class TwoFactorVerifyRequest(BaseModel):
    code: str


class TwoFactorDisableRequest(BaseModel):
    code: str


# Profile
class UserProfileRead(BaseSchema):
    first_name: str | None = None
    last_name: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    timezone: str | None = None
    country: str | None = None


class UserSettingsRead(BaseSchema):
    preferred_currency: str
    theme: str
    default_model: str | None = None
    notification_preferences: dict[str, Any] | None = None


class MeRead(BaseSchema, IDMixin):
    email: str
    username: str
    system_role: str
    is_active: bool
    is_verified: bool
    last_seen_at: datetime | None = None
    created_at: datetime
    profile: UserProfileRead | None = None
    settings: UserSettingsRead | None = None


class MeUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=3, max_length=32)
    first_name: str | None = None
    last_name: str | None = None
    bio: str | None = None
    timezone: str | None = None
    country: str | None = None
    preferred_currency: str | None = None
    theme: str | None = None
    default_model: str | None = None
    notification_preferences: dict[str, Any] | None = None


class AvatarUploadRead(BaseModel):
    avatar_url: str


# Refschemas
class UserPublicRead(BaseSchema, IDMixin):
    username: str
    avatar_url: str | None = None
    is_online: bool = False
