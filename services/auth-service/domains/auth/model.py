from datetime import datetime
from typing import Any
from uuid import UUID
from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Index,
                        String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from shared.database import Base, TimestampColumnsMixin, UUIDPKMixin


class User(Base, UUIDPKMixin, TimestampColumnsMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    system_role: Mapped[str] = mapped_column(
        String, nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


Index("uq_users_email_lower", func.lower(User.email), unique=True)
Index("uq_users_username_lower", func.lower(User.username), unique=True)


class UserProfile(Base, UUIDPKMixin):
    __tablename__ = "user_profiles"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), unique=True,
        nullable=False)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    last_name: Mapped[str | None] = mapped_column(String, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)


class UserSettings(Base, UUIDPKMixin):
    __tablename__ = "user_settings"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), unique=True,
        nullable=False)
    preferred_currency: Mapped[str] = mapped_column(String, default="USD")
    theme: Mapped[str] = mapped_column(String, default="system")
    default_model: Mapped[str | None] = mapped_column(String, nullable=True)
    notification_preferences: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True)


class SessionRevokeReason:
    ROTATED = "rotated"
    LOGOUT = "logout"
    REVOKED_ALL = "revoked_all"
    REUSE = "reuse"


class UserSession(Base, UUIDPKMixin):
    __tablename__ = "user_sessions"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
        index=True)
    refresh_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String, nullable=True)
    device_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(
        String(20), nullable=True)


Index(
    "uq_user_sessions_refresh_token_hash", UserSession.refresh_token_hash,
    unique=True)


class OAuthAccount(Base, UUIDPKMixin):
    __tablename__ = "oauth_accounts"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_user_id", name="uq_oauth_provider_user"), )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String, nullable=False)


class TwoFactorSecret(Base, UUIDPKMixin):
    __tablename__ = "two_factor_secrets"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), unique=True,
        nullable=False)
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)
    last_used_step: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True)
