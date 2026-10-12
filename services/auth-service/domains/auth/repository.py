from datetime import UTC, datetime, timedelta
from uuid import UUID
import security
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from domains.auth.model import (OAuthAccount, SessionRevokeReason,
                                TwoFactorSecret, User, UserProfile,
                                UserSession, UserSettings)
from shared.exceptions import NotFoundError

_EXPIRED_SESSION_RETENTION = timedelta(days=7)


class UserRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        result = await self._db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def require(
            self, user_id: UUID, message: str = "User not found.") -> User:
        user = await self.get_by_id(user_id)
        if user is None:
            raise NotFoundError(message)
        return user

    async def get_by_email(self, email: str) -> User | None:
        result = await self._db.execute(
            select(User).where(func.lower(User.email) == email.lower()))
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> User | None:
        result = await self._db.execute(
            select(User).where(func.lower(User.username) == username.lower()))
        return result.scalar_one_or_none()

    async def create(
            self, *, email: str, username: str, password_hash: str) -> User:
        user = User(
            email=email.lower(), username=username,
            password_hash=password_hash)
        self._db.add(user)
        await self._db.flush()
        return user

    async def create_profile(self, user_id: UUID) -> UserProfile:
        profile = UserProfile(user_id=user_id)
        self._db.add(profile)
        await self._db.flush()
        return profile

    async def create_settings(self, user_id: UUID) -> UserSettings:
        settings_row = UserSettings(user_id=user_id)
        self._db.add(settings_row)
        await self._db.flush()
        return settings_row

    async def get_profile(self, user_id: UUID) -> UserProfile | None:
        result = await self._db.execute(
            select(UserProfile).where(UserProfile.user_id == user_id))
        return result.scalar_one_or_none()

    async def get_settings(self, user_id: UUID) -> UserSettings | None:
        result = await self._db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id))
        return result.scalar_one_or_none()

    async def get_two_factor_secret(
            self, user_id: UUID) -> TwoFactorSecret | None:
        result = await self._db.execute(
            select(TwoFactorSecret).where(TwoFactorSecret.user_id == user_id))
        return result.scalar_one_or_none()

    async def upsert_two_factor_secret(
            self, user_id: UUID, secret: str) -> TwoFactorSecret:
        stored = security.encrypt_secret(secret)
        stmt = pg_insert(TwoFactorSecret).values(
            user_id=user_id, secret=stored, enabled=False)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"], set_={
                "secret": stored,
                "enabled": False,
                "last_used_step": None
            }, where=TwoFactorSecret.enabled.is_(False))
        await self._db.execute(stmt)
        await self._db.flush()
        result = await self._db.execute(
            select(TwoFactorSecret).where(TwoFactorSecret.user_id == user_id))
        return result.scalar_one()

    async def consume_totp_step(self, secret_id: UUID, step: int) -> bool:
        result = await self._db.execute(
            update(TwoFactorSecret).where(
                TwoFactorSecret.id == secret_id,
                or_(
                    TwoFactorSecret.last_used_step.is_(None),
                    TwoFactorSecret.last_used_step
                    < step)).values(last_used_step=step).returning(
                        TwoFactorSecret.id).execution_options(
                            synchronize_session="fetch"))
        return result.scalar_one_or_none() is not None

    async def get_oauth_account(
            self, provider: str, provider_user_id: str) -> OAuthAccount | None:
        result = await self._db.execute(
            select(OAuthAccount).where(
                OAuthAccount.provider == provider,
                OAuthAccount.provider_user_id == provider_user_id))
        return result.scalar_one_or_none()

    async def create_oauth_account(
            self, *, user_id: UUID, provider: str,
            provider_user_id: str) -> OAuthAccount:
        row = OAuthAccount(
            user_id=user_id, provider=provider,
            provider_user_id=provider_user_id)
        self._db.add(row)
        await self._db.flush()
        return row

    async def create_session(
            self, *, user_id: UUID, refresh_token: str, expires_at: datetime,
            ip_address: str | None = None,
            device_info: str | None = None) -> UserSession:
        await self._db.execute(
            delete(UserSession).where(
                UserSession.user_id == user_id, UserSession.expires_at
                < datetime.now(UTC) - _EXPIRED_SESSION_RETENTION))
        row = UserSession(
            user_id=user_id,
            refresh_token_hash=security.hash_refresh_token(refresh_token),
            expires_at=expires_at, ip_address=ip_address,
            device_info=device_info)
        self._db.add(row)
        await self._db.flush()
        return row

    async def get_session_by_refresh_token(
            self, refresh_token: str) -> UserSession | None:
        result = await self._db.execute(
            select(UserSession).where(
                UserSession.refresh_token_hash == security.hash_refresh_token(
                    refresh_token)))
        return result.scalar_one_or_none()

    async def claim_session_for_rotation(
            self, refresh_token: str) -> UserSession | None:
        now = datetime.now(UTC)
        result = await self._db.execute(
            update(UserSession).where(
                UserSession.refresh_token_hash == security.hash_refresh_token(
                    refresh_token), UserSession.revoked_at.is_(None),
                UserSession.expires_at > now).values(
                    revoked_at=now,
                    revoked_reason=SessionRevokeReason.ROTATED).returning(
                        UserSession).execution_options(
                            synchronize_session="fetch"))
        return result.scalar_one_or_none()

    async def revoke_session(
            self, session: UserSession, *,
            reason: str = SessionRevokeReason.LOGOUT) -> None:
        session.revoked_at = datetime.now(UTC)
        session.revoked_reason = reason
        await self._db.flush()

    async def revoke_all_sessions(
            self, user_id: UUID, *,
            reason: str = SessionRevokeReason.REVOKED_ALL) -> int:
        result = await self._db.execute(
            update(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None)).values(
                    revoked_at=datetime.now(UTC),
                    revoked_reason=reason).execution_options(
                        synchronize_session="fetch"))
        return int(getattr(result, "rowcount", 0) or 0)

    async def count_live_sessions(
            self, user_id: UUID, *, used_since: datetime) -> int:
        result = await self._db.execute(
            select(func.count()).select_from(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None), UserSession.expires_at
                > datetime.now(UTC), UserSession.created_at >= used_since))
        return int(result.scalar_one())
