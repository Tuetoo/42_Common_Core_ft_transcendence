import uuid
from datetime import datetime
from typing import Any
from sqlalchemy import (Boolean, DateTime, ForeignKey, String, Text,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from shared.database import Base
from shared.models import TimestampColumnsMixin, UUIDPKMixin


class User(Base, UUIDPKMixin, TimestampColumnsMixin):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    system_role: Mapped[str] = mapped_column(String, nullable=False,
                                             default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                            default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                              default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    profile: Mapped["UserProfile | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan",
        lazy="raise")
    settings: Mapped["UserSettings | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan",
        lazy="raise")
    two_factor_secret: Mapped["TwoFactorSecret | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan",
        lazy="raise")


class UserProfile(Base, UUIDPKMixin):
    __tablename__ = "user_profiles"
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), unique=True,
        nullable=False)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    last_name: Mapped[str | None] = mapped_column(String, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    user: Mapped["User"] = relationship(back_populates="profile", lazy="raise")


class UserSettings(Base, UUIDPKMixin):
    __tablename__ = "user_settings"
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), unique=True,
        nullable=False)
    preferred_currency: Mapped[str] = mapped_column(String, default="USD")
    theme: Mapped[str] = mapped_column(String, default="system")
    default_model: Mapped[str | None] = mapped_column(String, nullable=True)
    notification_preferences: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True)
    user: Mapped["User"] = relationship(back_populates="settings",
                                        lazy="raise")


class UserSession(Base, UUIDPKMixin):
    __tablename__ = "user_sessions"
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
        index=True)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String, nullable=True)
    device_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class OAuthAccount(Base, UUIDPKMixin):
    __tablename__ = "oauth_accounts"
    __table_args__ = (UniqueConstraint("provider", "provider_user_id",
                                       name="uq_oauth_provider_user"),)
    user_id: Mapped[uuid.UUID] = mapped_column(
            PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String, nullable=False)


class TwoFactorSecret(Base, UUIDPKMixin):
    __tablename__ = "two_factor_secrets"
    user_id: Mapped[uuid.UUID] = mapped_column(
                PGUUID(as_uuid=True), ForeignKey("users.id"), unique=True,
                nullable=False)
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                          default=False)
    user: Mapped["User"] = relationship(back_populates="two_factor_secret",
                                        lazy="raise")
